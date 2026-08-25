"""Minimal command-line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeagent import (
    Agent,
    ContextManager,
    EnvironmentConfig,
    MemoryManager,
    MemoryStore,
    PlanningBackend,
    PromptRuntime,
    RecoveryRuntime,
    SkillLoader,
    TodoStore,
    create_default_hooks,
    create_default_registry,
    resolve_planning_backend,
)
from codeagent.mcp import McpRouter
from codeagent.web.storage import SQLiteRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the codeagent Anthropic agent.")
    parser.add_argument(
        "query",
        nargs="*",
        help="Prompt to run once. If omitted, starts an interactive loop.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming text output even if STREAMING=true.",
    )
    parser.add_argument(
        "--planning-mode",
        choices=("auto", "tasks", "todo"),
        help="Planning backend. Interactive mode defaults to tasks; one-shot defaults to todo.",
    )
    parser.add_argument(
        "--task-list",
        help="Use an existing task list when the tasks backend is active.",
    )
    args = parser.parse_args(argv)

    env = EnvironmentConfig.from_env()
    stream = env.stream and not args.no_stream
    workspace = Path.cwd()
    query = " ".join(args.query).strip()
    requested_backend = args.planning_mode or env.planning_mode
    planning_backend = resolve_planning_backend(
        requested_backend,
        interactive=not bool(query),
    )
    skill_loader = create_skill_loader(env, workspace)
    skill_catalog = skill_loader.catalog_prompt() if skill_loader is not None else ""
    recovery_runtime = RecoveryRuntime(
        env.recovery_config,
        log=print if env.recovery_config.trace else None,
    )
    memory_store = create_memory_store(env, workspace)
    memory_manager = (
        MemoryManager(
            memory_store,
            env.memory_config,
            recovery_runtime=recovery_runtime,
        )
        if memory_store is not None
        else None
    )
    memory_catalog = (
        memory_manager.catalog_prompt() if memory_manager is not None else ""
    )
    todo_store = TodoStore() if planning_backend is PlanningBackend.TODO else None
    task_repository = None
    task_list_id = None
    task_state_provider = None
    if planning_backend is PlanningBackend.TASKS:
        task_repository = SQLiteRepository.for_workspace(workspace)
        if args.task_list:
            task_list = task_repository.get_task_list(args.task_list)
            if task_list is None:
                parser.error(f"task list not found: {args.task_list}")
            if Path(task_list.workspace).resolve() != workspace.resolve():
                parser.error("task list belongs to another workspace")
        else:
            task_list = task_repository.create_task_list(
                workspace=workspace,
                name="CLI tasks",
            )
        task_list_id = task_list.id
        print(f"Task list: {task_list_id}")

        def task_state() -> str:
            resources = task_repository.list_task_resources(task_list_id)
            return json.dumps(
                [resource.to_dict(camel_case=True) for resource in resources],
                ensure_ascii=False,
                indent=2,
            )

        task_state_provider = task_state
    context = ContextManager(
        config=env.context_config,
        todo_store=todo_store,
        task_state_provider=task_state_provider,
    )
    prompt_runtime = PromptRuntime(workspace=workspace, config=env.prompt_config)
    tools = create_default_registry(
        todo_store=todo_store,
        todo_log=print,
        skill_loader=skill_loader,
        memory_store=memory_store,
        memory_max_items=env.memory_config.max_loaded_items,
        planning_backend=planning_backend,
        task_service=task_repository,
        task_list_id=task_list_id,
    )
    mcp_path = (
        env.mcp_config_path
        if env.mcp_config_path.is_absolute()
        else workspace / env.mcp_config_path
    )
    mcp_router = McpRouter(mcp_path)
    mcp_router.register_tools(tools)

    agent = Agent(
        client=env.create_anthropic_client(
            stream=stream,
            on_text=print_stream_token if stream else None,
        ),
        tools=tools,
        config=env.to_agent_config(planning_backend=planning_backend),
        hooks=create_default_hooks(
            workspace=workspace,
            todo_store=todo_store,
            planning_backend=planning_backend,
            task_state_provider=task_state_provider,
        ),
        context=context,
        memory_manager=memory_manager,
        prompt_runtime=prompt_runtime,
        prompt_log=print if env.prompt_config.emit_trace else None,
        recovery_runtime=recovery_runtime,
        subagent_environment_factory=lambda: create_default_subagent_environment(
            workspace,
            skill_loader,
            memory_store,
            env,
        ),
        subagent_log=print,
        skill_catalog=skill_catalog,
        memory_catalog=memory_catalog,
    )

    try:
        if query:
            result = agent.run(query)
            if not stream and result.final_text:
                print(result.final_text)
            elif stream:
                print()
            return 0

        print("codeagent interactive mode. Type q, quit, or exit to stop.")
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if user_input.lower() in {"q", "quit", "exit"}:
                return 0
            if not user_input:
                continue

            result = agent.run(user_input)
            if not stream and result.final_text:
                print(result.final_text)
            elif stream:
                print()
    finally:
        mcp_router.close()


def print_stream_token(token: str) -> None:
    print(token, end="", flush=True)


def create_skill_loader(env: EnvironmentConfig, workspace: Path) -> SkillLoader | None:
    if not env.enable_skills:
        return None

    roots = [
        root if root.is_absolute() else workspace / root
        for root in env.skill_roots
    ]
    return SkillLoader(roots=roots)


def create_memory_store(env: EnvironmentConfig, workspace: Path) -> MemoryStore | None:
    if not env.memory_config.enabled:
        return None

    root = env.memory_config.memory_dir
    if not root.is_absolute():
        root = workspace / root
    return MemoryStore(
        root=root,
        max_memory_bytes=env.memory_config.max_memory_bytes,
    )


def create_default_subagent_environment(
    workspace: Path,
    skill_loader: SkillLoader | None,
    memory_store: MemoryStore | None,
    env: EnvironmentConfig,
):
    todo_store = TodoStore()
    context = ContextManager(config=env.context_config, todo_store=todo_store)
    return (
        create_default_registry(
            todo_store=todo_store,
            todo_log=print,
            skill_loader=skill_loader,
            memory_store=memory_store,
            allow_memory_write=env.memory_config.allow_subagent_write,
            memory_max_items=env.memory_config.max_loaded_items,
        ),
        create_default_hooks(workspace=workspace, todo_store=todo_store),
        context,
    )


if __name__ == "__main__":
    raise SystemExit(main())
