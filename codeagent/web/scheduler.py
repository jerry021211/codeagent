"""Multi-workspace FIFO run scheduler for the local web application."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from codeagent.events import (
    EventEmitter,
    ExecutionContext,
    RecordingEventSink,
)
from codeagent.permissions import PermissionRequest, WaitingPermissionBroker
from codeagent.prompts import PromptMode
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import ModelCallTimeout
from codeagent.runtime.execution import is_execution_failure
from codeagent.teams import (
    AgentSessionRunner,
    AgentSessionState,
    MessageBus,
    TeamAgentRole,
)
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.models import ApprovalRecord, RunRecord
from codeagent.web.storage import (
    ACTIVE_RUN_STATUSES,
    RecordNotFoundError,
    SQLiteRepository,
    StorageConflictError,
)

logger = logging.getLogger(__name__)


class AgentFactory(Protocol):
    def for_workspace(self, workspace: str) -> "AgentFactory": ...

    def create(
        self,
        *,
        event_emitter: EventEmitter,
        cancellation: CancellationToken,
        permission_broker: WaitingPermissionBroker,
        checkpoint: Any | None = None,
        root_prompt_mode: PromptMode | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class _RunJob:
    run_id: str
    conversation_id: str
    workspace: str
    prompt: str
    use_team: bool
    mode: str
    emitter: EventEmitter
    cancellation: CancellationToken
    broker: WaitingPermissionBroker


class RunScheduler:
    """Run independent conversations in a bounded pool of FIFO workers."""

    def __init__(
        self,
        repository: SQLiteRepository,
        agent_factory: AgentFactory,
        *,
        approval_timeout: float | None = 600.0,
        max_concurrent_runs: int = 4,
    ) -> None:
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be a positive integer")
        self.repository = repository
        self.agent_factory = agent_factory
        self.approval_timeout = approval_timeout
        self.max_concurrent_runs = max_concurrent_runs
        self._jobs: queue.Queue[_RunJob | None] = queue.Queue()
        self._controls: dict[str, _RunJob] = {}
        self._team_approval_brokers: dict[str, WaitingPermissionBroker] = {}
        self._team_worktrees: Any | None = None
        self._lead_locks: dict[str, threading.Lock] = {}
        self._lead_activities: dict[str, ExecutionActivity] = {}
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._stop_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._stopping.is_set():
                raise StorageConflictError("Web runtime is shutting down")
            if self._threads:
                return
            for index in range(self.max_concurrent_runs):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"codeagent-run-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def stop(self, timeout: float | None = None) -> None:
        with self._stop_lock:
            if self._closed:
                return
            with self._lock:
                if not self._stopping.is_set():
                    self._stopping.set()
                    for job in self._controls.values():
                        job.cancellation.cancel("Web runtime is shutting down")
                    for _ in self._threads:
                        self._jobs.put(None)
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            for thread in self._threads:
                thread.join(timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in self._threads):
                # Workers still own the factory and repository. A later stop may retry.
                raise TimeoutError("Run workers have not stopped; runtime resources remain open")
            close_factory = getattr(self.agent_factory, "close", None)
            if callable(close_factory):
                close_factory()
            self._closed = True

    def submit(
        self, conversation_id: str, content: str, *, use_team: bool = False,
        mode: str = "normal",
    ) -> RunRecord:
        # Admission, queue order and shutdown share one short critical section.
        with self._lock:
            self.start()
            return self._submit(conversation_id, content, use_team=use_team, mode=mode)

    def _submit(
        self, conversation_id: str, content: str, *, use_team: bool,
        mode: str,
    ) -> RunRecord:
        if mode not in {"normal", "discuss"}:
            raise ValueError("Unknown execution mode")
        if mode == "discuss" and (
            use_team or self.repository.get_active_team_run_for_conversation(conversation_id)
        ):
            raise ValueError("Discuss mode cannot start or control a Team")
        prompt = str(content).strip()
        if not prompt:
            raise ValueError("Message content cannot be empty")
        conversation = self.repository.get_conversation(conversation_id)
        if conversation is None:
            raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
        if conversation.title in {"New conversation", "新会话", "新对话"}:
            self.repository.update_conversation(
                conversation_id,
                title=_conversation_title(prompt),
            )
        requested_mode = "team" if use_team else "discuss" if mode == "discuss" else "single"
        run = self.repository.create_run(
            conversation_id,
            metadata={
                "requested_mode": requested_mode,
                "agent_profile": (
                    PromptMode.TEAM_PLANNER.value
                    if use_team
                    else mode
                ),
            },
        )
        self.repository.create_message(
            conversation_id,
            role="user",
            content=prompt,
            run_id=run.id,
            metadata={"status": "complete", "mode": mode},
        )
        emitter = EventEmitter(
            RecordingEventSink(self.repository),
            context=ExecutionContext(
                conversation_id=conversation_id,
                run_id=run.id,
                turn_id=f"turn_{uuid4().hex}",
            ),
        )
        cancellation = CancellationToken()

        def on_permission(request: PermissionRequest) -> None:
            public_input = _public_approval_input(request.tool_name, request.tool_input)
            persisted = self.repository.create_approval(
                run.id,
                approval_id=request.id,
                tool_name=request.tool_name,
                tool_input=public_input,
                reason=request.reason,
                expires_at=_approval_expiry(request.timeout),
            )
            emitter.emit(
                "approval.requested",
                {
                    "approval_id": persisted.id,
                    "tool_name": persisted.tool_name,
                    "input": public_input,
                    "reason": persisted.reason,
                    "summary": _approval_summary(persisted.tool_name, persisted.tool_input),
                },
            )

        def on_permission_timeout(request: PermissionRequest) -> None:
            approval = self.repository.get_approval(request.id)
            if approval is not None and approval.status == "pending":
                self.repository.expire_pending_approvals(approval_id=request.id)
            emitter.emit(
                "approval.expired",
                {
                    "approval_id": request.id,
                    "tool_name": request.tool_name,
                    "reason": "approval timeout",
                },
            )

        broker = WaitingPermissionBroker(
            default_timeout=self.approval_timeout,
            on_request=on_permission,
            on_timeout=on_permission_timeout,
        )
        job = _RunJob(
            run_id=run.id,
            conversation_id=conversation_id,
            workspace=conversation.workspace,
            prompt=prompt,
            use_team=bool(use_team),
            mode=mode,
            emitter=emitter,
            cancellation=cancellation,
            broker=broker,
        )
        with self._lock:
            self._controls[run.id] = job
        emitter.emit("run.queued", {"status": "queued", "queue_position": run.queue_position})
        self._jobs.put(job)
        return self.repository.get_run(run.id) or run

    def reload_mcp(self, workspace: str) -> bool:
        reload_factory = getattr(self.agent_factory, "reload_mcp", None)
        if not callable(reload_factory):
            return False
        reload_factory(workspace)
        return True

    def configure_team_runtime(self, worktree_manager: Any) -> None:
        """Attach the Runtime-owned Worktree registry used by Team Agents."""

        self._team_worktrees = worktree_manager

    def cancel(self, run_id: str) -> RunRecord:
        with self._lock:
            run = self.repository.request_run_cancel(run_id)
            job = self._controls.get(run_id)
            if job is not None and run.status in {*ACTIVE_RUN_STATUSES, "cancelled"}:
                job.cancellation.cancel("Cancelled by user")
                event_type = "run.cancelled" if run.status == "cancelled" else "run.cancelling"
                job.emitter.emit(event_type, {"status": run.status})
                if run.status == "cancelled":
                    # The queue retains a harmless tombstone; replay need not wait
                    # for busy workers to dequeue an already cancelled Run.
                    self._release(run_id)
        return self.repository.get_run(run_id) or run

    def is_run_pending(self, run_id: str) -> bool:
        """Include final event persistence, not just the database Run status."""
        with self._lock:
            return run_id in self._controls

    def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
    ) -> ApprovalRecord:
        approval = self.repository.get_approval(approval_id)
        if approval is None or approval.run_id != run_id:
            raise RecordNotFoundError(f"Approval not found for run: {approval_id}")
        if approval.status != "pending":
            if approval.decision != decision:
                raise ValueError(
                    f"Approval already resolved as {approval.decision}: {approval_id}"
                )
            return approval
        resolved = self.repository.resolve_approval(approval_id, decision)
        with self._lock:
            job = self._controls.get(run_id)
        if job is not None:
            job.broker.resolve(approval_id, decision == "allow")
            job.emitter.emit(
                "approval.allowed" if decision == "allow" else "approval.denied",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "tool_name": resolved.tool_name,
                },
            )
        else:
            with self._lock:
                broker = self._team_approval_brokers.pop(approval_id, None)
            if broker is not None:
                broker.resolve(approval_id, decision == "allow")
        return resolved

    def create_team_agent(
        self,
        session: Any,
        attempt: Any,
        cancellation: CancellationToken,
        worktree_manager: Any,
    ) -> Any:
        """Build one independent Teammate Agent rooted at its bound Worktree."""

        team = self.repository.get_team_run(attempt.team_run_id)
        if team is None:
            raise RecordNotFoundError(f"TeamRun not found: {attempt.team_run_id}")
        conversation = self.repository.get_conversation(team.conversation_id)
        if conversation is None:
            raise RecordNotFoundError(
                f"Conversation not found: {team.conversation_id}"
            )
        task = self.repository.get_task_resource(attempt.task_list_id, attempt.task_id)
        task_kind = str(task.task.metadata.get("kind") or "analysis").lower()
        binding = self.repository.get_attempt_worktree_binding(attempt.id)
        if task_kind == "code":
            if binding is None:
                raise RuntimeError("Code Teammate has no Worktree binding")
            binding = worktree_manager.validate_binding(binding.id)
            execution_workspace = binding.path
        else:
            if binding is not None:
                raise RuntimeError("Analysis Teammate must not have a Worktree binding")
            execution_workspace = conversation.workspace
        emitter = EventEmitter(
            RecordingEventSink(self.repository),
            context=ExecutionContext(
                conversation_id=team.conversation_id,
                run_id=team.root_run_id,
                turn_id=f"teamturn_{uuid4().hex}",
                agent_id=attempt.agent_id,
                parent_agent_id=team.lead_agent_id,
            ),
        )

        def on_permission(request: PermissionRequest) -> None:
            public_input = _public_approval_input(request.tool_name, request.tool_input)
            persisted = self.repository.create_approval(
                team.root_run_id,
                approval_id=request.id,
                tool_name=request.tool_name,
                tool_input=public_input,
                reason=request.reason,
                expires_at=_approval_expiry(request.timeout),
            )
            with self._lock:
                self._team_approval_brokers[persisted.id] = broker
            emitter.emit(
                "approval.requested",
                {
                    "approval_id": persisted.id,
                    "tool_name": persisted.tool_name,
                    "input": public_input,
                    "reason": persisted.reason,
                    "summary": _approval_summary(
                        persisted.tool_name, persisted.tool_input
                    ),
                    "team_run_id": team.id,
                    "attempt_id": attempt.id,
                },
            )

        def on_timeout(request: PermissionRequest) -> None:
            with self._lock:
                self._team_approval_brokers.pop(request.id, None)
            approval = self.repository.get_approval(request.id)
            if approval is not None and approval.status == "pending":
                self.repository.expire_pending_approvals(approval_id=request.id)
            emitter.emit(
                "approval.expired",
                {
                    "approval_id": request.id,
                    "tool_name": request.tool_name,
                    "team_run_id": team.id,
                    "attempt_id": attempt.id,
                },
            )

        broker = WaitingPermissionBroker(
            default_timeout=self.approval_timeout,
            on_request=on_permission,
            on_timeout=on_timeout,
        )
        team_factory_method = getattr(self.agent_factory, "for_team_workspace", None)
        if callable(team_factory_method):
            workspace_factory = team_factory_method(
                execution_workspace,
                project_workspace=conversation.workspace,
            )
        else:
            factory_method = getattr(self.agent_factory, "for_workspace", None)
            workspace_factory = (
                factory_method(execution_workspace)
                if callable(factory_method)
                else self.agent_factory
            )
        return workspace_factory.create(
            event_emitter=emitter,
            cancellation=cancellation,
            permission_broker=broker,
            checkpoint=None,
            team_session=session,
            team_attempt=attempt,
            worktree_manager=worktree_manager,
        )

    def team_lead_activities(self) -> tuple[ExecutionActivity, ...]:
        """Expose active foreground Lead calls to the existing Team watchdog."""
        with self._lock:
            return tuple(self._lead_activities.values())

    def run_team_lead_cycle(
        self,
        team_run_id: str,
        *,
        run_id: str | None = None,
        cancellation: CancellationToken | None = None,
        execution_activity: ExecutionActivity | None = None,
    ) -> Any:
        """Run one serialized Root/Lead turn from its durable Team inbox."""

        if self._team_worktrees is None:
            raise RuntimeError("Team Worktree registry is not configured")
        team = self.repository.get_team_run(team_run_id)
        if team is None:
            raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
        conversation = self.repository.get_conversation(team.conversation_id)
        if conversation is None:
            raise RecordNotFoundError(f"Conversation not found: {team.conversation_id}")
        sessions = [
            session
            for session in self.repository.list_agent_sessions(
                team.id, role=TeamAgentRole.LEAD.value
            )
            if session.state
            not in {
                AgentSessionState.FAILED,
                AgentSessionState.LOST,
                AgentSessionState.SHUTDOWN,
            }
        ]
        if len(sessions) != 1:
            raise RuntimeError("TeamRun must have exactly one active Lead Session")
        session = sessions[0]
        with self._lock:
            lead_lock = self._lead_locks.setdefault(team.id, threading.Lock())
        with lead_lock:
            session = self.repository.get_agent_session(session.id)
            if session.state in {
                AgentSessionState.FAILED,
                AgentSessionState.LOST,
                AgentSessionState.SHUTDOWN,
            }:
                raise RuntimeError(f"Lead Session is terminal: {session.state.value}")
            if session.state is not AgentSessionState.WORK:
                session = self.repository.transition_agent_session(
                    session.id, AgentSessionState.WORK.value
                )
            all_pending = self.repository.fetch_unacked_team_messages(session.id)
            pending = [
                message
                for message in all_pending
                if message.type != "USER_INSTRUCTION"
                or (
                    run_id is not None
                    and str(message.payload.get("run_id") or "") == run_id
                )
            ]
            if not pending:
                self.repository.transition_agent_session(
                    session.id, AgentSessionState.IDLE.value
                )
                return None
            token = cancellation or CancellationToken()
            emitter = EventEmitter(
                RecordingEventSink(self.repository),
                context=ExecutionContext(
                    conversation_id=team.conversation_id,
                    run_id=run_id or team.root_run_id,
                    turn_id=f"leadturn_{uuid4().hex}",
                    agent_id=team.lead_agent_id,
                ),
            )
            factory_method = getattr(self.agent_factory, "for_workspace", None)
            workspace_factory = (
                factory_method(conversation.workspace)
                if callable(factory_method)
                else self.agent_factory
            )
            agent = workspace_factory.create(
                event_emitter=emitter,
                cancellation=token,
                permission_broker=WaitingPermissionBroker(
                    default_timeout=self.approval_timeout
                ),
                checkpoint=None,
                team_session=session,
                worktree_manager=self._team_worktrees,
            )
            activity = execution_activity or agent.execution_activity or ExecutionActivity(token)
            agent.set_execution_activity(activity)
            with self._lock:
                self._lead_activities[team.id] = activity
            try:
                result = AgentSessionRunner(self.repository).run(
                    agent, session.id,
                    included_message_ids=frozenset(message.id for message in pending),
                )
            except ModelCallTimeout as exc:
                self.repository.transition_agent_session(
                    session.id, AgentSessionState.WAITING.value,
                    waiting_reason=exc.reason_code,
                )
                raise
            finally:
                with self._lock:
                    self._lead_activities.pop(team.id, None)
            unresolved = _unresolved_lead_actions(self.repository, team.id, pending)
            if unresolved:
                reason = "Lead did not persist required decision(s): " + ", ".join(
                    unresolved
                )
                self.repository.transition_agent_session(
                    session.id,
                    AgentSessionState.WAITING.value,
                    waiting_reason="lead_decision_not_recorded",
                )
                result.stop_reason = "runtime_contract:lead_decision_not_recorded"
                result.final_text = "\n\n".join(
                    item for item in (result.final_text, f"Runtime: {reason}") if item
                )
            return result

    def _execute_team_lead(self, job: _RunJob, team: Any) -> None:
        lead_profile = (
            PromptMode.TEAM_PLANNER
            if getattr(team.state, "value", team.state) == "planning"
            else PromptMode.TEAM_LEAD
        )
        job.emitter.emit(
            "agent.profile.selected",
            {
                "profile": lead_profile.value,
                "requested_mode": "active_team",
                "team_run_id": team.id,
            },
        )
        sessions = [
            session
            for session in self.repository.list_agent_sessions(
                team.id, role=TeamAgentRole.LEAD.value
            )
            if session.state
            not in {
                AgentSessionState.FAILED,
                AgentSessionState.LOST,
                AgentSessionState.SHUTDOWN,
            }
        ]
        if len(sessions) != 1:
            raise RuntimeError("TeamRun must have exactly one active Lead Session")
        session = sessions[0]
        MessageBus(self.repository).send(
            team.id,
            sender_type="runtime",
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=session.generation,
            message_type="USER_INSTRUCTION",
            payload={"content": job.prompt, "run_id": job.run_id},
            dedupe_key=f"user-instruction:{job.run_id}",
            priority="control",
        )
        result = self.run_team_lead_cycle(
            team.id, run_id=job.run_id, cancellation=job.cancellation
        )
        if result is None:
            raise RuntimeError("Lead USER_INSTRUCTION was not available to its Session")
        job.cancellation.raise_if_cancelled()
        terminal_status = (
            "failed"
            if result.stop_reason.startswith(
                ("recovery_failed", "max_iterations", "runtime_contract")
            )
            else "completed"
        )
        if result.final_text:
            self.repository.create_message(
                job.conversation_id,
                role="assistant",
                content=result.final_text,
                run_id=job.run_id,
                metadata={
                    "status": "complete" if terminal_status == "completed" else "failed",
                    "team_run_id": team.id,
                    "agent_role": "lead",
                },
            )
            job.emitter.emit(
                "message.completed",
                {
                    "role": "assistant",
                    "content": result.final_text,
                    "chars": len(result.final_text),
                    "team_run_id": team.id,
                    "agent_role": "lead",
                },
            )
        current_run = self.repository.get_run(job.run_id)
        self.repository.update_run_status(
            job.run_id,
            terminal_status,
            error=result.final_text if terminal_status == "failed" else None,
            metadata={
                **(current_run.metadata if current_run is not None else {}),
                "stop_reason": result.stop_reason,
                "usage": result.usage.to_dict(),
                "team_run_id": team.id,
                "agent_role": "lead",
                "agent_profile": lead_profile.value,
            },
        )
        job.emitter.emit(
            "run.completed" if terminal_status == "completed" else "run.failed",
            {
                "status": terminal_status,
                "stop_reason": result.stop_reason,
                "usage": result.usage.to_dict(),
                "modified_files": [],
                "team_run_id": team.id,
                "agent_role": "lead",
            },
        )

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                if self._stopping.is_set():
                    current = self.repository.get_run(job.run_id)
                    if current is not None and current.status == "queued":
                        self.repository.update_run_status(
                            job.run_id,
                            "interrupted",
                            error={"message": "Web runtime stopped before execution"},
                        )
                        job.emitter.emit(
                            "run.interrupted",
                            {"status": "interrupted", "reason": "runtime shutdown"},
                        )
                else:
                    self._execute(job)
            except Exception as exc:
                # A failed checkpoint/event write must not permanently lose a worker.
                logger.exception("Run worker failed for %s", job.run_id if job else None)
                if job is not None:
                    try:
                        current = self.repository.get_run(job.run_id)
                        if current is not None and current.status in ACTIVE_RUN_STATUSES:
                            error = {"type": type(exc).__name__, "message": str(exc)}
                            self.repository.update_run_status(job.run_id, "failed", error=error)
                            job.emitter.emit("run.failed", {"status": "failed", "error": error})
                    except Exception:
                        logger.exception("Could not persist failed Run %s", job.run_id)
            finally:
                if job is not None:
                    self._release(job.run_id)
                self._jobs.task_done()

    def _execute(self, job: _RunJob) -> None:
        agent = None
        try:
            with self._lock:
                current = self.repository.get_run(job.run_id)
                if current is None or current.status not in ACTIVE_RUN_STATUSES:
                    return
                job.cancellation.raise_if_cancelled()
                self.repository.start_run(job.run_id)
                job.emitter.emit("run.started", {"status": "running"})
            active_team = self.repository.get_active_team_run_for_conversation(
                job.conversation_id
            )
            if active_team is not None:
                if job.mode == "discuss":
                    raise ValueError("A Team became active after this discussion was queued")
                self._execute_team_lead(job, active_team)
                return
            if not job.use_team:
                missing = self.repository.get_uncheckpointed_tool_run(job.conversation_id, exclude_run_id=job.run_id)
                if missing is not None:
                    raise RuntimeError(f"上次运行 {missing.id} 未能保存完整上下文，已暂停继续执行，以免重复产生副作用。请先恢复该运行的检查点，或核实实际状态后新建会话。")
            checkpoint = self.repository.get_latest_checkpoint(job.conversation_id)
            factory_method = getattr(self.agent_factory, "for_workspace", None)
            workspace_factory = (
                factory_method(job.workspace)
                if job.workspace and callable(factory_method)
                else self.agent_factory
            )
            profile = (
                PromptMode.TEAM_PLANNER
                if job.use_team
                else PromptMode(job.mode)
            )
            job.emitter.emit(
                "agent.profile.selected",
                {
                    "profile": profile.value,
                    "requested_mode": "team" if job.use_team else "discuss" if job.mode == "discuss" else "single",
                },
            )
            create_kwargs = {
                "event_emitter": job.emitter,
                "cancellation": job.cancellation,
                "permission_broker": job.broker,
                "checkpoint": checkpoint,
            }
            if job.use_team or job.mode == "discuss":
                create_kwargs["root_prompt_mode"] = profile
            agent = workspace_factory.create(**create_kwargs)
            if not job.use_team:
                with self._lock:
                    job.cancellation.raise_if_cancelled()
                    current = self.repository.get_run(job.run_id) or current
                    current = self.repository.update_run_status(
                        job.run_id, current.status,
                        metadata={**current.metadata, "tool_checkpoint_required": True},
                    )
            result = agent.run(job.prompt)
            job.cancellation.raise_if_cancelled()
            terminal_status = (
                "failed" if is_execution_failure(result.stop_reason)
                else "completed"
            )
            active_team = self.repository.get_active_team_run_for_conversation(
                job.conversation_id
            )
            contract_error = None
            if job.use_team and active_team is None:
                terminal_status = "failed"
                contract_error = (
                    "Explicit Team mode ended without submitting a Team Plan. "
                    "No TeamRun, Attempt, or Worktree was created."
                )
                job.emitter.emit(
                    "team.plan.not_submitted",
                    {"status": "failed", "reason": contract_error},
                )
            final_text = result.final_text
            if contract_error:
                final_text = "\n\n".join(
                    item for item in (result.final_text, f"Runtime: {contract_error}") if item
                )
            if final_text:
                self.repository.create_message(
                    job.conversation_id,
                    role="assistant",
                    content=final_text,
                    run_id=job.run_id,
                    metadata={"status": "complete" if terminal_status == "completed" else "failed"},
                )
                job.emitter.emit(
                    "message.completed",
                    {
                        "role": "assistant",
                        "content": final_text,
                        "chars": len(final_text),
                    },
                )
            self._finish(
                job,
                agent,
                terminal_status,
                error=(final_text if terminal_status == "failed" else None),
                metadata={
                    **current.metadata,
                    "stop_reason": result.stop_reason,
                    "usage": result.usage.to_dict(),
                    **(
                        {"team_run_id": active_team.id}
                        if active_team is not None
                        else {}
                    ),
                },
            )
            job.emitter.emit(
                "run.completed" if terminal_status == "completed" else "run.failed",
                {
                    "status": terminal_status,
                    "stop_reason": result.stop_reason,
                    "usage": result.usage.to_dict(),
                    "modified_files": list(agent.context.state.files_changed),
                },
            )
        except ModelCallTimeout as exc:
            error = {"type": exc.reason_code, "message": exc.reason}
            if agent is not None:
                self._finish(job, agent, "failed", error=error)
            else:
                self.repository.update_run_status(job.run_id, "failed", error=error)
            job.emitter.emit("run.failed", {"status": "failed", "error": error})
        except CancelledError as exc:
            latest = self.repository.get_run(job.run_id)
            if latest is not None and latest.status not in {"cancelled", "completed", "failed", "interrupted"}:
                if agent is not None:
                    self._finish(job, agent, "cancelled", error={"message": exc.reason})
                else:
                    self.repository.update_run_status(job.run_id, "cancelled", error={"message": exc.reason})
            job.emitter.emit("run.cancelled", {"status": "cancelled", "reason": exc.reason})
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            if agent is not None:
                self._finish(job, agent, "failed", error=error)
            else:
                self.repository.update_run_status(job.run_id, "failed", error=error)
            job.emitter.emit("run.failed", {"status": "failed", "error": error})

    def _finish(
        self,
        job: _RunJob,
        agent: Any,
        status: str,
        *,
        error: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from codeagent.messages import validate_tool_history

        validate_tool_history(agent.messages)
        checkpoint_metadata = {
            "tool_history_version": 1,
            "todo_revision": (
                agent.context.todo_store.revision if agent.context.todo_store else 0
            ),
        }
        export_state = getattr(agent, "export_execution_state", None)
        if not job.use_team and callable(export_state):
            checkpoint_metadata["execution_guard"] = export_state()
        self.repository.finish_run_with_checkpoint(
            job.run_id,
            status=status,
            messages=agent.messages,
            todos=agent.context.todo_store.todos if agent.context.todo_store else [],
            context=serialize_runtime_state(agent.context.state),
            error=error,
            metadata=metadata,
            checkpoint_metadata=checkpoint_metadata,
        )

    def _release(self, run_id: str) -> None:
        with self._lock:
            self._controls.pop(run_id, None)


def _approval_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "bash":
        return str(tool_input.get("command") or "执行命令")
    path = tool_input.get("file_path") or tool_input.get("path")
    return f"{tool_name}: {path}" if path else tool_name


def _unresolved_lead_actions(
    repository: Any, team_run_id: str, messages: list[Any]
) -> list[str]:
    unresolved: list[str] = []
    team_messages = None
    for message in messages:
        if message.type == "ATTEMPT_PLAN_SUBMITTED" and message.attempt_id:
            revision = int(message.payload.get("attempt_plan_revision") or 0)
            plans = repository.list_attempt_plans(message.attempt_id)
            plan = next((item for item in plans if item.revision == revision), None)
            if plan is not None and plan.status.value == "submitted":
                unresolved.append(f"Attempt {message.attempt_id} plan p{revision}")
        elif message.type == "CANDIDATE_SUBMITTED":
            candidate_id = str(message.payload.get("candidate_id") or "")
            if candidate_id:
                candidate = repository.get_candidate(candidate_id)
                if candidate.status.value == "submitted":
                    unresolved.append(f"Candidate {candidate_id}")
        elif message.type == "QUESTION":
            if team_messages is None:
                team_messages = repository.list_team_messages(team_run_id)
            if not any(
                item.type == "ANSWER" and item.correlation_id == message.id
                for item in team_messages
            ):
                unresolved.append(f"Question {message.id}")
    return unresolved


def _conversation_title(prompt: str, limit: int = 36) -> str:
    one_line = " ".join(prompt.split())
    return one_line if len(one_line) <= limit else one_line[:limit].rstrip() + "…"


def _public_approval_input(tool_name: str, value: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in {"write_file", "edit_file"}:
        return dict(value)
    public = dict(value)
    for key in ("content", "old_string", "new_string"):
        if key in public:
            public[f"{key}_chars"] = len(str(public.pop(key)))
    return public


def _approval_expiry(timeout: float | None) -> str | None:
    if timeout is None:
        return None
    return (datetime.now(UTC) + timedelta(seconds=max(0.0, timeout))).isoformat()


__all__ = ["AgentFactory", "RunScheduler"]
