"""Composition root for an isolated web-run Agent instance."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from codeagent import (
    Agent,
    ContextManager,
    EnvironmentConfig,
    MemoryManager,
    MemoryStore,
    PromptRuntime,
    RecoveryRuntime,
    SkillLoader,
    TodoStore,
    create_default_hooks,
    create_default_registry,
)
from codeagent.context import RuntimeState
from codeagent.events import EventEmitter, UsageTracker
from codeagent.permissions import PermissionPolicy, WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.tools import WorkspaceGuard


class WebAgentFactory:
    """Build fresh, non-shared mutable runtime objects for one workspace."""

    def __init__(self, env: EnvironmentConfig, workspace: str | Path) -> None:
        self.env = env
        self.workspace = Path(workspace).resolve()
        self.workspace_guard = WorkspaceGuard(self.workspace)

    def create(
        self,
        *,
        event_emitter: EventEmitter,
        cancellation: CancellationToken,
        permission_broker: WaitingPermissionBroker,
        checkpoint: Any | None = None,
    ) -> Agent:
        todo_store = TodoStore()
        state = RuntimeState()
        messages: list[dict[str, Any]] = []
        if checkpoint is not None:
            messages = [dict(item) for item in checkpoint.messages]
            todo_store.todos = [dict(item) for item in checkpoint.todos]
            todo_store.revision = int(checkpoint.metadata.get("todo_revision", 0))
            state = _restore_runtime_state(checkpoint.context)

        changed_files = set(state.files_changed)
        context_config = replace(
            self.env.context_config,
            transcript_dir=_workspace_path(
                self.workspace_guard, self.env.context_config.transcript_dir
            ),
            tool_output_dir=_workspace_path(
                self.workspace_guard, self.env.context_config.tool_output_dir
            ),
        )
        context = ContextManager(
            config=context_config,
            state=state,
            todo_store=todo_store,
        )
        skill_loader = self._skill_loader()
        memory_store = self._memory_store()
        recovery = RecoveryRuntime(self.env.recovery_config)
        memory_manager = (
            MemoryManager(
                memory_store,
                self.env.memory_config,
                recovery_runtime=recovery,
            )
            if memory_store is not None
            else None
        )
        usage_tracker = UsageTracker()

        def todo_changed(_rendered: str) -> None:
            event_emitter.emit(
                "todo.updated",
                {
                    "revision": todo_store.revision,
                    "todos": [
                        {"id": f"todo-{index}", **dict(todo)}
                        for index, todo in enumerate(todo_store.todos)
                    ],
                },
            )

        def tools_for():
            return create_default_registry(
                todo_store=todo_store,
                todo_log=todo_changed,
                skill_loader=skill_loader,
                memory_store=memory_store,
                allow_memory_write=True,
                memory_max_items=self.env.memory_config.max_loaded_items,
                workspace_guard=self.workspace_guard,
                changed_files=changed_files,
            )

        policy = PermissionPolicy(
            workspace=self.workspace,
            broker=permission_broker,
            cancellation=cancellation,
        )
        hooks = create_default_hooks(
            permission_policy=policy,
            workspace=self.workspace,
            todo_store=todo_store,
            log=lambda _message: None,
        )
        client = self.env.create_anthropic_client(
            stream=self.env.stream,
            event_emitter=event_emitter,
            usage_tracker=usage_tracker,
        )

        def subagent_environment():
            sub_todos = TodoStore()
            return (
                create_default_registry(
                    todo_store=sub_todos,
                    skill_loader=skill_loader,
                    memory_store=memory_store,
                    allow_memory_write=self.env.memory_config.allow_subagent_write,
                    memory_max_items=self.env.memory_config.max_loaded_items,
                    workspace_guard=self.workspace_guard,
                    changed_files=changed_files,
                ),
                create_default_hooks(
                    permission_policy=policy,
                    workspace=self.workspace,
                    todo_store=sub_todos,
                    log=lambda _message: None,
                ),
                ContextManager(config=context_config, todo_store=sub_todos),
            )

        return Agent(
            client=client,
            tools=tools_for(),
            config=self.env.to_agent_config(),
            hooks=hooks,
            context=context,
            memory_manager=memory_manager,
            prompt_runtime=PromptRuntime(
                workspace=self.workspace,
                config=self.env.prompt_config,
            ),
            recovery_runtime=recovery,
            event_emitter=event_emitter,
            usage_tracker=usage_tracker,
            cancellation=cancellation,
            messages=messages,
            subagent_environment_factory=subagent_environment,
            skill_catalog=(skill_loader.catalog_prompt() if skill_loader else ""),
            memory_catalog=(memory_manager.catalog_prompt() if memory_manager else ""),
        )

    def for_workspace(self, workspace: str | Path) -> "WebAgentFactory":
        """Return an isolated factory while reusing immutable environment config."""

        return type(self)(self.env, workspace)

    def _skill_loader(self) -> SkillLoader | None:
        if not self.env.enable_skills:
            return None
        roots = [
            self.workspace_guard.ensure_within(root)
            if root.is_absolute()
            else self.workspace_guard.resolve(root)
            for root in self.env.skill_roots
        ]
        return SkillLoader(roots=roots)

    def _memory_store(self) -> MemoryStore | None:
        if not self.env.memory_config.enabled:
            return None
        configured = self.env.memory_config.memory_dir
        root = (
            self.workspace_guard.ensure_within(configured)
            if configured.is_absolute()
            else self.workspace_guard.resolve(configured)
        )
        return MemoryStore(
            root=root,
            max_memory_bytes=self.env.memory_config.max_memory_bytes,
        )


def serialize_runtime_state(state: RuntimeState) -> dict[str, Any]:
    return {item.name: getattr(state, item.name) for item in fields(RuntimeState)}


def _restore_runtime_state(payload: dict[str, Any]) -> RuntimeState:
    allowed = {item.name for item in fields(RuntimeState)}
    values = {key: value for key, value in dict(payload or {}).items() if key in allowed}
    return RuntimeState(**values)


def _workspace_path(guard: WorkspaceGuard, value: Path) -> Path:
    return guard.ensure_within(value) if value.is_absolute() else guard.resolve(value)


__all__ = ["WebAgentFactory", "serialize_runtime_state"]
