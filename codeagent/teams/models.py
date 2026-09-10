"""Persistent domain records for the first Agent Team delivery.

The records in this module contain no scheduler, SQLite, or HTTP behavior.
They are the small contract shared by the persistence and runtime layers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


JsonObject = dict[str, Any]


class TeamRunState(str, Enum):
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    READY_FOR_MANUAL_INTEGRATION = "ready_for_manual_integration"
    COMPLETED = "completed"
    CLOSED_WITH_UNMERGED_CANDIDATES = "closed_with_unmerged_candidates"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TeamPlanStatus(str, Enum):
    DRAFT = "draft"
    PENDING_USER_APPROVAL = "pending_user_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class TeamAgentRole(str, Enum):
    LEAD = "lead"
    TEAMMATE = "teammate"


class AgentSessionState(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    WORK = "work"
    WAITING = "waiting"
    SUSPECT = "suspect"
    LOST = "lost"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


class TaskAttemptState(str, Enum):
    ASSIGNED = "assigned"
    PLAN_REQUIRED = "plan_required"
    PLAN_SUBMITTED = "plan_submitted"
    PLAN_APPROVED = "plan_approved"
    RUNNING = "running"
    WAITING = "waiting"
    CANDIDATE_SUBMITTED = "candidate_submitted"
    REVIEW_REJECTED = "review_rejected"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class DependencyRequirement(str, Enum):
    TASK_COMPLETED = "task_completed"
    CANDIDATE_INTEGRATED = "candidate_integrated"


class AttemptPlanStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class CandidateStatus(str, Enum):
    SUBMITTED = "submitted"
    REWORK = "rework"
    ACCEPTED = "accepted"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class TeamRunRecord:
    id: str
    conversation_id: str
    root_run_id: str
    task_list_id: str
    lead_agent_id: str
    base_commit: str
    state: TeamRunState
    created_at: str
    updated_at: str
    active_plan_revision: int | None = None
    max_teammates: int = 3
    token_budget: int | None = None
    model_call_budget: int | None = None
    deadline_at: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass(frozen=True, slots=True)
class TeamPlanRevisionRecord:
    team_run_id: str
    revision: int
    status: TeamPlanStatus
    plan: JsonObject
    plan_hash: str
    created_by: str
    created_at: str
    submitted_at: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(frozen=True, slots=True)
class TeamAgentRecord:
    id: str
    team_run_id: str
    role: TeamAgentRole
    name: str
    created_at: str
    model: str = ""
    capabilities: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["role"] = self.role.value
        result["capabilities"] = list(self.capabilities)
        return result


@dataclass(frozen=True, slots=True)
class AgentSessionRecord:
    id: str
    team_run_id: str
    agent_id: str
    generation: int
    state: AgentSessionState
    created_at: str
    updated_at: str
    heartbeat_at: str
    current_attempt_id: str | None = None
    checkpoint_id: str | None = None
    waiting_reason: str | None = None
    failure: JsonObject | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass(frozen=True, slots=True)
class AgentSessionCheckpointRecord:
    id: str
    session_id: str
    generation: int
    revision: int
    messages: list[JsonObject]
    context: JsonObject
    metadata: JsonObject
    safe_boundary: str
    created_at: str

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    id: str
    team_run_id: str
    task_list_id: str
    task_id: str
    agent_id: str
    session_id: str
    ordinal: int
    state: TaskAttemptState
    team_plan_revision: int
    attempt_base_commit: str
    lease_token: str
    write_enabled: bool
    result_unknown: bool
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested_at: str | None = None
    worker_exited_at: str | None = None
    error: JsonObject | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass(frozen=True, slots=True)
class TeamMessageRecord:
    id: str
    team_run_id: str
    sender_type: str
    recipient_type: str
    recipient_agent_id: str
    recipient_generation: int
    type: str
    payload_version: int
    payload: JsonObject
    artifact_refs: tuple[str, ...]
    correlation_id: str | None
    causation_id: str | None
    dedupe_key: str
    sequence_no: int
    priority: str
    created_at: str
    sender_agent_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    delivered_at: str | None = None
    acked_at: str | None = None
    delivery_attempts: int = 0
    last_delivery_error: str | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["artifact_refs"] = list(self.artifact_refs)
        return result


@dataclass(frozen=True, slots=True)
class TaskSchedulingRecord:
    task_id: str
    dependency_ready: bool
    schedulable: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


@dataclass(frozen=True, slots=True)
class ResourceLeaseRecord:
    id: str
    team_run_id: str
    attempt_id: str
    resource_kind: str
    resource_key: str
    generation: int
    state: str
    acquired_at: str
    released_at: str | None = None

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorktreeBindingRecord:
    id: str
    team_run_id: str
    attempt_id: str
    agent_id: str
    session_id: str
    generation: int
    path: str
    branch: str
    base_commit: str
    head_commit: str
    fingerprint: str
    state: str
    write_scopes: tuple[str, ...]
    write_enabled: bool
    created_at: str
    updated_at: str
    frozen_reason: str | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["write_scopes"] = list(self.write_scopes)
        return result


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    id: str
    team_run_id: str
    agent_id: str
    session_id: str
    task_id: str
    attempt_id: str
    tool_call_id: str
    tool_name: str
    risk: str
    status: str
    is_write: bool
    result_unknown: bool
    input: JsonObject
    started_at: str
    worktree_id: str | None = None
    trace_id: str | None = None
    plan_revision: int | None = None
    output_ref: str | None = None
    error: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AttemptPlanRecord:
    id: str
    team_run_id: str
    attempt_id: str
    revision: int
    status: AttemptPlanStatus
    summary: str
    planned_files: tuple[str, ...]
    planned_commands: tuple[str, ...]
    planned_tests: tuple[str, ...]
    write_scopes: tuple[str, ...]
    scope_hash: str
    risk_level: str
    team_plan_revision: int
    base_commit: str
    worktree_fingerprint: str
    created_by: str
    created_at: str
    submitted_at: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["status"] = self.status.value
        for name in (
            "planned_files",
            "planned_commands",
            "planned_tests",
            "write_scopes",
        ):
            result[name] = list(result[name])
        return result


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    id: str
    team_run_id: str
    task_id: str
    attempt_id: str
    revision: int
    status: CandidateStatus
    summary: str
    changed_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    diff_ref: str
    diff_hash: str
    base_commit: str
    worktree_head: str
    tests_reported: tuple[str, ...]
    known_risks: tuple[str, ...]
    submitted_by: str
    submitted_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_reason: str | None = None
    user_approval_required: bool = False
    user_decision: str | None = None
    user_decided_by: str | None = None
    user_decided_at: str | None = None
    user_decision_reason: str | None = None
    commit_hash: str | None = None
    committed_at: str | None = None
    integrated_commit: str | None = None
    integrated_at: str | None = None

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        result["status"] = self.status.value
        for name in (
            "changed_files",
            "untracked_files",
            "tests_reported",
            "known_risks",
        ):
            result[name] = list(result[name])
        return result


@dataclass(frozen=True, slots=True)
class ValidationRunRecord:
    id: str
    team_run_id: str
    attempt_id: str
    candidate_id: str
    command: str
    status: str
    exit_code: int | None
    output_ref: str | None
    duration_ms: int | None
    started_at: str
    finished_at: str | None = None

    def to_dict(self) -> JsonObject:
        return asdict(self)


__all__ = [
    "AgentSessionCheckpointRecord",
    "AgentSessionRecord",
    "AgentSessionState",
    "AttemptPlanRecord",
    "AttemptPlanStatus",
    "CandidateRecord",
    "CandidateStatus",
    "DependencyRequirement",
    "JsonObject",
    "ResourceLeaseRecord",
    "TaskAttemptRecord",
    "TaskAttemptState",
    "TaskSchedulingRecord",
    "TeamAgentRecord",
    "TeamAgentRole",
    "TeamMessageRecord",
    "TeamPlanRevisionRecord",
    "TeamPlanStatus",
    "TeamRunRecord",
    "TeamRunState",
    "ToolExecutionRecord",
    "ValidationRunRecord",
    "WorktreeBindingRecord",
]
