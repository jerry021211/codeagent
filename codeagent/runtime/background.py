"""Bounded single-process supervisor for Agent Team sessions."""

from __future__ import annotations

import os
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from uuid import uuid4

from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.cancellation import ModelCallTimeout
from codeagent.runtime.activity import ExecutionActivity, MODEL_TIMEOUT_REASONS
from codeagent.teams import (
    AgentSessionRunner,
    AgentSessionState,
    CandidateService,
    CandidateStatus,
    TaskAttemptRecord,
    TeamAgentRole,
    TeamRunState,
)
from codeagent.worktrees import WorktreeError, WorktreeManager
from codeagent.teams.tasks import validate_task_execution


class TeamAgentBuilder(Protocol):
    def __call__(
        self,
        session: Any,
        attempt: TaskAttemptRecord,
        cancellation: CancellationToken,
    ) -> Any: ...


@dataclass(slots=True)
class _WorkerJob:
    attempt_id: str
    session_id: str
    cancellation: CancellationToken
    future: Future[Any]
    activity: ExecutionActivity
    cancel_requested: bool = False
    suspect: bool = False


@dataclass(slots=True)
class _LeadJob:
    team_run_id: str
    cancellation: CancellationToken
    future: Future[Any]
    activity: ExecutionActivity


@dataclass(slots=True)
class _ValidationJob:
    candidate_id: str
    future: Future[Any]


