"""Minimal command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from codeagent import (
    Agent,
    ContextManager,
    EnvironmentConfig,
    SkillLoader,
    TodoStore,
    create_default_hooks,
    create_default_registry,
)


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
    args = parser.parse_args(argv)

    env = EnvironmentConfig.from_env()
    stream = env.stream and not args.no_stream
    workspace = Path.cwd()
    skill_loader = create_skill_loader(env, workspace)
    skill_catalog = skill_loader.catalog_prompt() if skill_loader is not None else ""
    todo_store = TodoStore()
    context = ContextManager(config=env.context_config, todo_store=todo_store)
    agent = Agent(
        client=env.create_anthropic_client(
            stream=stream,
            on_text=print_stream_token if stream else None,
        ),
        tools=create_default_registry(
            todo_store=todo_store,
            todo_log=print,
            skill_loader=skill_loader,
        ),
        config=env.to_agent_config(),
        hooks=create_default_hooks(workspace=workspace, todo_store=todo_store),
        context=context,
        subagent_environment_factory=lambda: create_default_subagent_environment(
            workspace,
            skill_loader,
            env,
        ),
        subagent_log=print,
        skill_catalog=skill_catalog,
    )

    query = " ".join(args.query).strip()
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


def create_default_subagent_environment(
    workspace: Path,
    skill_loader: SkillLoader | None,
    env: EnvironmentConfig,
):
    todo_store = TodoStore()
    context = ContextManager(config=env.context_config, todo_store=todo_store)
    return (
        create_default_registry(
            todo_store=todo_store,
            todo_log=print,
            skill_loader=skill_loader,
        ),
        create_default_hooks(workspace=workspace, todo_store=todo_store),
        context,
    )


if __name__ == "__main__":
    raise SystemExit(main())
