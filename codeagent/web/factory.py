"""Composition root for an isolated web-run Agent instance."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent import (
    Agent,
    ContextManager,
    EnvironmentConfig,
    MemoryManager,
    MemoryStore,
    PromptMode,
    PromptRuntime,
    PlanningBackend,
    RecoveryRuntime,
    SkillLoader,
    TodoStore,
    create_default_hooks,
    create_default_registry,
)
from codeagent.context import RuntimeState
from codeagent.events import EventEmitter, UsageTracker
from codeagent.mcp import McpRouter
from codeagent.messages import reconcile_tool_history
from codeagent.memory import MemoryAccessController, MemoryWriteBlocked
from codeagent.permissions import PermissionPolicy, WaitingPermissionBroker
from codeagent.runtime import CancellationToken, RuntimeDataPaths
from codeagent.runtime.activity import ExecutionActivity
from codeagent.tools import LoadContextHistoryTool, LoadToolOutputTool, ToolRegistry, WorkspaceGuard
from codeagent.web.questions import WebUserQuestions
from codeagent.teams import (
    ActiveTeamRootToolExecutionGate,
    AgentSessionRecord,
    LeadTeamPlanTool,
    ReadOnlyTeamToolExecutionGate,
    TaskAttemptRecord,
    TeamAgentRole,
    TeamPlannerToolExecutionGate,
    TeamRunState,
    TeamStatusTool,
    TeamToolExecutionGate,
    create_lead_tools,
    create_teammate_tools,
)
from codeagent.worktrees import WorktreeManager, WorktreeManagerRegistry


class WebAgentFactory:
    """Build fresh, non-shared mutable runtime objects for one workspace."""

    def __init__(
        self,
        env: EnvironmentConfig,
        workspace: str | Path,
        task_service: Any,
        *,
        project_workspace: str | Path | None = None,
        data_paths: RuntimeDataPaths | None = None,
        memory_access: MemoryAccessController | None = None,
    ) -> None:
        self.env = env
        self.workspace = Path(workspace).resolve()
        self.project_workspace = Path(project_workspace or workspace).resolve()
        self.workspace_guard = WorkspaceGuard(self.workspace)
        self.task_service = task_service
        self.data_paths = data_paths or RuntimeDataPaths(env.data_dir)
        self.memory_access = memory_access or MemoryAccessController(
            lambda project: bool(
                self.task_service.has_active_team_run_for_workspace(project)
            )
        )
        self._mcp_routers: dict[tuple[Path, Path, bool], McpRouter] = {}
        self._mcp_lock = threading.RLock()
        self._retired_mcp_routers: list[McpRouter] = []

    def create(
        self,
        *,
        event_emitter: EventEmitter,
        cancellation: CancellationToken,
        permission_broker: WaitingPermissionBroker,
        checkpoint: Any | None = None,
        team_session: AgentSessionRecord | None = None,
        team_attempt: TaskAttemptRecord | None = None,
        worktree_manager: WorktreeManager | None = None,
        root_prompt_mode: PromptMode | None = None,
    ) -> Agent:
        if team_attempt is not None and team_session is None:
            raise ValueError(
                "team_attempt requires its independent AgentSession"
            )
        if team_session is not None and worktree_manager is None:
            raise ValueError("Team AgentSession requires the Runtime Worktree registry")
        if team_attempt is not None and team_attempt.session_id != team_session.id:
            raise ValueError("Team Attempt does not belong to the supplied AgentSession")
        if team_session is not None and root_prompt_mode is not None:
            raise ValueError("root_prompt_mode is only valid for a Root Agent")
        root_mode = root_prompt_mode or PromptMode.NORMAL
        team_agent = None
        team = None
        if team_session is not None:
            team_agent = self.task_service.get_team_agent(team_session.agent_id)
            team = self.task_service.get_team_run(team_session.team_run_id)
            if team is None:
                raise ValueError("Team AgentSession TeamRun no longer exists")
        team_planner = (
            team_session is None and root_mode is PromptMode.TEAM_PLANNER
        ) or (
            team_agent is not None
            and team_agent.role is TeamAgentRole.LEAD
            and team.state is TeamRunState.PLANNING
        )
        self._import_legacy_context()
        state = RuntimeState()
        messages: list[dict[str, Any]] = []
        if checkpoint is not None:
            messages = [dict(item) for item in checkpoint.messages]
            if team_session is None and not team_planner:
                messages, repaired = reconcile_tool_history(
                    messages,
                    repair_missing=not getattr(checkpoint, "metadata", {}).get("tool_history_version"),
                )
                if repaired:
                    event_emitter.emit("history.repaired", {"tool_use_ids": repaired, "status": "unknown"})
            state = _restore_runtime_state(checkpoint.context)

        execution = event_emitter.context
        task_list = self.task_service.ensure_conversation_task_list(
            execution.conversation_id
        )

        def task_state() -> str:
            resources = self.task_service.list_task_resources(task_list.id)
            return json.dumps(
                [resource.to_dict(camel_case=True) for resource in resources],
                ensure_ascii=False,
                indent=2,
            )

        changed_files = set(state.files_changed)
        generation = team_session.generation if team_session is not None else None
        context_root = self.data_paths.context_dir(
            self.project_workspace,
            conversation_id=execution.conversation_id,
            agent_id=execution.agent_id,
            generation=generation,
        )
        context_config = replace(
            self.env.context_config,
            transcript_dir=context_root / "transcripts",
            tool_output_dir=context_root / "tool-results",
        )
        context = ContextManager(
            config=context_config,
            state=state,
            todo_store=None,
            task_state_provider=task_state,
        )
        skill_loader = self._skill_loader()
        memory_store = self._memory_store(
            always_read_only=team_session is not None or team_planner or root_mode is PromptMode.DISCUSS
        )
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

        def tools_for():
            registry = create_default_registry(
                ask_user_fn=(
                    WebUserQuestions(self.task_service, event_emitter, cancellation).ask
                    if team_session is None and not team_planner else None
                ),
                skill_loader=skill_loader,
                memory_store=memory_store,
                allow_memory_write=team_session is None and not team_planner,
                memory_max_items=self.env.memory_config.max_loaded_items,
                workspace_guard=self.workspace_guard,
                changed_files=changed_files,
                planning_backend=PlanningBackend.TASKS,
                task_service=self.task_service,
                task_list_id=task_list.id,
                conversation_id=execution.conversation_id,
                run_id=execution.run_id,
                agent_id=execution.agent_id,
            )
            registry.register(LoadToolOutputTool(context_config.tool_output_dir))
            registry.register(LoadContextHistoryTool(context_config.transcript_dir))
            self._mcp_router(
                force_workspace_cwd=team_session is not None
            ).register_tools(registry)
            if self.env.team_runtime_enabled and team_planner:
                registry.register(
                    LeadTeamPlanTool(
                        self.task_service,
                        WorktreeManagerRegistry(
                            self.task_service,
                            self.data_paths.worktree_root(self.env.team_worktree_root),
                        ),
                        execution.conversation_id,
                        team.root_run_id if team is not None else execution.run_id,
                        task_list.id,
                        self.memory_access,
                    )
                )
                if team is not None:
                    registry.register(
                        TeamStatusTool(
                            self.task_service,
                            team.id,
                            self.env.team_write_enabled,
                        )
                    )
            if team_planner:
                registry = _read_only_registry(
                    registry,
                    team.metadata.get("read_only_mcp_tools", ())
                    if team is not None
                    else (),
                )
                registry = registry.copy_without({"remember", "subagent"})
            return registry

        policy = PermissionPolicy(
            workspace=self.workspace,
            broker=permission_broker,
            cancellation=cancellation,
        )
        hooks = create_default_hooks(
            permission_policy=policy,
            workspace=self.workspace,
            log=lambda _message: None,
            planning_backend=PlanningBackend.TASKS,
            task_state_provider=(
                task_state if team_session is None and not team_planner else None
            ),
        )
        client = self.env.create_anthropic_client(
            stream=self.env.stream,
            event_emitter=event_emitter,
            usage_tracker=usage_tracker,
        )

        def subagent_environment():
            sub_todos = TodoStore()
            subagent_root = context_root / "subagents" / uuid4().hex
            subagent_context_config = replace(
                context_config,
                transcript_dir=subagent_root / "transcripts",
                tool_output_dir=subagent_root / "tool-results",
            )
            subagent_tools = create_default_registry(
                todo_store=sub_todos,
                skill_loader=skill_loader,
                memory_store=memory_store,
                allow_memory_write=self.env.memory_config.allow_subagent_write,
                memory_max_items=self.env.memory_config.max_loaded_items,
                workspace_guard=self.workspace_guard,
                changed_files=changed_files,
                planning_backend=PlanningBackend.TODO,
            )
            subagent_tools.register(
                LoadToolOutputTool(subagent_context_config.tool_output_dir)
            )
            subagent_tools.register(
                LoadContextHistoryTool(subagent_context_config.transcript_dir)
            )
            return (
                subagent_tools,
                create_default_hooks(
                    permission_policy=policy,
                    workspace=self.workspace,
                    todo_store=sub_todos,
                    log=lambda _message: None,
                    planning_backend=PlanningBackend.TODO,
                ),
                ContextManager(config=subagent_context_config, todo_store=sub_todos),
            )

        agent_config = self.env.to_agent_config(planning_backend=PlanningBackend.TASKS)
        if team_session is not None or team_planner:
            agent_config = replace(agent_config, loop_guard=None)
        agent = Agent(
            client=client,
            tools=tools_for(),
            config=agent_config,
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
            allow_subagents=team_session is None and not team_planner,
            prompt_mode=root_mode if team_session is None else None,
        )
        if checkpoint is not None and team_session is None and not team_planner:
            guard_state = getattr(checkpoint, "metadata", {}).get("execution_guard")
            if guard_state is not None:
                agent.restore_execution_state(guard_state)
        if team_planner and team_session is None:
            agent.tools = TeamPlannerToolExecutionGate(
                self.task_service,
                execution.conversation_id,
                task_list.id,
            ).wrap(agent.tools)
        elif self.env.team_runtime_enabled and team_session is None:
            agent.tools = ActiveTeamRootToolExecutionGate(
                self.task_service,
                execution.conversation_id,
            ).wrap(agent.tools)
        if team_session is not None:
            assert worktree_manager is not None
            agent.set_execution_activity(ExecutionActivity(
                cancellation,
                response_timeout=self.env.team_model_response_timeout,
                model_timeout=self.env.team_model_call_timeout,
            ))
            assert team_agent is not None
            assert team is not None
            registry = agent.tools.copy_without({"remember", "subagent"})
            if team_agent.role is TeamAgentRole.LEAD:
                if team_planner:
                    registry = _read_only_registry(
                        registry,
                        team.metadata.get("read_only_mcp_tools", ()),
                    )
                    gate = TeamPlannerToolExecutionGate(
                        self.task_service,
                        execution.conversation_id,
                        task_list.id,
                    )
                    agent.prompt_mode = PromptMode.TEAM_PLANNER
                else:
                    registry = registry.copy_without(
                        {"TaskCreate", "TaskUpdate", "TeamPlanSubmit"}
                    )
                    registry = _read_only_registry(
                        registry,
                        team.metadata.get("read_only_mcp_tools", ()),
                    )
                    for tool in create_lead_tools(
                        self.task_service,
                        worktree_manager,
                        team.id,
                        team.lead_agent_id,
                        allow_code=self.env.team_write_enabled,
                        yield_callback=agent.request_yield,
                    ):
                        registry.register(tool)
                    gate = ReadOnlyTeamToolExecutionGate(
                        self.task_service,
                        read_only_mcp_tools=team.metadata.get(
                            "read_only_mcp_tools", ()
                        ),
                        trace_id=execution.run_id,
                    )
                    agent.prompt_mode = PromptMode.TEAM_LEAD
            else:
                if team_attempt is None:
                    raise ValueError("Teammate Session requires an assigned Attempt")
                registry = registry.copy_without(
                    {"TaskCreate", "TaskUpdate", "TeamPlanSubmit"}
                )
                task = self.task_service.get_task_resource(
                    team_attempt.task_list_id, team_attempt.task_id
                )
                metadata = task.task.metadata
                task_kind = str(metadata.get("kind") or "analysis").lower()
                for tool in create_teammate_tools(
                    self.task_service,
                    worktree_manager if task_kind == "code" else None,
                    team_attempt,
                    task_kind=task_kind,
                    write_enabled=team_attempt.write_enabled,
                    yield_callback=agent.request_yield,
                ):
                    registry.register(tool)
                if task_kind == "code":
                    binding_record = self.task_service.get_attempt_worktree_binding(
                        team_attempt.id
                    )
                    if binding_record is None:
                        raise ValueError("Code Teammate has no Worktree binding")
                    binding = worktree_manager.validate_binding(binding_record.id)
                    if Path(binding.path).resolve() != self.workspace:
                        raise ValueError(
                            "Teammate factory workspace does not match Worktree binding"
                        )
                    approved_mcp = set(metadata.get("read_only_mcp_tools", ()))
                    if team_attempt.write_enabled:
                        approved_mcp.update(metadata.get("allowed_mcp_tools", ()))
                    registry = _team_mcp_registry(registry, approved_mcp)
                    if not team_attempt.write_enabled:
                        registry = registry.copy_without({"write_file", "edit_file"})
                    gate = TeamToolExecutionGate(
                        self.task_service,
                        worktree_manager,
                        team_attempt.id,
                        read_only_mcp_tools=metadata.get("read_only_mcp_tools", ()),
                        allowed_mcp_tools=metadata.get("allowed_mcp_tools", ()),
                        trace_id=execution.run_id,
                        pause_callback=agent.request_yield,
                    )
                    agent.prompt_mode = (
                        PromptMode.TEAMMATE_WORK
                        if team_attempt.write_enabled
                        else PromptMode.TEAMMATE_PLAN
                    )
                else:
                    if self.workspace != self.project_workspace:
                        raise ValueError(
                            "Analysis Teammate must use the read-only source workspace"
                        )
                    registry = _read_only_registry(
                        registry,
                        metadata.get("read_only_mcp_tools", ()),
                    )
                    gate = ReadOnlyTeamToolExecutionGate(
                        self.task_service,
                        attempt_id=team_attempt.id,
                        read_only_mcp_tools=metadata.get("read_only_mcp_tools", ()),
                        trace_id=execution.run_id,
                    )
                    agent.prompt_mode = PromptMode.TEAMMATE_ANALYSIS
            agent.tools = gate.wrap(registry)
            agent.allow_subagents = False
            agent.subagent_environment_factory = None
        agent.permission_broker = permission_broker
        if agent.execution_activity is not None:
            permission_broker.execution_activity = agent.execution_activity
        return agent

    def for_workspace(self, workspace: str | Path) -> "WebAgentFactory":
        """Return an isolated factory while reusing immutable environment config."""

        factory = type(self)(
            self.env,
            workspace,
            self.task_service,
            project_workspace=workspace,
            data_paths=self.data_paths,
            memory_access=self.memory_access,
        )
        factory._mcp_routers = self._mcp_routers
        factory._mcp_lock = self._mcp_lock
        factory._retired_mcp_routers = self._retired_mcp_routers
        return factory

    def for_team_workspace(
        self, workspace: str | Path, *, project_workspace: str | Path
    ) -> "WebAgentFactory":
        """Bind tools to a Worktree while retaining the original project identity."""

        factory = type(self)(
            self.env,
            workspace,
            self.task_service,
            project_workspace=project_workspace,
            data_paths=self.data_paths,
            memory_access=self.memory_access,
        )
        factory._mcp_routers = self._mcp_routers
        factory._mcp_lock = self._mcp_lock
        factory._retired_mcp_routers = self._retired_mcp_routers
        return factory

    def close(self) -> None:
        # Called only after root and Team workers have exited.
        with self._mcp_lock:
            for router in [*self._mcp_routers.values(), *self._retired_mcp_routers]:
                router.close()
            self._mcp_routers.clear()
            self._retired_mcp_routers.clear()

    def reload_mcp(self, workspace: str | Path) -> None:
        config_path = self._mcp_config_path(Path(workspace).resolve())
        with self._mcp_lock:
            for key in [item for item in self._mcp_routers if item[0] == config_path]:
                # Existing Agents keep their tools/connections until shutdown.
                self._retired_mcp_routers.append(self._mcp_routers.pop(key))

    def _mcp_router(self, *, force_workspace_cwd: bool = False) -> McpRouter:
        config_path = self._mcp_config_path(self.workspace)
        key = (config_path, self.workspace, force_workspace_cwd)
        with self._mcp_lock:
            router = self._mcp_routers.get(key)
            if router is None:
                router = McpRouter(
                    config_path,
                    workspace_root=self.workspace if force_workspace_cwd else None,
                    force_workspace_cwd=force_workspace_cwd,
                )
                self._mcp_routers[key] = router
            return router

    def _mcp_config_path(self, workspace: Path) -> Path:
        configured = self.env.mcp_config_path
        return configured if configured.is_absolute() else workspace / configured

    def _skill_loader(self) -> SkillLoader | None:
        if not self.env.enable_skills:
            return None
        roots = [
            self.data_paths.skill_root(root)
            for root in self.env.skill_roots
        ]
        return SkillLoader(roots=roots)

    def _memory_store(self, *, always_read_only: bool) -> MemoryStore | None:
        if not self.env.memory_config.enabled:
            return None
        root = self.data_paths.memory_dir(self.project_workspace)
        policy = self.memory_access.policy(
            self.project_workspace,
            always_read_only=always_read_only,
        )
        legacy = self.data_paths.legacy_path(
            self.project_workspace, self.env.memory_config.memory_dir
        )
        try:
            with policy.writing():
                self.data_paths.import_legacy_directory(legacy, root)
        except MemoryWriteBlocked:
            pass
        return MemoryStore(
            root=root,
            max_memory_bytes=self.env.memory_config.max_memory_bytes,
            access_policy=policy,
        )

    def _import_legacy_context(self) -> None:
        configured = self.env.context_config
        for name, legacy_value in (
            ("transcripts", configured.transcript_dir),
            ("tool-results", configured.tool_output_dir),
        ):
            legacy = self.data_paths.legacy_path(
                self.project_workspace, legacy_value
            )
            destination = self.data_paths.legacy_context_dir(
                self.project_workspace, name
            )
            self.data_paths.import_legacy_directory(legacy, destination)


def _read_only_registry(
    registry: ToolRegistry, read_only_mcp_tools: Iterable[str]
) -> ToolRegistry:
    return _team_mcp_registry(registry, read_only_mcp_tools).copy_without(
        {"write_file", "edit_file"}
    )


def _team_mcp_registry(
    registry: ToolRegistry, allowed_mcp_tools: Iterable[str]
) -> ToolRegistry:
    allowed_mcp = {str(name) for name in allowed_mcp_tools}
    blocked = {
        str(schema["name"])
        for schema in registry.schemas()
        if str(schema["name"]).startswith("mcp__")
        and str(schema["name"]) not in allowed_mcp
    }
    return registry.copy_without(blocked)


def serialize_runtime_state(state: RuntimeState) -> dict[str, Any]:
    return {item.name: getattr(state, item.name) for item in fields(RuntimeState)}


def _restore_runtime_state(payload: dict[str, Any]) -> RuntimeState:
    allowed = {item.name for item in fields(RuntimeState)}
    values = {key: value for key, value in dict(payload or {}).items() if key in allowed}
    return RuntimeState(**values)


__all__ = ["WebAgentFactory", "serialize_runtime_state"]
