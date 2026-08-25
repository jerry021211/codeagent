"""Multi-workspace FIFO run scheduler for the local web application."""

from __future__ import annotations

import queue
import threading
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
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.models import ApprovalRecord, RunRecord
from codeagent.web.storage import (
    ACTIVE_RUN_STATUSES,
    RecordNotFoundError,
    SQLiteRepository,
)


class AgentFactory(Protocol):
    def for_workspace(self, workspace: str) -> "AgentFactory": ...

    def create(
        self,
        *,
        event_emitter: EventEmitter,
        cancellation: CancellationToken,
        permission_broker: WaitingPermissionBroker,
        checkpoint: Any | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class _RunJob:
    run_id: str
    conversation_id: str
    workspace: str
    prompt: str
    emitter: EventEmitter
    cancellation: CancellationToken
    broker: WaitingPermissionBroker


class RunScheduler:
    """Execute one root task at a time and keep all state transitions durable."""

    def __init__(
        self,
        repository: SQLiteRepository,
        agent_factory: AgentFactory,
        *,
        approval_timeout: float | None = 600.0,
    ) -> None:
        self.repository = repository
        self.agent_factory = agent_factory
        self.approval_timeout = approval_timeout
        self._jobs: queue.Queue[_RunJob | None] = queue.Queue()
        self._controls: dict[str, _RunJob] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="codeagent-run-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stopping.set()
        with self._lock:
            for job in self._controls.values():
                job.cancellation.cancel("Web runtime is shutting down")
        self._jobs.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=None if timeout is None else max(0.0, timeout))
        with self._lock:
            remaining = list(self._controls.values())
        for job in remaining:
            current = self.repository.get_run(job.run_id)
            if current is not None and current.status in ACTIVE_RUN_STATUSES:
                try:
                    self.repository.update_run_status(
                        job.run_id,
                        "interrupted",
                        error={"message": "Web runtime stopped before execution completed"},
                    )
                    job.emitter.emit(
                        "run.interrupted",
                        {"status": "interrupted", "reason": "runtime shutdown"},
                    )
                except Exception:
                    pass
            self._release(job.run_id)
        close_factory = getattr(self.agent_factory, "close", None)
        if callable(close_factory):
            close_factory()

    def submit(self, conversation_id: str, content: str) -> RunRecord:
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
        run = self.repository.create_run(conversation_id)
        self.repository.create_message(
            conversation_id,
            role="user",
            content=prompt,
            run_id=run.id,
            metadata={"status": "complete"},
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
            emitter=emitter,
            cancellation=cancellation,
            broker=broker,
        )
        with self._lock:
            self._controls[run.id] = job
        emitter.emit("run.queued", {"status": "queued", "queue_position": run.queue_position})
        self._jobs.put(job)
        self.start()
        return self.repository.get_run(run.id) or run

    def reload_mcp(self, workspace: str) -> bool:
        with self._lock:
            if any(job.workspace == workspace for job in self._controls.values()):
                return False
        reload_factory = getattr(self.agent_factory, "reload_mcp", None)
        if not callable(reload_factory):
            return False
        reload_factory(workspace)
        return True

    def cancel(self, run_id: str) -> RunRecord:
        run = self.repository.request_run_cancel(run_id)
        with self._lock:
            job = self._controls.get(run_id)
        if job is not None:
            job.cancellation.cancel("Cancelled by user")
            event_type = "run.cancelled" if run.status == "cancelled" else "run.cancelling"
            job.emitter.emit(event_type, {"status": run.status})
        return self.repository.get_run(run_id) or run

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
        return resolved

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
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
                    self._release(job.run_id)
                else:
                    self._execute(job)
            finally:
                self._jobs.task_done()

    def _execute(self, job: _RunJob) -> None:
        current = self.repository.get_run(job.run_id)
        if current is None:
            return
        if current.status == "cancelled" or job.cancellation.is_cancelled:
            self._release(job.run_id)
            return

        agent = None
        try:
            self.repository.start_run(job.run_id)
            job.emitter.emit("run.started", {"status": "running"})
            checkpoint = self.repository.get_latest_checkpoint(job.conversation_id)
            factory_method = getattr(self.agent_factory, "for_workspace", None)
            workspace_factory = (
                factory_method(job.workspace)
                if job.workspace and callable(factory_method)
                else self.agent_factory
            )
            agent = workspace_factory.create(
                event_emitter=job.emitter,
                cancellation=job.cancellation,
                permission_broker=job.broker,
                checkpoint=checkpoint,
            )
            result = agent.run(job.prompt)
            job.cancellation.raise_if_cancelled()
            terminal_status = (
                "failed" if result.stop_reason.startswith(("recovery_failed", "max_iterations"))
                else "completed"
            )
            if result.final_text:
                self.repository.create_message(
                    job.conversation_id,
                    role="assistant",
                    content=result.final_text,
                    run_id=job.run_id,
                    metadata={"status": "complete" if terminal_status == "completed" else "failed"},
                )
                job.emitter.emit(
                    "message.completed",
                    {
                        "role": "assistant",
                        "content": result.final_text,
                        "chars": len(result.final_text),
                    },
                )
            self._finish(
                job,
                agent,
                terminal_status,
                error=(result.final_text if terminal_status == "failed" else None),
                metadata={"stop_reason": result.stop_reason, "usage": result.usage.to_dict()},
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
        finally:
            self._release(job.run_id)

    def _finish(
        self,
        job: _RunJob,
        agent: Any,
        status: str,
        *,
        error: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.repository.finish_run_with_checkpoint(
            job.run_id,
            status=status,
            messages=agent.messages,
            todos=agent.context.todo_store.todos if agent.context.todo_store else [],
            context=serialize_runtime_state(agent.context.state),
            error=error,
            metadata=metadata,
            checkpoint_metadata={
                "todo_revision": (
                    agent.context.todo_store.revision if agent.context.todo_store else 0
                )
            },
        )

    def _release(self, run_id: str) -> None:
        with self._lock:
            self._controls.pop(run_id, None)


def _approval_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "bash":
        return str(tool_input.get("command") or "执行命令")
    path = tool_input.get("file_path") or tool_input.get("path")
    return f"{tool_name}: {path}" if path else tool_name


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