class TeamSupervisor:
    """Schedule ready Team Tasks onto a bounded local thread pool.

    The supervisor never treats lease expiry as proof that a Python thread has
    stopped.  Cancellation and SUSPECT status revoke permissions immediately,
    while resource release happens only when the Future is done.
    """

    def __init__(
        self,
        repository: Any,
        agent_builder: TeamAgentBuilder,
        *,
        enabled: bool = False,
        write_enabled: bool = False,
        max_workers: int = 4,
        poll_interval: float = 0.25,
        heartbeat_timeout: float = 120.0,
        model_response_timeout: float = 300.0,
        model_call_timeout: float = 600.0,
        max_attempts_per_task: int = 2,
        worktree_manager: WorktreeManager | None = None,
        lead_runner: Any | None = None,
        lead_activity_provider: Callable[[], tuple[ExecutionActivity, ...]] | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_attempts_per_task < 1:
            raise ValueError("max_attempts_per_task must be at least 1")
        if any(
            not math.isfinite(value) or value <= 0
            for value in (model_response_timeout, model_call_timeout)
        ):
            raise ValueError("Model timeouts must be positive")
        self.repository = repository
        self.agent_builder = agent_builder
        self.enabled = bool(enabled)
        self.write_enabled = bool(write_enabled and enabled)
        self.max_workers = max_workers
        self.poll_interval = max(0.01, float(poll_interval))
        self.heartbeat_timeout = max(0.01, float(heartbeat_timeout))
        self.model_response_timeout = model_response_timeout
        self.model_call_timeout = model_call_timeout
        self.max_attempts_per_task = max_attempts_per_task
        self.worktree_manager = worktree_manager
        self.lead_runner = lead_runner
        self.lead_activity_provider = lead_activity_provider
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="codeagent-team",
        )
        self._jobs: dict[str, _WorkerJob] = {}
        self._lead_jobs: dict[str, _LeadJob] = {}
        self._validation_jobs: dict[str, _ValidationJob] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._control_loop,
                name="codeagent-team-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stopping.set()
        with self._lock:
            jobs = list(self._jobs.values())
            lead_jobs = list(self._lead_jobs.values())
        for job in jobs:
            try:
                self.repository.request_attempt_cancel(
                    job.attempt_id,
                    requested_by="runtime",
                    reason="Team runtime is shutting down",
                    command_id=f"shutdown:{job.attempt_id}:{uuid4().hex}",
                )
            except Exception:
                pass
            job.cancel_requested = True
            job.cancellation.cancel("Team runtime is shutting down")
            job.activity.interrupt_request()
        for job in lead_jobs:
            job.cancellation.cancel("Team runtime is shutting down")
            job.activity.interrupt_request()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=None if timeout is None else max(0.0, timeout))
        self._reap_finished()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def dispatch_once(self, team_run_id: str | None = None) -> int:
        """Reap workers and atomically assign as many currently ready Tasks as fit."""

        if not self.enabled or self._stopping.is_set():
            return 0
        self._reap_finished()
        self._mark_stale_workers()
        self._schedule_candidate_validations()
        self._schedule_lead_wakes()
        with self._lock:
            available_slots = self.max_workers - (
                len(self._jobs) + len(self._lead_jobs) + len(self._validation_jobs)
            )
        if available_slots <= 0:
            return 0
        teams = (
            [self.repository.get_team_run(team_run_id)]
            if team_run_id is not None
            else self.repository.list_team_runs(state=TeamRunState.RUNNING.value)
        )
        scheduled = 0
        for team in teams:
            if team is None or team.state is not TeamRunState.RUNNING:
                continue
            self._ensure_planned_teammates(team)
            resumed = self._resume_existing_attempts(team.id, available_slots - scheduled)
            scheduled += resumed
            if scheduled >= available_slots:
                return scheduled
            decisions = {
                item.task_id: item
                for item in self.repository.list_task_scheduling(
                    team.id,
                    allow_code=self.write_enabled and self.worktree_manager is not None,
                )
            }
            idle_sessions = self.repository.list_agent_sessions(
                team.id,
                state=AgentSessionState.IDLE.value,
                role=TeamAgentRole.TEAMMATE.value,
            )
            active_team_attempts = sum(
                1
                for item in self.repository.list_task_attempts(team.id)
                if item.state.value
                not in {"succeeded", "failed", "cancelled", "orphaned"}
            )
            idle_sessions = idle_sessions[
                : max(0, team.max_teammates - active_team_attempts)
            ]
            tasks = self.repository.list_task_resources(team.task_list_id)
            for task, session in zip(
                [item for item in tasks if decisions[item.task.id].schedulable],
                idle_sessions,
            ):
                if scheduled >= available_slots:
                    return scheduled
                previous = self.repository.list_task_attempts(
                    team.id, task_id=task.task.id
                )
                if len(previous) >= self.max_attempts_per_task:
                    continue
                metadata = task.task.metadata
                is_code = str(metadata.get("kind") or "analysis").lower() == "code"
                if is_code:
                    assert self.worktree_manager is not None
                    try:
                        self.worktree_manager.ensure_baseline_ready(team.id)
                    except WorktreeError:
                        continue
                try:
                    attempt = self.repository.claim_task_attempt(
                        team.id,
                        task_id=task.task.id,
                        agent_id=session.agent_id,
                        session_id=session.id,
                        expected_task_revision=task.revision,
                        command_id=f"dispatch:{team.id}:{task.task.id}:{uuid4().hex}",
                    )
                except Exception:
                    continue
                if is_code:
                    try:
                        self.worktree_manager.create_for_attempt(attempt)
                        attempt = self.repository.get_task_attempt(attempt.id)
                    except Exception as exc:
                        self.repository.fail_attempt_after_worker_exit(
                            attempt.id,
                            error={
                                "type": type(exc).__name__,
                                "message": f"Worktree setup failed: {exc}",
                            },
                        )
                        continue
                cancellation = CancellationToken()
                activity = self._new_activity(cancellation)
                future = self._executor.submit(
                    self._run_worker,
                    session.id,
                    attempt,
                    cancellation,
                    activity,
                )
                with self._lock:
                    self._jobs[attempt.id] = _WorkerJob(
                        attempt_id=attempt.id,
                        session_id=session.id,
                        cancellation=cancellation,
                        activity=activity,
                        future=future,
                    )
                scheduled += 1
        return scheduled

    def _ensure_planned_teammates(self, team: Any) -> None:
        """Create the approved plan's workers lazily and idempotently."""

        if team.active_plan_revision is None:
            return
        plan = self.repository.get_team_plan_revision(
            team.id, team.active_plan_revision
        ).plan
        raw_count = plan.get("teammate_count")
        if raw_count is None:
            return
        count = int(raw_count)
        if not 1 <= count <= team.max_teammates:
            raise ValueError("Approved Team Plan has an invalid teammate_count")

        agents = self.repository.list_team_agents(team.id)
        teammates = [
            item for item in agents if item.role is TeamAgentRole.TEAMMATE
        ]
        for index in range(len(teammates), count):
            teammates.append(
                self.repository.create_team_agent(
                    team.id,
                    name=f"Teammate {index + 1}",
                    agent_id=f"agent_{team.id}_{index + 1}",
                )
            )

        active_session_agents = {
            item.agent_id
            for item in self.repository.list_agent_sessions(
                team.id, role=TeamAgentRole.TEAMMATE.value
            )
            if item.state
            not in {
                AgentSessionState.FAILED,
                AgentSessionState.LOST,
                AgentSessionState.SHUTDOWN,
            }
        }
        for teammate in teammates[:count]:
            if teammate.id not in active_session_agents:
                self.repository.create_agent_session(team.id, teammate.id)

    def _resume_existing_attempts(self, team_run_id: str, limit: int) -> int:
        """Resume paused workers without creating Attempts or reacquiring leases."""

        if limit <= 0:
            return 0
        resumable_states = {"plan_required", "running", "review_rejected"}
        with self._lock:
            active_ids = set(self._jobs)
        resumed = 0
        for session in self.repository.list_agent_sessions(
            team_run_id,
            state=AgentSessionState.WORK.value,
            role=TeamAgentRole.TEAMMATE.value,
        ):
            if resumed >= limit or not session.current_attempt_id:
                break
            attempt = self.repository.get_task_attempt(session.current_attempt_id)
            if attempt.id in active_ids or attempt.state.value not in resumable_states:
                continue
            if attempt.cancel_requested_at is not None or attempt.result_unknown:
                continue
            task = self.repository.get_task_resource(
                attempt.task_list_id, attempt.task_id
            )
            is_code = str(task.task.metadata.get("kind") or "analysis").lower() == "code"
            if is_code:
                if not self.write_enabled or self.worktree_manager is None:
                    continue
                binding = self.repository.get_attempt_worktree_binding(attempt.id)
                if binding is None:
                    continue
                try:
                    self.worktree_manager.validate_binding(binding.id)
                except WorktreeError:
                    continue
            cancellation = CancellationToken()
            activity = self._new_activity(cancellation)
            future = self._executor.submit(
                self._run_worker,
                session.id,
                attempt,
                cancellation,
                activity,
            )
            with self._lock:
                self._jobs[attempt.id] = _WorkerJob(
                    attempt_id=attempt.id,
                    session_id=session.id,
                    cancellation=cancellation,
                    activity=activity,
                    future=future,
                )
            active_ids.add(attempt.id)
            resumed += 1
        return resumed

    def cancel_attempt(
        self,
        attempt_id: str,
        *,
        requested_by: str = "user",
        reason: str = "Cancelled by user",
        command_id: str | None = None,
    ) -> TaskAttemptRecord:
        attempt = self.repository.request_attempt_cancel(
            attempt_id,
            requested_by=requested_by,
            reason=reason,
            command_id=command_id or f"cancel:{attempt_id}:{uuid4().hex}",
        )
        with self._lock:
            job = self._jobs.get(attempt_id)
        if job is not None:
            job.cancel_requested = True
            job.cancellation.cancel(reason)
            job.activity.interrupt_request()
        elif attempt.state.value not in {"cancelled", "failed", "orphaned", "succeeded"}:
            attempt = self.repository.finalize_cancelled_attempt(
                attempt_id,
                reason=reason,
            )
        return attempt

    def active_attempt_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._jobs)

    def resume_attempt(
        self,
        attempt_id: str,
        *,
        resumed_by: str,
        reason: str,
        command_id: str,
        acknowledge_unknown_result: bool = False,
    ) -> TaskAttemptRecord:
        """Recheck a paused Attempt and reopen it without replaying any tool."""

        attempt = self.repository.get_task_attempt(attempt_id)
        with self._lock:
            job = self._jobs.get(attempt_id)
            if job is not None and not job.future.done():
                if self.repository.team_command_recorded(
                    attempt.team_run_id, command_id, "resume_task_attempt"
                ):
                    return attempt
                raise WorktreeError("Attempt worker is still running")
        if attempt.result_unknown and not acknowledge_unknown_result:
            raise WorktreeError(
                "Unknown write result must be acknowledged before recovery"
            )
        session = self.repository.get_agent_session(attempt.session_id)
        replace_session = session.state is AgentSessionState.LOST
        if replace_session:
            if str((attempt.error or {}).get("type") or "") != "service_restart":
                raise WorktreeError(
                    "Only a service-restart Session can be replaced during recovery"
                )
        task = self.repository.get_task_resource(
            attempt.task_list_id, attempt.task_id
        )
        is_code = str(task.task.metadata.get("kind") or "analysis").lower() == "code"
        fingerprint = None
        binding = None
        if is_code:
            if self.worktree_manager is None:
                raise WorktreeError("Team Worktree runtime is unavailable")
            binding = self.repository.get_attempt_worktree_binding(attempt_id)
            if binding is None:
                raise WorktreeError("Code Attempt has no Worktree binding")
            binding = self.worktree_manager.validate_recoverable_binding(binding.id)
            changed_paths = self.worktree_manager.recovery_changed_paths(binding.id)
            outside = [
                path
                for path in changed_paths
                if not _path_allowed_for_recovery(
                    path,
                    binding.write_scopes,
                    repository_lease=_has_repository_lease(
                        self.repository, attempt.team_run_id, attempt.id
                    ),
                )
            ]
            if outside:
                raise WorktreeError(
                    "Worktree still contains changes outside the declared write "
                    "scope: " + ", ".join(outside)
                )
            fingerprint = binding.fingerprint
        replacement_session = None
        replacement_fingerprint = None
        if replace_session:
            replacement_session = self.repository.create_agent_session(
                attempt.team_run_id,
                attempt.agent_id,
            )
            if replacement_session.state is not AgentSessionState.IDLE:
                raise WorktreeError("Replacement Teammate Session is not idle")
            if binding is not None:
                assert self.worktree_manager is not None
                replacement_fingerprint = self.worktree_manager.recovery_fingerprint(
                    binding.id,
                    replacement_session.generation,
                )
        return self.repository.resume_task_attempt(
            attempt_id,
            resumed_by=resumed_by,
            reason=reason,
            command_id=command_id,
            validated_worktree_fingerprint=fingerprint,
            acknowledge_unknown_result=acknowledge_unknown_result,
            replacement_session_id=(
                replacement_session.id if replacement_session is not None else None
            ),
            replacement_worktree_fingerprint=replacement_fingerprint,
        )

    def _control_loop(self) -> None:
        while not self._stopping.wait(self.poll_interval):
            self.dispatch_once()
        self._reap_finished()

    def _run_worker(
        self,
        session_id: str,
        attempt: TaskAttemptRecord,
        cancellation: CancellationToken,
        activity: ExecutionActivity,
    ) -> Any:
        session = self.repository.get_agent_session(session_id)
        agent = self.agent_builder(session, attempt, cancellation)

        agent.set_execution_activity(activity)
        return AgentSessionRunner(self.repository).run(agent, session_id)

    def _new_activity(self, cancellation: CancellationToken) -> ExecutionActivity:
        return ExecutionActivity(
            cancellation, response_timeout=self.model_response_timeout,
            model_timeout=self.model_call_timeout,
        )

    def _reap_finished(self) -> None:
        with self._lock:
            finished = [job for job in self._jobs.values() if job.future.done()]
        for job in finished:
            try:
                result = job.future.result()
            except ModelCallTimeout as exc:
                self.repository.pause_attempt_after_worker_exit(
                    job.attempt_id, reason=exc.reason_code,
                )
            except CancelledError as exc:
                if job.suspect:
                    self.repository.orphan_attempt_after_worker_exit(
                        job.attempt_id,
                        reason=exc.reason,
                    )
                elif job.cancel_requested:
                    self.repository.finalize_cancelled_attempt(
                        job.attempt_id,
                        reason=exc.reason,
                    )
                else:
                    self.repository.fail_attempt_after_worker_exit(
                        job.attempt_id,
                        error={"type": type(exc).__name__, "message": exc.reason},
                    )
            except Exception as exc:
                if job.suspect:
                    self.repository.orphan_attempt_after_worker_exit(
                        job.attempt_id,
                        reason=str(exc) or "Suspect worker exited",
                    )
                elif job.cancel_requested:
                    self.repository.finalize_cancelled_attempt(
                        job.attempt_id,
                        reason=str(exc) or "Cancelled worker exited",
                    )
                else:
                    self.repository.fail_attempt_after_worker_exit(
                        job.attempt_id,
                        error={"type": type(exc).__name__, "message": str(exc)},
                    )
            else:
                if job.suspect:
                    self.repository.orphan_attempt_after_worker_exit(
                        job.attempt_id,
                        reason="Suspect worker exited after cancellation",
                    )
                elif job.cancel_requested:
                    self.repository.finalize_cancelled_attempt(
                        job.attempt_id,
                        reason="Cancelled worker exited",
                    )
                elif not result.yielded:
                    attempt = self.repository.get_task_attempt(job.attempt_id)
                    if attempt.state.value not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "orphaned",
                    }:
                        if result.stop_reason.startswith("max_iterations:"):
                            self.repository.pause_attempt_after_worker_exit(
                                job.attempt_id,
                                reason="agent_iteration_limit",
                            )
                        elif result.stop_reason.startswith("recovery_failed:"):
                            self.repository.pause_attempt_after_worker_exit(
                                job.attempt_id,
                                reason="agent_runtime_failed",
                            )
                        elif not self._request_protocol_correction(attempt):
                            self.repository.pause_attempt_after_worker_exit(
                                job.attempt_id,
                                reason="protocol_incomplete",
                            )
            with self._lock:
                self._jobs.pop(job.attempt_id, None)
        with self._lock:
            lead_finished = [
                job for job in self._lead_jobs.values() if job.future.done()
            ]
            validation_finished = [
                job for job in self._validation_jobs.values() if job.future.done()
            ]
        for job in lead_finished:
            try:
                job.future.result()
            except ModelCallTimeout as exc:
                for session in self.repository.list_agent_sessions(
                    job.team_run_id, role=TeamAgentRole.LEAD.value,
                ):
                    if session.state not in {
                        AgentSessionState.LOST, AgentSessionState.FAILED,
                        AgentSessionState.SHUTDOWN,
                    } and not (
                        session.state is AgentSessionState.WAITING
                        and session.waiting_reason == exc.reason_code
                    ):
                        self.repository.transition_agent_session(
                            session.id, AgentSessionState.WAITING.value,
                            waiting_reason=exc.reason_code,
                        )
            except CancelledError:
                sessions = [
                    session
                    for session in self.repository.list_agent_sessions(
                        job.team_run_id, role=TeamAgentRole.LEAD.value
                    )
                    if session.state
                    not in {
                        AgentSessionState.FAILED,
                        AgentSessionState.LOST,
                        AgentSessionState.SHUTDOWN,
                    }
                ]
                if len(sessions) == 1:
                    self.repository.transition_agent_session(
                        sessions[0].id,
                        AgentSessionState.IDLE.value,
                    )
            except Exception as exc:
                sessions = [
                    session
                    for session in self.repository.list_agent_sessions(
                        job.team_run_id, role=TeamAgentRole.LEAD.value
                    )
                    if session.state
                    not in {
                        AgentSessionState.FAILED,
                        AgentSessionState.LOST,
                        AgentSessionState.SHUTDOWN,
                    }
                ]
                if len(sessions) == 1:
                    self.repository.transition_agent_session(
                        sessions[0].id,
                        AgentSessionState.FAILED.value,
                        failure={"type": type(exc).__name__, "message": str(exc)},
                    )
            finally:
                with self._lock:
                    self._lead_jobs.pop(job.team_run_id, None)
        for job in validation_finished:
            try:
                job.future.result()
            except Exception as exc:
                candidate = self.repository.get_candidate(job.candidate_id)
                if candidate.status is not CandidateStatus.COMMITTED:
                    self.repository.mark_candidate_commit_unknown(
                        candidate.id,
                        reason=f"Runtime validation outcome requires inspection: {exc}",
                    )
            finally:
                with self._lock:
                    self._validation_jobs.pop(job.candidate_id, None)

    def _request_protocol_correction(self, attempt: TaskAttemptRecord) -> bool:
        """Ask once for the missing Team terminal tool, durably and idempotently."""

        dedupe_key = f"protocol-correction:{attempt.id}:1"
        if any(
            message.dedupe_key == dedupe_key
            for message in self.repository.list_team_messages(attempt.team_run_id)
        ):
            return False
        session = self.repository.get_agent_session(attempt.session_id)
        task = self.repository.get_task_resource(
            attempt.task_list_id, attempt.task_id
        )
        task_kind = str(task.task.metadata.get("kind") or "analysis").lower()
        try:
            validate_task_execution(task.task.metadata)
        except ValueError:
            return False
        # Do not prescribe a terminal tool from a different role or permission mode.
        # Legacy contradictory Attempts stay paused; a reminder cannot repair them.
        if attempt.result_unknown:
            return False
        if (
            task_kind == "analysis" and attempt.state.value == "running"
            and not attempt.write_enabled
        ):
            required_action = "Call team_submit_analysis_result with the final result."
        elif (
            task_kind == "code" and attempt.state.value == "plan_required"
            and not attempt.write_enabled
        ):
            required_action = "Call team_submit_attempt_plan, then wait for review."
        elif (
            task_kind == "code" and attempt.state.value in {"running", "review_rejected"}
            and attempt.write_enabled
        ):
            required_action = (
                "Inspect the current Worktree, run the required tests, and call "
                "team_submit_candidate."
            )
        else:
            return False
        self.repository.send_team_message(
            attempt.team_run_id,
            sender_type="runtime",
            recipient_type="teammate",
            recipient_agent_id=attempt.agent_id,
            recipient_generation=session.generation,
            message_type="SYSTEM_ERROR",
            payload={
                "reason_code": "protocol_incomplete",
                "reason": required_action,
                "effective_scope": "attempt",
            },
            dedupe_key=dedupe_key,
            task_id=attempt.task_id,
            attempt_id=attempt.id,
            priority="control",
        )
        return True

    def _schedule_lead_wakes(self) -> None:
        if self.lead_runner is None:
            return
        for team in self.repository.list_team_runs():
            with self._lock:
                if (
                    len(self._jobs)
                    + len(self._lead_jobs)
                    + len(self._validation_jobs)
                    >= self.max_workers
                ):
                    return
                if team.id in self._lead_jobs:
                    continue
            active_leads = [
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
            if len(active_leads) != 1:
                continue
            if active_leads[0].waiting_reason in MODEL_TIMEOUT_REASONS:
                # A new user instruction may resume the Lead; inbox polling may not.
                continue
            messages = self.repository.list_team_messages(
                team.id, recipient_agent_id=team.lead_agent_id
            )
            wakeable = []
            active_user_instruction = False
            for message in messages:
                if message.acked_at is not None or message.type == "PROGRESS":
                    continue
                if message.type == "USER_INSTRUCTION":
                    run = self.repository.get_run(str(message.payload.get("run_id") or ""))
                    if run is not None and run.status in {"queued", "running"}:
                        active_user_instruction = True
                    continue
                wakeable.append(message)
            if active_user_instruction or not wakeable:
                continue
            cancellation = CancellationToken()
            activity = self._new_activity(cancellation)
            future = self._executor.submit(
                self.lead_runner,
                team.id,
                cancellation=cancellation,
                execution_activity=activity,
            )
            with self._lock:
                self._lead_jobs[team.id] = _LeadJob(
                    team.id, cancellation, future, activity
                )

    def _schedule_candidate_validations(self) -> None:
        if self.worktree_manager is None:
            return
        for team in self.repository.list_team_runs(state=TeamRunState.RUNNING.value):
            for candidate in self.repository.list_candidates(team.id):
                if candidate.status is not CandidateStatus.ACCEPTED:
                    continue
                if (
                    candidate.user_approval_required
                    and candidate.user_decision != "approved"
                ):
                    continue
                with self._lock:
                    if (
                        len(self._jobs)
                        + len(self._lead_jobs)
                        + len(self._validation_jobs)
                        >= self.max_workers
                    ):
                        return
                    if candidate.id in self._validation_jobs:
                        continue
                    future = self._executor.submit(
                        CandidateService(
                            self.repository, self.worktree_manager
                        ).validate_and_commit,
                        candidate.id,
                        trace_id=f"runtime:{team.id}",
                    )
                    self._validation_jobs[candidate.id] = _ValidationJob(
                        candidate.id, future
                    )

    def _mark_stale_workers(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            lead_jobs = list(self._lead_jobs.values())
        for job in jobs:
            if job.future.done() or job.suspect:
                continue
            if job.cancellation.is_cancelled:
                job.activity.interrupt_request()
                continue
            reason = job.activity.timeout_reason(self.heartbeat_timeout)
            if reason is None:
                continue
            self.repository.mark_attempt_suspect(
                job.attempt_id,
                reason=reason,
            )
            job.suspect = reason not in MODEL_TIMEOUT_REASONS
            job.cancellation.cancel(reason, reason_code=reason)
            job.activity.interrupt_request()
        for job in lead_jobs:
            if job.future.done():
                continue
            reason = job.activity.timeout_reason()
            if reason is not None:
                job.cancellation.cancel(reason, reason_code=reason)
            if job.cancellation.is_cancelled:
                job.activity.interrupt_request()
        if self.lead_activity_provider is not None:
            # Foreground Root/Lead turns run on RunScheduler, not our worker pool.
            for activity in self.lead_activity_provider():
                reason = activity.timeout_reason()
                if reason is not None:
                    activity.cancellation.cancel(reason, reason_code=reason)
                if activity.cancellation.is_cancelled:
                    activity.interrupt_request()


def _path_allowed_for_recovery(
    path: str,
    scopes: tuple[str, ...],
    *,
    repository_lease: bool,
) -> bool:
    if not scopes:
        return repository_lease
    normalized = _path_key(path)
    return any(
        normalized == _scope_root(scope)
        or normalized.startswith(_scope_root(scope) + "/")
        for scope in scopes
    )


def _has_repository_lease(repository: Any, team_run_id: str, attempt_id: str) -> bool:
    return any(
        lease.state in {"active", "orphaned"}
        and lease.resource_kind == "repository"
        for lease in repository.list_resource_leases(
            team_run_id, attempt_id=attempt_id
        )
    )


def _path_key(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def _scope_root(scope: str) -> str:
    normalized = _path_key(scope)
    return normalized[:-3].rstrip("/") if normalized.endswith("/**") else normalized


# Kept as an import-compatible name for the former extension point.
BackgroundTaskRunner = TeamSupervisor


__all__ = ["BackgroundTaskRunner", "TeamAgentBuilder", "TeamSupervisor"]
