"""Thread-safe SQLite persistence for the local CodeAgent web runtime.

The repository owns all SQL and JSON serialization.  Callers work with typed
records and framework-neutral methods, which keeps the scheduler and HTTP API
independent from SQLite and makes a future persistence adapter straightforward.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol
from uuid import uuid4

from codeagent.events import RunEvent, TokenUsage, redact_payload, utc_now_iso
from codeagent.runtime.data_paths import RuntimeDataPaths
from codeagent.tasks import (
    TaskActivityRecord,
    TaskListRecord,
    TaskListScope,
    TaskRecord,
    TaskResource,
    TaskStatus,
)
from codeagent.teams import (
    AgentSessionCheckpointRecord,
    AgentSessionRecord,
    AgentSessionState,
    AttemptPlanRecord,
    AttemptPlanStatus,
    CandidateRecord,
    CandidateStatus,
    DependencyRequirement,
    ResourceLeaseRecord,
    TaskAttemptRecord,
    TaskAttemptState,
    TaskSchedulingRecord,
    TeamAgentRecord,
    TeamAgentRole,
    TeamMessageRecord,
    TeamPlanRevisionRecord,
    TeamPlanStatus,
    TeamRunRecord,
    TeamRunState,
    ToolExecutionRecord,
    ValidationRunRecord,
    WorktreeBindingRecord,
)
from codeagent.teams.messages import validate_team_message
from codeagent.teams.tasks import requires_attempt_plan, validate_task_execution
from codeagent.web.models import (
    ApprovalRecord,
    CheckpointRecord,
    ConversationRecord,
    JsonObject,
    MessageRecord,
    ModelCallRecord,
    RunRecord,
)
from codeagent.web.team_observation import install_observation, read_changes


ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
RUN_STATUSES = ACTIVE_RUN_STATUSES | TERMINAL_RUN_STATUSES
APPROVAL_DECISIONS = frozenset({"allow", "deny"})
APPROVAL_STATUSES = frozenset({"pending", "allowed", "denied", "expired"})
MODEL_CALL_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})
TASK_STATUSES = frozenset(item.value for item in TaskStatus)
TASK_LIST_SCOPES = frozenset(item.value for item in TaskListScope)
TEAM_RUN_STATES = frozenset(item.value for item in TeamRunState)
TEAM_PLAN_STATUSES = frozenset(item.value for item in TeamPlanStatus)
TEAM_AGENT_ROLES = frozenset(item.value for item in TeamAgentRole)
AGENT_SESSION_STATES = frozenset(item.value for item in AgentSessionState)
TASK_ATTEMPT_STATES = frozenset(item.value for item in TaskAttemptState)
DEPENDENCY_REQUIREMENTS = frozenset(item.value for item in DependencyRequirement)

_ACTIVE_AGENT_SESSION_STATES = (
    AgentSessionState.STARTING.value,
    AgentSessionState.IDLE.value,
    AgentSessionState.WORK.value,
    AgentSessionState.WAITING.value,
    AgentSessionState.SUSPECT.value,
)
_ACTIVE_TASK_ATTEMPT_STATES = tuple(
    item.value
    for item in TaskAttemptState
    if item
    not in {
        TaskAttemptState.SUCCEEDED,
        TaskAttemptState.FAILED,
        TaskAttemptState.CANCELLED,
        TaskAttemptState.ORPHANED,
    }
)
_TERMINAL_TEAM_RUN_STATES = frozenset(
    {
        TeamRunState.COMPLETED.value,
        TeamRunState.CLOSED_WITH_UNMERGED_CANDIDATES.value,
        TeamRunState.FAILED.value,
        TeamRunState.CANCELLED.value,
    }
)

_UNSET = object()


class StorageError(RuntimeError):
    """Base class for repository-level failures."""


class RecordNotFoundError(StorageError):
    """Raised when a requested record does not exist."""


class StorageConflictError(StorageError):
    """Raised when a uniqueness or active-run invariant is violated."""


class InvalidStateTransitionError(StorageError):
    """Raised when a run is moved through an invalid lifecycle transition."""


class Repository(Protocol):
    """Persistence contract consumed by the web scheduler and API layer."""

    def create_conversation(
        self,
        *,
        title: str = "New conversation",
        workspace: str | Path | None = None,
        conversation_id: str | None = None,
    ) -> ConversationRecord: ...

    def create_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: Any,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        message_id: str | None = None,
    ) -> MessageRecord: ...

    def create_run(
        self,
        conversation_id: str,
        *,
        run_id: str | None = None,
        status: str = "queued",
        metadata: Mapping[str, Any] | None = None,
    ) -> RunRecord: ...

    def append_event(self, event: RunEvent) -> RunEvent: ...

    def list_events(
        self, run_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[RunEvent]: ...

    def wait_for_events(
        self, run_id: str, after_seq: int = 0, timeout: float = 15.0
    ) -> list[RunEvent]: ...

    def aggregate_usage(
        self,
        *,
        run_id: str | None = None,
        conversation_id: str | None = None,
        include_breakdown: bool = True,
    ) -> JsonObject: ...


class SQLiteRepository:
    """A single-process, thread-safe SQLite repository.

    One connection is protected by a re-entrant lock.  This deliberately
    supports both on-disk databases and ``:memory:`` databases while remaining
    safe when FastAPI dispatches synchronous handlers from multiple threads.
    WAL mode is enabled for on-disk databases.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        recover_incomplete: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._event_condition = threading.Condition()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self.initialize()
        if recover_incomplete:
            self.mark_incomplete_runs_interrupted()
            self.recover_incomplete_team_runtime()

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        recover_incomplete: bool = True,
        data_dir: str | Path | None = None,
    ) -> "SQLiteRepository":
        paths = RuntimeDataPaths(Path(data_dir)) if data_dir else RuntimeDataPaths.default()
        database = paths.state_database
        legacy = Path(workspace).expanduser().resolve() / ".codeagent" / "state.db"
        if not database.exists() and legacy.is_file():
            _backup_legacy_database(legacy, database)
        return cls(
            database,
            recover_incomplete=recover_incomplete,
        )

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            active_task_list_id TEXT
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            queue_position INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            cancel_requested_at TEXT,
            error_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            next_event_seq INTEGER NOT NULL DEFAULT 0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_conversation
            ON runs(conversation_id)
            WHERE status IN ('queued', 'running');
        CREATE INDEX IF NOT EXISTS runs_status_queue_idx
            ON runs(status, queue_position, created_at);
        CREATE INDEX IF NOT EXISTS runs_conversation_idx
            ON runs(conversation_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_conversation_idx
            ON messages(conversation_id, created_at, id);
        CREATE INDEX IF NOT EXISTS messages_run_idx ON messages(run_id);

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL DEFAULT 'agent_root',
            parent_agent_id TEXT,
            iteration INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(run_id, seq)
        );
        CREATE INDEX IF NOT EXISTS events_replay_idx ON events(run_id, seq);

        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            event_seq INTEGER,
            tool_call_id TEXT,
            tool_name TEXT NOT NULL,
            tool_input_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decision TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            resolved_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS approvals_run_status_idx
            ON approvals(run_id, status, created_at);

        CREATE TABLE IF NOT EXISTS model_calls (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            parent_agent_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            call_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_creation_input_tokens INTEGER,
            cache_read_input_tokens INTEGER,
            usage_available INTEGER NOT NULL DEFAULT 0,
            estimated INTEGER NOT NULL DEFAULT 0,
            error_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS model_calls_run_idx
            ON model_calls(run_id, started_at);
        CREATE INDEX IF NOT EXISTS model_calls_conversation_idx
            ON model_calls(conversation_id, started_at);

        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
            run_status TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            todos_json TEXT NOT NULL,
            context_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS checkpoints_conversation_idx
            ON checkpoints(conversation_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS task_lists (
            id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            name TEXT NOT NULL,
            scope TEXT NOT NULL,
            origin_conversation_id TEXT,
            next_task_number INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE INDEX IF NOT EXISTS task_lists_workspace_idx
            ON task_lists(workspace, archived_at, updated_at DESC);

        CREATE TABLE IF NOT EXISTS tasks (
            task_list_id TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
            id TEXT NOT NULL,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            active_form TEXT,
            owner TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(task_list_id, id)
        );
        CREATE INDEX IF NOT EXISTS tasks_list_status_idx
            ON tasks(task_list_id, status, CAST(id AS INTEGER));
        CREATE UNIQUE INDEX IF NOT EXISTS one_in_progress_task_per_owner
            ON tasks(task_list_id, owner)
            WHERE status = 'in_progress' AND owner IS NOT NULL;

        CREATE TABLE IF NOT EXISTS task_dependencies (
            task_list_id TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
            blocker_id TEXT NOT NULL,
            blocked_id TEXT NOT NULL,
            PRIMARY KEY(task_list_id, blocker_id, blocked_id),
            FOREIGN KEY(task_list_id, blocker_id)
                REFERENCES tasks(task_list_id, id) ON DELETE CASCADE,
            FOREIGN KEY(task_list_id, blocked_id)
                REFERENCES tasks(task_list_id, id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS task_dependencies_blocked_idx
            ON task_dependencies(task_list_id, blocked_id);

        CREATE TABLE IF NOT EXISTS task_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_list_id TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            conversation_id TEXT,
            run_id TEXT,
            agent_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS task_activity_list_idx
            ON task_activity(task_list_id, id);
        CREATE INDEX IF NOT EXISTS task_activity_task_idx
            ON task_activity(task_list_id, task_id, id DESC);

        CREATE TABLE IF NOT EXISTS team_runs (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            root_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
            task_list_id TEXT NOT NULL REFERENCES task_lists(id) ON DELETE RESTRICT,
            lead_agent_id TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            state TEXT NOT NULL,
            active_plan_revision INTEGER,
            max_teammates INTEGER NOT NULL DEFAULT 3,
            token_budget INTEGER,
            model_call_budget INTEGER,
            deadline_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS team_runs_state_idx
            ON team_runs(state, updated_at);

        CREATE TABLE IF NOT EXISTS team_plan_revisions (
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            decided_by TEXT,
            decided_at TEXT,
            decision_reason TEXT,
            PRIMARY KEY(team_run_id, revision)
        );
        CREATE INDEX IF NOT EXISTS team_plan_status_idx
            ON team_plan_revisions(team_run_id, status, revision);

        CREATE TABLE IF NOT EXISTS team_agents (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(team_run_id, name)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_lead_per_team
            ON team_agents(team_run_id) WHERE role = 'lead';

        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            state TEXT NOT NULL,
            current_attempt_id TEXT,
            checkpoint_id TEXT,
            heartbeat_at TEXT NOT NULL,
            waiting_reason TEXT,
            failure_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(agent_id, generation)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_team_agent
            ON agent_sessions(agent_id)
            WHERE state IN ('starting', 'idle', 'work', 'waiting', 'suspect');
        CREATE INDEX IF NOT EXISTS agent_sessions_team_state_idx
            ON agent_sessions(team_run_id, state, updated_at);

        CREATE TABLE IF NOT EXISTS agent_session_checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            messages_json TEXT NOT NULL,
            context_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            safe_boundary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, revision)
        );
        CREATE INDEX IF NOT EXISTS agent_session_checkpoints_latest_idx
            ON agent_session_checkpoints(session_id, revision DESC);

        CREATE TABLE IF NOT EXISTS task_attempts (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            task_list_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL,
            state TEXT NOT NULL,
            team_plan_revision INTEGER NOT NULL,
            attempt_base_commit TEXT NOT NULL,
            lease_token TEXT NOT NULL UNIQUE,
            write_enabled INTEGER NOT NULL DEFAULT 0,
            result_unknown INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            cancel_requested_at TEXT,
            worker_exited_at TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_list_id, task_id, ordinal),
            FOREIGN KEY(task_list_id, task_id)
                REFERENCES tasks(task_list_id, id) ON DELETE RESTRICT,
            FOREIGN KEY(team_run_id, team_plan_revision)
                REFERENCES team_plan_revisions(team_run_id, revision) ON DELETE RESTRICT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_task
            ON task_attempts(task_list_id, task_id)
            WHERE state NOT IN ('succeeded', 'failed', 'cancelled', 'orphaned');
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_agent
            ON task_attempts(agent_id)
            WHERE state NOT IN ('succeeded', 'failed', 'cancelled', 'orphaned');
        CREATE INDEX IF NOT EXISTS task_attempts_team_state_idx
            ON task_attempts(team_run_id, state, created_at);

        CREATE TABLE IF NOT EXISTS resource_leases (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            resource_kind TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            generation INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            acquired_at TEXT NOT NULL,
            released_at TEXT
        );
        CREATE INDEX IF NOT EXISTS resource_leases_team_state_idx
            ON resource_leases(team_run_id, state, resource_kind, resource_key);

        CREATE TABLE IF NOT EXISTS team_messages (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            sender_type TEXT NOT NULL,
            sender_agent_id TEXT,
            recipient_type TEXT NOT NULL,
            recipient_agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE CASCADE,
            recipient_generation INTEGER NOT NULL,
            task_id TEXT,
            attempt_id TEXT REFERENCES task_attempts(id) ON DELETE SET NULL,
            type TEXT NOT NULL,
            payload_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            correlation_id TEXT,
            causation_id TEXT,
            dedupe_key TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            acked_at TEXT,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            last_delivery_error TEXT,
            UNIQUE(team_run_id, dedupe_key),
            UNIQUE(team_run_id, recipient_agent_id, recipient_generation, sequence_no)
        );
        CREATE INDEX IF NOT EXISTS team_messages_inbox_idx
            ON team_messages(
                team_run_id, recipient_agent_id, recipient_generation,
                acked_at, priority, sequence_no
            );

        CREATE TABLE IF NOT EXISTS team_message_consumptions (
            message_id TEXT NOT NULL REFERENCES team_messages(id) ON DELETE CASCADE,
            consumer_agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE CASCADE,
            consumer_generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            result_ref TEXT,
            processed_at TEXT NOT NULL,
            PRIMARY KEY(message_id, consumer_agent_id, consumer_generation)
        );

        CREATE TABLE IF NOT EXISTS team_commands (
            command_id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_base_confirmations (
            team_run_id TEXT PRIMARY KEY REFERENCES team_runs(id) ON DELETE CASCADE,
            base_commit TEXT NOT NULL,
            source_head TEXT NOT NULL,
            source_dirty INTEGER NOT NULL,
            status_hash TEXT NOT NULL,
            confirmed_by TEXT NOT NULL,
            confirmed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worktree_bindings (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL,
            path TEXT NOT NULL,
            branch TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            head_commit TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            write_scopes_json TEXT NOT NULL DEFAULT '[]',
            write_enabled INTEGER NOT NULL DEFAULT 0,
            frozen_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(attempt_id),
            UNIQUE(path),
            UNIQUE(branch)
        );
        CREATE INDEX IF NOT EXISTS worktree_bindings_team_state_idx
            ON worktree_bindings(team_run_id, state, created_at);

        CREATE TABLE IF NOT EXISTS tool_executions (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL REFERENCES team_agents(id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE RESTRICT,
            task_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            worktree_id TEXT REFERENCES worktree_bindings(id) ON DELETE SET NULL,
            tool_call_id TEXT NOT NULL,
            trace_id TEXT,
            plan_revision INTEGER,
            tool_name TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL,
            is_write INTEGER NOT NULL DEFAULT 0,
            result_unknown INTEGER NOT NULL DEFAULT 0,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_ref TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(attempt_id, tool_call_id)
        );
        CREATE INDEX IF NOT EXISTS tool_executions_attempt_idx
            ON tool_executions(attempt_id, started_at, id);

        CREATE TABLE IF NOT EXISTS attempt_plans (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            planned_files_json TEXT NOT NULL DEFAULT '[]',
            planned_commands_json TEXT NOT NULL DEFAULT '[]',
            planned_tests_json TEXT NOT NULL DEFAULT '[]',
            write_scopes_json TEXT NOT NULL DEFAULT '[]',
            scope_hash TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            team_plan_revision INTEGER NOT NULL,
            base_commit TEXT NOT NULL,
            worktree_fingerprint TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            decided_by TEXT,
            decided_at TEXT,
            decision_reason TEXT,
            UNIQUE(attempt_id, revision)
        );
        CREATE INDEX IF NOT EXISTS attempt_plans_attempt_status_idx
            ON attempt_plans(attempt_id, status, revision);

        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            untracked_files_json TEXT NOT NULL DEFAULT '[]',
            diff_ref TEXT NOT NULL,
            diff_hash TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            worktree_head TEXT NOT NULL,
            tests_reported_json TEXT NOT NULL DEFAULT '[]',
            known_risks_json TEXT NOT NULL DEFAULT '[]',
            submitted_by TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            reviewed_by TEXT,
            reviewed_at TEXT,
            review_reason TEXT,
            user_approval_required INTEGER NOT NULL DEFAULT 0,
            user_decision TEXT,
            user_decided_by TEXT,
            user_decided_at TEXT,
            user_decision_reason TEXT,
            commit_hash TEXT,
            committed_at TEXT,
            integrated_commit TEXT,
            integrated_at TEXT,
            UNIQUE(attempt_id, revision)
        );
        CREATE INDEX IF NOT EXISTS candidates_team_status_idx
            ON candidates(team_run_id, status, submitted_at);

        CREATE TABLE IF NOT EXISTS validation_runs (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
            candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER,
            output_ref TEXT,
            duration_ms INTEGER,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS validation_runs_candidate_idx
            ON validation_runs(candidate_id, started_at, id);

        CREATE TABLE IF NOT EXISTS manual_integration_checks (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
            target_ref TEXT NOT NULL,
            target_commit TEXT NOT NULL,
            status TEXT NOT NULL,
            checks_json TEXT NOT NULL DEFAULT '[]',
            verified_by TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS manual_integration_checks_team_idx
            ON manual_integration_checks(team_run_id, verified_at, id);
        """
        with self._lock:
            self._ensure_open()
            self._connection.executescript(schema)
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(conversations)"
                ).fetchall()
            }
            if "workspace" not in columns:
                self._connection.execute(
                    "ALTER TABLE conversations "
                    "ADD COLUMN workspace TEXT NOT NULL DEFAULT ''"
                )
            if "active_task_list_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE conversations ADD COLUMN active_task_list_id TEXT"
                )
            dependency_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(task_dependencies)"
                ).fetchall()
            }
            if "dependency_requirement" not in dependency_columns:
                self._connection.execute(
                    "ALTER TABLE task_dependencies ADD COLUMN "
                    "dependency_requirement TEXT NOT NULL DEFAULT 'task_completed'"
                )
            attempt_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(task_attempts)"
                ).fetchall()
            }
            if "cancel_requested_at" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE task_attempts ADD COLUMN cancel_requested_at TEXT"
                )
            if "worker_exited_at" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE task_attempts ADD COLUMN worker_exited_at TEXT"
                )
            candidate_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(candidates)"
                ).fetchall()
            }
            if "integrated_commit" not in candidate_columns:
                self._connection.execute(
                    "ALTER TABLE candidates ADD COLUMN integrated_commit TEXT"
                )
            if "integrated_at" not in candidate_columns:
                self._connection.execute(
                    "ALTER TABLE candidates ADD COLUMN integrated_at TEXT"
                )
            for name, definition in (
                ("user_approval_required", "INTEGER NOT NULL DEFAULT 0"),
                ("user_decision", "TEXT"),
                ("user_decided_by", "TEXT"),
                ("user_decided_at", "TEXT"),
                ("user_decision_reason", "TEXT"),
            ):
                if name not in candidate_columns:
                    self._connection.execute(
                        f"ALTER TABLE candidates ADD COLUMN {name} {definition}"
                    )
            legacy_conversations = self._connection.execute(
                """
                SELECT id, title, workspace, created_at, updated_at
                FROM conversations WHERE active_task_list_id IS NULL
                """
            ).fetchall()
            for conversation in legacy_conversations:
                task_list_id = _new_id("tasklist")
                self._connection.execute(
                    """
                    INSERT INTO task_lists(
                        id, workspace, name, scope, origin_conversation_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_list_id,
                        str(conversation["workspace"] or ""),
                        f"{conversation['title']} tasks",
                        TaskListScope.CONVERSATION_PRIVATE.value,
                        str(conversation["id"]),
                        str(conversation["created_at"]),
                        str(conversation["updated_at"]),
                    ),
                )
                self._connection.execute(
                    "UPDATE conversations SET active_task_list_id = ? WHERE id = ?",
                    (task_list_id, conversation["id"]),
                )
            self._connection.execute("PRAGMA user_version = 9")
            install_observation(self._connection)

    def list_team_changes(
        self, team_run_id: str, *, after: int = 0, limit: int = 100,
        table: str | None = None, seq: int | None = None,
    ) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid observation cursor or page size")
        with self._lock:
            self._ensure_open()
            team = self.get_team_run(team_run_id)
            if team is None:
                raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
            return read_changes(self._connection, team, after=after, limit=limit, table=table, seq=seq)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
        with self._event_condition:
            self._event_condition.notify_all()

    def health_check(self) -> bool:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("Repository is closed")

    # Conversations -----------------------------------------------------

    def create_conversation(
        self,
        *,
        title: str = "New conversation",
        workspace: str | Path | None = None,
        conversation_id: str | None = None,
        created_at: str | None = None,
    ) -> ConversationRecord:
        identifier = conversation_id or _new_id("conv")
        clean_title = str(title).strip() or "New conversation"
        workspace_value = str(workspace or "").strip()
        now = created_at or utc_now_iso()
        try:
            with self._transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        id, title, workspace, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (identifier, clean_title, workspace_value, now, now),
                )
                task_list_id = _new_id("tasklist")
                connection.execute(
                    """
                    INSERT INTO task_lists(
                        id, workspace, name, scope, origin_conversation_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_list_id,
                        workspace_value,
                        f"{clean_title} tasks",
                        TaskListScope.CONVERSATION_PRIVATE.value,
                        identifier,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE conversations SET active_task_list_id = ? WHERE id = ?",
                    (task_list_id, identifier),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError(
                f"Conversation already exists: {identifier}"
            ) from exc
        return self._require_conversation(identifier)

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _row_to_conversation(row) if row else None

    def _require_conversation(self, conversation_id: str) -> ConversationRecord:
        record = self.get_conversation(conversation_id)
        if record is None:
            raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
        return record

    def list_conversations(
        self,
        *,
        include_archived: bool = False,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConversationRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if not include_archived:
            conditions.append("archived_at IS NULL")
        if query and query.strip():
            conditions.append("title LIKE ? ESCAPE '\\'")
            parameters.append(f"%{_escape_like(query.strip())}%")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend((_positive_limit(limit), max(0, offset)))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM conversations
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_conversation(row) for row in rows]

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | object = _UNSET,
        archived: bool | object = _UNSET,
    ) -> ConversationRecord:
        assignments = ["updated_at = ?"]
        parameters: list[Any] = [utc_now_iso()]
        if title is not _UNSET:
            clean_title = str(title).strip()
            if not clean_title:
                raise ValueError("Conversation title cannot be empty")
            assignments.append("title = ?")
            parameters.append(clean_title)
        if archived is not _UNSET:
            assignments.append("archived_at = ?")
            parameters.append(utc_now_iso() if bool(archived) else None)
        parameters.append(conversation_id)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE conversations SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(
                    f"Conversation not found: {conversation_id}"
                )
            if archived is not _UNSET and bool(archived):
                connection.execute(
                    """
                    UPDATE tasks SET status = 'pending', owner = NULL,
                        revision = revision + 1, updated_at = ?
                    WHERE status = 'in_progress' AND owner LIKE ?
                    """,
                    (utc_now_iso(), f"{conversation_id}:%"),
                )
        return self._require_conversation(conversation_id)

    def bind_unassigned_workspaces(self, workspace: str | Path) -> int:
        """Bind records created before schema v2 to the server's default workspace."""

        value = str(workspace).strip()
        if not value:
            raise ValueError("Workspace path cannot be empty")
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE conversations SET workspace = ? WHERE workspace = ''",
                (value,),
            )
            connection.execute(
                "UPDATE task_lists SET workspace = ? WHERE workspace = ''",
                (value,),
            )
        return max(0, cursor.rowcount)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
        return cursor.rowcount > 0

    # Task lists and tasks ---------------------------------------------

    def ensure_conversation_task_list(self, conversation_id: str) -> TaskListRecord:
        conversation = self._require_conversation(conversation_id)
        if conversation.active_task_list_id:
            record = self.get_task_list(conversation.active_task_list_id)
            if record is not None and record.archived_at is None:
                return record
        now = utc_now_iso()
        identifier = _new_id("tasklist")
        with self._transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if current is None:
                raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
            if current["active_task_list_id"]:
                existing = connection.execute(
                    "SELECT * FROM task_lists WHERE id = ?",
                    (current["active_task_list_id"],),
                ).fetchone()
                if existing is not None and existing["archived_at"] is None:
                    return _row_to_task_list(existing)
            connection.execute(
                """
                INSERT INTO task_lists(
                    id, workspace, name, scope, origin_conversation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    str(current["workspace"] or ""),
                    f"{current['title']} tasks",
                    TaskListScope.CONVERSATION_PRIVATE.value,
                    conversation_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET active_task_list_id = ?, updated_at = ? WHERE id = ?",
                (identifier, now, conversation_id),
            )
        return self._require_task_list(identifier)

    def create_task_list(
        self,
        *,
        workspace: str | Path,
        name: str,
        scope: str = TaskListScope.CONVERSATION_PRIVATE.value,
        origin_conversation_id: str | None = None,
        task_list_id: str | None = None,
    ) -> TaskListRecord:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("Task list name cannot be empty")
        _validate_choice("task list scope", scope, TASK_LIST_SCOPES)
        identifier = task_list_id or _new_id("tasklist")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO task_lists(
                    id, workspace, name, scope, origin_conversation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    str(workspace),
                    clean_name,
                    scope,
                    origin_conversation_id,
                    now,
                    now,
                ),
            )
        return self._require_task_list(identifier)

    def get_task_list(self, task_list_id: str) -> TaskListRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM task_lists WHERE id = ?", (task_list_id,)
            ).fetchone()
        return _row_to_task_list(row) if row else None

    def _require_task_list(self, task_list_id: str) -> TaskListRecord:
        record = self.get_task_list(task_list_id)
        if record is None:
            raise RecordNotFoundError(f"Task list not found: {task_list_id}")
        return record

    def list_task_lists(
        self,
        *,
        workspace: str | Path | None = None,
        include_archived: bool = False,
    ) -> list[TaskListRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if workspace is not None:
            conditions.append("workspace = ?")
            parameters.append(str(workspace))
        if not include_archived:
            conditions.append("archived_at IS NULL")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM task_lists {where} ORDER BY updated_at DESC, id DESC",
                parameters,
            ).fetchall()
        return [_row_to_task_list(row) for row in rows]

    def update_task_list(
        self,
        task_list_id: str,
        *,
        name: str | object = _UNSET,
        promote: bool = False,
        archived: bool | object = _UNSET,
        expected_revision: int | None = None,
    ) -> TaskListRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM task_lists WHERE id = ?", (task_list_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Task list not found: {task_list_id}")
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise StorageConflictError("Task list revision conflict")
            values = {
                "name": str(row["name"]),
                "scope": str(row["scope"]),
                "archived_at": row["archived_at"],
            }
            if name is not _UNSET:
                clean_name = str(name).strip()
                if not clean_name:
                    raise ValueError("Task list name cannot be empty")
                values["name"] = clean_name
            if promote:
                values["scope"] = TaskListScope.WORKSPACE_SHARED.value
            if archived is not _UNSET:
                values["archived_at"] = now if bool(archived) else None
            connection.execute(
                """
                UPDATE task_lists
                SET name = ?, scope = ?, archived_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    values["name"],
                    values["scope"],
                    values["archived_at"],
                    now,
                    task_list_id,
                ),
            )
        self._notify_activity()
        return self._require_task_list(task_list_id)

    def bind_conversation_task_list(
        self,
        conversation_id: str,
        task_list_id: str,
    ) -> ConversationRecord:
        with self._transaction(immediate=True) as connection:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            task_list = connection.execute(
                "SELECT * FROM task_lists WHERE id = ?", (task_list_id,)
            ).fetchone()
            if conversation is None:
                raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
            if task_list is None or task_list["archived_at"] is not None:
                raise RecordNotFoundError(f"Task list not found: {task_list_id}")
            if str(conversation["workspace"] or "") != str(task_list["workspace"]):
                raise StorageConflictError("Conversation and task list use different workspaces")
            if (
                task_list["scope"] == TaskListScope.CONVERSATION_PRIVATE.value
                and task_list["origin_conversation_id"] != conversation_id
            ):
                raise StorageConflictError("Private task list cannot be bound to another conversation")
            owner_prefix = f"{conversation_id}:%"
            busy = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE task_list_id = ? AND status = 'in_progress' AND owner LIKE ?
                LIMIT 1
                """,
                (conversation["active_task_list_id"], owner_prefix),
            ).fetchone()
            if busy is not None and conversation["active_task_list_id"] != task_list_id:
                raise StorageConflictError("Release the current in-progress task before switching lists")
            now = utc_now_iso()
            connection.execute(
                "UPDATE conversations SET active_task_list_id = ?, updated_at = ? WHERE id = ?",
                (task_list_id, now, conversation_id),
            )
        return self._require_conversation(conversation_id)

    def create_task(
        self,
        task_list_id: str,
        *,
        subject: str,
        description: str,
        active_form: str | None = None,
        blocked_by: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> TaskResource:
        clean_subject = str(subject).strip()
        clean_description = str(description).strip()
        if not clean_subject or not clean_description:
            raise ValueError("Task subject and description cannot be empty")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            task_list = connection.execute(
                "SELECT * FROM task_lists WHERE id = ?", (task_list_id,)
            ).fetchone()
            if task_list is None or task_list["archived_at"] is not None:
                raise RecordNotFoundError(f"Task list not found: {task_list_id}")
            task_id = str(int(task_list["next_task_number"]))
            connection.execute(
                """
                INSERT INTO tasks(
                    task_list_id, id, subject, description, active_form,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_list_id,
                    task_id,
                    clean_subject,
                    clean_description,
                    _clean_optional(active_form),
                    _json_dumps(dict(metadata or {})),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE task_lists
                SET next_task_number = next_task_number + 1,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, task_list_id),
            )
            self._apply_dependency_changes(
                connection,
                task_list_id,
                task_id,
                {
                    "add_blocks": [],
                    "add_blocked_by": [str(value) for value in blocked_by or ()],
                    "remove_blocks": [],
                    "remove_blocked_by": [],
                },
            )
            self._insert_task_activity(
                connection,
                task_list_id=task_list_id,
                task_id=task_id,
                event_type="created",
                conversation_id=conversation_id,
                run_id=run_id,
                agent_id=agent_id,
                payload={"subject": clean_subject},
                created_at=now,
            )
        self._notify_activity()
        return self.get_task_resource(task_list_id, task_id)

    def get_task_resource(self, task_list_id: str, task_id: str) -> TaskResource:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE task_list_id = ? AND id = ?",
                (task_list_id, str(task_id)),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Task not found: {task_id}")
            return self._row_to_task_resource(row)

    def list_task_resources(
        self,
        task_list_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
    ) -> list[TaskResource]:
        self._require_task_list(task_list_id)
        conditions = ["task_list_id = ?"]
        parameters: list[Any] = [task_list_id]
        if status is not None:
            _validate_choice("task status", status, TASK_STATUSES)
            conditions.append("status = ?")
            parameters.append(status)
        if owner is not None:
            conditions.append("owner = ?")
            parameters.append(owner)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM tasks WHERE {' AND '.join(conditions)}
                ORDER BY CAST(id AS INTEGER), id
                """,
                parameters,
            ).fetchall()
            return [self._row_to_task_resource(row) for row in rows]

    def update_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        changes: Mapping[str, Any],
        expected_revision: int | None = None,
        actor_owner: str | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        human_override: bool = False,
    ) -> TaskResource:
        allowed = {
            "subject", "description", "active_form", "owner", "status", "metadata",
            "add_blocks", "add_blocked_by", "remove_blocks", "remove_blocked_by",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_task_resource(task_list_id, task_id)
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_list_id = ? AND id = ?",
                (task_list_id, str(task_id)),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Task not found: {task_id}")
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise StorageConflictError("Task revision conflict")

            subject = str(changes.get("subject", row["subject"])).strip()
            description = str(changes.get("description", row["description"])).strip()
            if not subject or not description:
                raise ValueError("Task subject and description cannot be empty")
            active_form = (
                _clean_optional(changes["active_form"])
                if "active_form" in changes
                else row["active_form"]
            )
            old_owner = row["owner"]
            owner = changes.get("owner", old_owner)
            if owner is not None:
                owner = str(owner).strip() or None
            status_value = str(changes.get("status", row["status"]))
            _validate_choice("task status", status_value, TASK_STATUSES)

            if not human_override and "owner" in changes:
                if owner not in {None, actor_owner}:
                    raise StorageConflictError("Agent cannot assign a task to another owner")
                if old_owner not in {None, actor_owner}:
                    raise StorageConflictError("Task is owned by another agent")
            if status_value == TaskStatus.IN_PROGRESS.value:
                owner = owner or actor_owner
                if owner is None:
                    raise StorageConflictError("In-progress task requires an owner")
                if old_owner not in {None, owner} and not human_override:
                    raise StorageConflictError("Task is owned by another agent")
                if self._has_open_blockers(connection, task_list_id, str(task_id)):
                    raise StorageConflictError("Blocked task cannot be started")
            if status_value == TaskStatus.COMPLETED.value:
                owner = owner or old_owner or actor_owner
                if self._has_open_blockers(connection, task_list_id, str(task_id)):
                    raise StorageConflictError("Blocked task cannot be completed")
                if not human_override and old_owner not in {None, actor_owner}:
                    raise StorageConflictError("Only the task owner can complete it")
            if status_value == TaskStatus.PENDING.value:
                if row["status"] == TaskStatus.COMPLETED.value:
                    downstream = connection.execute(
                        """
                        SELECT 1 FROM task_dependencies d
                        JOIN tasks t ON t.task_list_id = d.task_list_id AND t.id = d.blocked_id
                        WHERE d.task_list_id = ? AND d.blocker_id = ?
                          AND t.status IN ('in_progress', 'completed') LIMIT 1
                        """,
                        (task_list_id, str(task_id)),
                    ).fetchone()
                    if downstream is not None:
                        raise StorageConflictError("Cannot reopen a task with active or completed downstream work")
                owner = None

            metadata = _json_loads(row["metadata_json"], {})
            if "metadata" in changes:
                incoming = changes["metadata"]
                if not isinstance(incoming, Mapping):
                    raise ValueError("metadata must be an object")
                for key, value in incoming.items():
                    if value is None:
                        metadata.pop(str(key), None)
                    else:
                        metadata[str(key)] = value

            dependency_changes = {
                key: [str(value) for value in changes.get(key, [])]
                for key in (
                    "add_blocks", "add_blocked_by", "remove_blocks", "remove_blocked_by"
                )
            }
            self._apply_dependency_changes(
                connection,
                task_list_id,
                str(task_id),
                dependency_changes,
            )
            try:
                connection.execute(
                    """
                    UPDATE tasks SET subject = ?, description = ?, active_form = ?,
                        owner = ?, status = ?, metadata_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE task_list_id = ? AND id = ?
                    """,
                    (
                        subject,
                        description,
                        active_form,
                        owner,
                        status_value,
                        _json_dumps(metadata),
                        now,
                        task_list_id,
                        str(task_id),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError("Owner already has an in-progress task") from exc
            connection.execute(
                "UPDATE task_lists SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, task_list_id),
            )
            event_type = (
                "completed"
                if status_value == TaskStatus.COMPLETED.value
                and row["status"] != TaskStatus.COMPLETED.value
                else "updated"
            )
            self._insert_task_activity(
                connection,
                task_list_id=task_list_id,
                task_id=str(task_id),
                event_type=event_type,
                conversation_id=conversation_id,
                run_id=run_id,
                agent_id=agent_id,
                payload=redact_payload(dict(changes)),
                created_at=now,
            )
        self._notify_activity()
        return self.get_task_resource(task_list_id, str(task_id))

    def list_task_activity(
        self,
        task_list_id: str,
        *,
        task_id: str | None = None,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[TaskActivityRecord]:
        conditions = ["task_list_id = ?", "id > ?"]
        parameters: list[Any] = [task_list_id, max(0, int(after_id))]
        if task_id is not None:
            conditions.append("task_id = ?")
            parameters.append(str(task_id))
        parameters.append(_positive_limit(limit))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM task_activity WHERE {' AND '.join(conditions)}
                ORDER BY id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_task_activity(row) for row in rows]

    def wait_for_task_activity(
        self,
        task_list_id: str,
        after_id: int = 0,
        timeout: float = 15.0,
    ) -> list[TaskActivityRecord]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            activities = self.list_task_activity(task_list_id, after_id=after_id)
            if activities:
                return activities
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._event_condition:
                self._event_condition.wait(timeout=remaining)

    def _row_to_task_resource(self, row: sqlite3.Row) -> TaskResource:
        task_list_id = str(row["task_list_id"])
        task_id = str(row["id"])
        blocks = self._connection.execute(
            """
            SELECT blocked_id FROM task_dependencies
            WHERE task_list_id = ? AND blocker_id = ?
            ORDER BY CAST(blocked_id AS INTEGER), blocked_id
            """,
            (task_list_id, task_id),
        ).fetchall()
        blocked_by = self._connection.execute(
            """
            SELECT blocker_id FROM task_dependencies
            WHERE task_list_id = ? AND blocked_id = ?
            ORDER BY CAST(blocker_id AS INTEGER), blocker_id
            """,
            (task_list_id, task_id),
        ).fetchall()
        return TaskResource(
            task_list_id=task_list_id,
            task=TaskRecord(
                id=task_id,
                subject=str(row["subject"]),
                description=str(row["description"]),
                active_form=row["active_form"],
                owner=row["owner"],
                status=TaskStatus(str(row["status"])),
                blocks=tuple(str(item["blocked_id"]) for item in blocks),
                blocked_by=tuple(str(item["blocker_id"]) for item in blocked_by),
                metadata=_json_loads(row["metadata_json"], {}),
            ),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _has_open_blockers(
        connection: sqlite3.Connection,
        task_list_id: str,
        task_id: str,
    ) -> bool:
        return connection.execute(
            """
            SELECT 1 FROM task_dependencies d
            JOIN tasks t ON t.task_list_id = d.task_list_id AND t.id = d.blocker_id
            WHERE d.task_list_id = ? AND d.blocked_id = ? AND t.status != 'completed'
            LIMIT 1
            """,
            (task_list_id, task_id),
        ).fetchone() is not None

    def _apply_dependency_changes(
        self,
        connection: sqlite3.Connection,
        task_list_id: str,
        task_id: str,
        changes: Mapping[str, Sequence[str]],
    ) -> None:
        removals = [
            *((task_id, item) for item in changes["remove_blocks"]),
            *((item, task_id) for item in changes["remove_blocked_by"]),
        ]
        for blocker_id, blocked_id in removals:
            connection.execute(
                """
                DELETE FROM task_dependencies
                WHERE task_list_id = ? AND blocker_id = ? AND blocked_id = ?
                """,
                (task_list_id, blocker_id, blocked_id),
            )
        additions = [
            *((task_id, item) for item in changes["add_blocks"]),
            *((item, task_id) for item in changes["add_blocked_by"]),
        ]
        for blocker_id, blocked_id in additions:
            if blocker_id == blocked_id:
                raise StorageConflictError("Task cannot depend on itself")
            rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE task_list_id = ? AND id IN (?, ?)
                """,
                (task_list_id, blocker_id, blocked_id),
            ).fetchall()
            if len(rows) != 2:
                raise RecordNotFoundError("Dependency task not found in current task list")
            blocked_row = connection.execute(
                "SELECT status FROM tasks WHERE task_list_id = ? AND id = ?",
                (task_list_id, blocked_id),
            ).fetchone()
            if blocked_row["status"] != TaskStatus.PENDING.value:
                raise StorageConflictError("Dependencies can only be added to pending tasks")
            existing = connection.execute(
                """
                SELECT 1 FROM task_dependencies
                WHERE task_list_id = ? AND blocker_id = ? AND blocked_id = ?
                """,
                (task_list_id, blocker_id, blocked_id),
            ).fetchone()
            if existing is not None:
                continue
            if self._dependency_path_exists(
                connection, task_list_id, blocked_id, blocker_id
            ):
                raise StorageConflictError("Task dependency would create a cycle")
            connection.execute(
                """
                INSERT INTO task_dependencies(task_list_id, blocker_id, blocked_id)
                VALUES (?, ?, ?)
                """,
                (task_list_id, blocker_id, blocked_id),
            )

    @staticmethod
    def _dependency_path_exists(
        connection: sqlite3.Connection,
        task_list_id: str,
        start_id: str,
        target_id: str,
    ) -> bool:
        row = connection.execute(
            """
            WITH RECURSIVE reachable(id) AS (
                SELECT blocked_id FROM task_dependencies
                WHERE task_list_id = ? AND blocker_id = ?
                UNION
                SELECT d.blocked_id FROM task_dependencies d
                JOIN reachable r ON d.blocker_id = r.id
                WHERE d.task_list_id = ?
            )
            SELECT 1 FROM reachable WHERE id = ? LIMIT 1
            """,
            (task_list_id, start_id, task_list_id, target_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def _insert_task_activity(
        connection: sqlite3.Connection,
        *,
        task_list_id: str,
        task_id: str,
        event_type: str,
        conversation_id: str | None,
        run_id: str | None,
        agent_id: str | None,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_activity(
                task_list_id, task_id, event_type, conversation_id,
                run_id, agent_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_list_id,
                task_id,
                event_type,
                conversation_id,
                run_id,
                agent_id,
                _json_dumps(dict(payload)),
                created_at,
            ),
        )

    def _notify_activity(self) -> None:
        with self._event_condition:
            self._event_condition.notify_all()

    # Agent Team -------------------------------------------------------

    def create_team_run(
        self,
        *,
        conversation_id: str,
        root_run_id: str,
        task_list_id: str,
        base_commit: str,
        lead_agent_id: str | None = None,
        lead_name: str = "Lead",
        max_teammates: int = 3,
        token_budget: int | None = None,
        model_call_budget: int | None = None,
        deadline_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        team_run_id: str | None = None,
    ) -> TeamRunRecord:
        """Create a TeamRun, its stable Lead identity, and Lead Session atomically."""

        identifier = team_run_id or _new_id("team")
        lead_identifier = lead_agent_id or _new_id("agent")
        clean_base = str(base_commit).strip()
        clean_name = str(lead_name).strip() or "Lead"
        if not clean_base:
            raise ValueError("Team base_commit is required")
        if max_teammates < 1:
            raise ValueError("max_teammates must be at least 1")
        now = utc_now_iso()
        lead_session_id = _new_id("session")
        try:
            with self._transaction(immediate=True) as connection:
                run = connection.execute(
                    "SELECT conversation_id FROM runs WHERE id = ?", (root_run_id,)
                ).fetchone()
                if run is None:
                    raise RecordNotFoundError(f"Run not found: {root_run_id}")
                if str(run["conversation_id"]) != conversation_id:
                    raise StorageConflictError(
                        "TeamRun conversation does not match its root run"
                    )
                task_list = connection.execute(
                    "SELECT 1 FROM task_lists WHERE id = ? AND archived_at IS NULL",
                    (task_list_id,),
                ).fetchone()
                if task_list is None:
                    raise RecordNotFoundError(f"Task list not found: {task_list_id}")
                terminal_placeholders = ",".join(
                    "?" for _ in _TERMINAL_TEAM_RUN_STATES
                )
                active_team = connection.execute(
                    f"""
                    SELECT id FROM team_runs
                    WHERE conversation_id = ?
                      AND state NOT IN ({terminal_placeholders})
                    """,
                    (conversation_id, *sorted(_TERMINAL_TEAM_RUN_STATES)),
                ).fetchone()
                if active_team is not None:
                    raise StorageConflictError(
                        "Conversation already has an active TeamRun: "
                        f"{active_team['id']}"
                    )
                connection.execute(
                    """
                    INSERT INTO team_runs(
                        id, conversation_id, root_run_id, task_list_id,
                        lead_agent_id, base_commit, state, max_teammates,
                        token_budget, model_call_budget, deadline_at,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        conversation_id,
                        root_run_id,
                        task_list_id,
                        lead_identifier,
                        clean_base,
                        max_teammates,
                        token_budget,
                        model_call_budget,
                        deadline_at,
                        _json_dumps(dict(metadata or {})),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO team_agents(
                        id, team_run_id, role, name, created_at
                    ) VALUES (?, ?, 'lead', ?, ?)
                    """,
                    (lead_identifier, identifier, clean_name, now),
                )
                connection.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, team_run_id, agent_id, generation, state,
                        heartbeat_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 'idle', ?, ?, ?)
                    """,
                    (lead_session_id, identifier, lead_identifier, now, now, now),
                )
                self._append_event_in_transaction(
                    connection,
                    RunEvent(
                        type="team.created",
                        run_id=root_run_id,
                        conversation_id=conversation_id,
                        agent_id=lead_identifier,
                        payload={
                            "team_run_id": identifier,
                            "task_list_id": task_list_id,
                            "base_commit": clean_base,
                            "state": TeamRunState.PLANNING.value,
                        },
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError(
                "A TeamRun already exists for this root run or identifier"
            ) from exc
        self._notify_activity()
        return self._require_team_run(identifier)

    def get_team_run(self, team_run_id: str) -> TeamRunRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM team_runs WHERE id = ?", (team_run_id,)
            ).fetchone()
        return _row_to_team_run(row) if row else None

    def list_team_runs(
        self,
        *,
        state: str | None = None,
    ) -> list[TeamRunRecord]:
        parameters: list[Any] = []
        where = ""
        if state is not None:
            _validate_choice("TeamRun state", state, TEAM_RUN_STATES)
            where = "WHERE state = ?"
            parameters.append(state)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM team_runs {where} ORDER BY created_at, id",
                parameters,
            ).fetchall()
        return [_row_to_team_run(row) for row in rows]

    def get_active_team_run_for_conversation(
        self, conversation_id: str
    ) -> TeamRunRecord | None:
        """Return the conversation's only non-terminal TeamRun, if one exists."""

        placeholders = ",".join("?" for _ in _TERMINAL_TEAM_RUN_STATES)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM team_runs
                WHERE conversation_id = ? AND state NOT IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                (conversation_id, *sorted(_TERMINAL_TEAM_RUN_STATES)),
            ).fetchall()
        if len(rows) > 1:
            raise StorageConflictError(
                "Conversation has multiple active TeamRuns; execution is paused"
            )
        return _row_to_team_run(rows[0]) if rows else None

    def has_active_team_run_for_workspace(self, workspace: str | Path) -> bool:
        """Return whether shared project Memory must currently be read-only."""

        target = os.path.normcase(str(Path(workspace).expanduser().resolve()))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT c.workspace, t.state
                FROM team_runs t
                JOIN conversations c ON c.id = t.conversation_id
                """
            ).fetchall()
        return any(
            str(row["state"]) not in _TERMINAL_TEAM_RUN_STATES
            and os.path.normcase(
                str(Path(str(row["workspace"])).expanduser().resolve())
            )
            == target
            for row in rows
            if str(row["workspace"]).strip()
        )

    def cancel_team_run(
        self,
        team_run_id: str,
        *,
        cancelled_by: str,
        reason: str,
        command_id: str,
    ) -> TeamRunRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "cancel_team_run"
            )
            if replay is not None:
                return _row_to_team_run(self._team_row(connection, team_run_id))
            team = self._team_row(connection, team_run_id)
            if str(team["state"]) in {
                TeamRunState.COMPLETED.value,
                TeamRunState.FAILED.value,
                TeamRunState.CANCELLED.value,
                TeamRunState.CLOSED_WITH_UNMERGED_CANDIDATES.value,
            }:
                if str(team["state"]) == TeamRunState.CANCELLED.value:
                    return _row_to_team_run(team)
                raise InvalidStateTransitionError("Terminal TeamRun cannot be cancelled")
            connection.execute(
                "UPDATE team_runs SET state = 'cancelled', updated_at = ? WHERE id = ?",
                (now, team_run_id),
            )
            connection.execute(
                "UPDATE task_attempts SET write_enabled = 0, updated_at = ? "
                "WHERE team_run_id = ? AND state NOT IN ('succeeded','failed','cancelled','orphaned')",
                (now, team_run_id),
            )
            connection.execute(
                "UPDATE worktree_bindings SET write_enabled = 0, updated_at = ? "
                "WHERE team_run_id = ?",
                (now, team_run_id),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "cancel_team_run",
                {"team_run_id": team_run_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.cancelled",
                {
                    "team_run_id": team_run_id,
                    "cancelled_by": str(cancelled_by),
                    "reason": str(reason),
                },
            )
        self._notify_activity()
        return self._require_team_run(team_run_id)

    def _require_team_run(self, team_run_id: str) -> TeamRunRecord:
        record = self.get_team_run(team_run_id)
        if record is None:
            raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
        return record

    def create_team_plan_revision(
        self,
        team_run_id: str,
        *,
        plan: Mapping[str, Any],
        created_by: str,
        command_id: str,
    ) -> TeamPlanRevisionRecord:
        """Create one immutable DRAFT revision; no revision is generated implicitly."""

        clean_plan = dict(plan)
        if not clean_plan:
            raise ValueError("Team Plan cannot be empty")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "create_team_plan"
            )
            if replay is not None:
                return self._team_plan_from_connection(
                    connection, team_run_id, int(replay["revision"])
                )
            team = self._team_row(connection, team_run_id)
            if str(team["state"]) != TeamRunState.PLANNING.value:
                raise InvalidStateTransitionError(
                    "Team Plan revisions can only be created while PLANNING"
                )
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS value "
                    "FROM team_plan_revisions WHERE team_run_id = ?",
                    (team_run_id,),
                ).fetchone()["value"]
            )
            plan_json = _json_dumps(clean_plan)
            plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO team_plan_revisions(
                    team_run_id, revision, status, plan_json, plan_hash,
                    created_by, created_at
                ) VALUES (?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    team_run_id,
                    revision,
                    plan_json,
                    plan_hash,
                    str(created_by),
                    now,
                ),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "create_team_plan",
                {"revision": revision},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.plan.created",
                {"team_run_id": team_run_id, "revision": revision},
                agent_id=str(created_by),
            )
        self._notify_activity()
        return self.get_team_plan_revision(team_run_id, revision)

    def submit_team_plan_revision(
        self,
        team_run_id: str,
        revision: int,
        *,
        command_id: str,
    ) -> TeamPlanRevisionRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "submit_team_plan"
            )
            if replay is not None:
                return self._team_plan_from_connection(
                    connection, team_run_id, int(replay["revision"])
                )
            team = self._team_row(connection, team_run_id)
            if str(team["state"]) != TeamRunState.PLANNING.value:
                raise InvalidStateTransitionError(
                    "TeamRun must be PLANNING before plan submission"
                )
            plan = self._team_plan_row(connection, team_run_id, revision)
            if str(plan["status"]) != TeamPlanStatus.DRAFT.value:
                raise InvalidStateTransitionError("Only a DRAFT Team Plan can be submitted")
            self._validate_team_plan_tasks(connection, str(team["task_list_id"]), _json_loads(plan["plan_json"], {}))
            connection.execute(
                """
                UPDATE team_plan_revisions
                SET status = 'pending_user_approval', submitted_at = ?
                WHERE team_run_id = ? AND revision = ?
                """,
                (now, team_run_id, revision),
            )
            connection.execute(
                """
                UPDATE team_runs
                SET state = 'waiting_approval', active_plan_revision = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (now, team_run_id),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "submit_team_plan",
                {"revision": revision},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.plan.submitted",
                {"team_run_id": team_run_id, "revision": revision},
            )
        self._notify_activity()
        return self.get_team_plan_revision(team_run_id, revision)

    def decide_team_plan_revision(
        self,
        team_run_id: str,
        revision: int,
        *,
        decision: str,
        decided_by: str,
        reason: str,
        command_id: str,
    ) -> TeamPlanRevisionRecord:
        """Approve or reject a pending immutable Team Plan revision."""

        normalized = str(decision).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("Team Plan decision must be approve or reject")
        command_kind = f"{normalized}_team_plan"
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, command_kind
            )
            if replay is not None:
                return self._team_plan_from_connection(
                    connection, team_run_id, int(replay["revision"])
                )
            team = self._team_row(connection, team_run_id)
            plan = self._team_plan_row(connection, team_run_id, revision)
            if str(plan["status"]) != TeamPlanStatus.PENDING_USER_APPROVAL.value:
                raise InvalidStateTransitionError(
                    "Only a pending Team Plan can be approved or rejected"
                )
            if str(team["state"]) != TeamRunState.WAITING_APPROVAL.value:
                raise InvalidStateTransitionError(
                    "TeamRun is not waiting for Team Plan approval"
                )
            status = (
                TeamPlanStatus.APPROVED.value
                if normalized == "approve"
                else TeamPlanStatus.REJECTED.value
            )
            team_state = (
                TeamRunState.RUNNING.value
                if normalized == "approve"
                else TeamRunState.PLANNING.value
            )
            active_revision = revision if normalized == "approve" else None
            if normalized == "approve":
                self._validate_team_plan_tasks(connection, str(team["task_list_id"]), _json_loads(plan["plan_json"], {}))
            connection.execute(
                """
                UPDATE team_plan_revisions
                SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?
                WHERE team_run_id = ? AND revision = ?
                """,
                (
                    status,
                    str(decided_by),
                    now,
                    str(reason),
                    team_run_id,
                    revision,
                ),
            )
            connection.execute(
                """
                UPDATE team_runs
                SET state = ?, active_plan_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (team_state, active_revision, now, team_run_id),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                command_kind,
                {"revision": revision, "status": status},
                now,
            )
            self._append_team_event(
                connection,
                team,
                f"team.plan.{status}",
                {
                    "team_run_id": team_run_id,
                    "revision": revision,
                    "status": status,
                    "reason": str(reason),
                },
                agent_id=str(decided_by),
            )
        self._notify_activity()
        return self.get_team_plan_revision(team_run_id, revision)

    def get_team_plan_revision(
        self, team_run_id: str, revision: int
    ) -> TeamPlanRevisionRecord:
        with self._lock:
            self._ensure_open()
            return self._team_plan_from_connection(
                self._connection, team_run_id, revision
            )

    def list_team_plan_revisions(
        self, team_run_id: str
    ) -> list[TeamPlanRevisionRecord]:
        self._require_team_run(team_run_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM team_plan_revisions WHERE team_run_id = ? "
                "ORDER BY revision",
                (team_run_id,),
            ).fetchall()
        return [_row_to_team_plan_revision(row) for row in rows]

    def create_team_agent(
        self,
        team_run_id: str,
        *,
        name: str,
        role: str = TeamAgentRole.TEAMMATE.value,
        model: str = "",
        capabilities: Sequence[str] = (),
        agent_id: str | None = None,
    ) -> TeamAgentRecord:
        _validate_choice("team agent role", role, TEAM_AGENT_ROLES)
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("Team agent name cannot be empty")
        identifier = agent_id or _new_id("agent")
        now = utc_now_iso()
        try:
            with self._transaction(immediate=True) as connection:
                self._team_row(connection, team_run_id)
                connection.execute(
                    """
                    INSERT INTO team_agents(
                        id, team_run_id, role, name, model,
                        capabilities_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        team_run_id,
                        role,
                        clean_name,
                        str(model),
                        _json_dumps([str(item) for item in capabilities]),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("Team agent identity already exists") from exc
        return self.get_team_agent(identifier)

    def get_team_agent(self, agent_id: str) -> TeamAgentRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM team_agents WHERE id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Team agent not found: {agent_id}")
        return _row_to_team_agent(row)

    def list_team_agents(self, team_run_id: str) -> list[TeamAgentRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM team_agents WHERE team_run_id = ? "
                "ORDER BY CASE role WHEN 'lead' THEN 0 ELSE 1 END, created_at, id",
                (team_run_id,),
            ).fetchall()
        return [_row_to_team_agent(row) for row in rows]

    def create_agent_session(
        self,
        team_run_id: str,
        agent_id: str,
        *,
        state: str = AgentSessionState.IDLE.value,
        generation: int | None = None,
        session_id: str | None = None,
    ) -> AgentSessionRecord:
        _validate_choice("agent session state", state, AGENT_SESSION_STATES)
        identifier = session_id or _new_id("session")
        now = utc_now_iso()
        try:
            with self._transaction(immediate=True) as connection:
                agent = connection.execute(
                    "SELECT team_run_id FROM team_agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if agent is None or str(agent["team_run_id"]) != team_run_id:
                    raise RecordNotFoundError(
                        f"Team agent not found in TeamRun: {agent_id}"
                    )
                active = connection.execute(
                    """
                    SELECT * FROM agent_sessions
                    WHERE agent_id = ?
                      AND state IN ('starting','idle','work','waiting','suspect')
                    """,
                    (agent_id,),
                ).fetchone()
                if active is not None:
                    if generation is not None and int(active["generation"]) != int(
                        generation
                    ):
                        raise StorageConflictError(
                            "Agent already has another active Session generation"
                        )
                    return _row_to_agent_session(active)
                next_generation = generation
                if next_generation is None:
                    next_generation = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
                            "FROM agent_sessions WHERE agent_id = ?",
                            (agent_id,),
                        ).fetchone()["value"]
                    )
                connection.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, team_run_id, agent_id, generation, state,
                        heartbeat_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        team_run_id,
                        agent_id,
                        next_generation,
                        state,
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError(
                "Agent already has an active session or generation"
            ) from exc
        return self.get_agent_session(identifier)

    def get_agent_session(self, session_id: str) -> AgentSessionRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Agent session not found: {session_id}")
        return _row_to_agent_session(row)

    def list_agent_sessions(
        self,
        team_run_id: str,
        *,
        state: str | None = None,
        role: str | None = None,
    ) -> list[AgentSessionRecord]:
        conditions = ["s.team_run_id = ?"]
        parameters: list[Any] = [team_run_id]
        if state is not None:
            _validate_choice("agent session state", state, AGENT_SESSION_STATES)
            conditions.append("s.state = ?")
            parameters.append(state)
        if role is not None:
            _validate_choice("team agent role", role, TEAM_AGENT_ROLES)
            conditions.append("a.role = ?")
            parameters.append(role)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT s.* FROM agent_sessions s
                JOIN team_agents a ON a.id = s.agent_id
                WHERE {' AND '.join(conditions)}
                ORDER BY s.created_at, s.id
                """,
                parameters,
            ).fetchall()
        return [_row_to_agent_session(row) for row in rows]

    def transition_agent_session(
        self,
        session_id: str,
        target_state: str,
        *,
        waiting_reason: str | None = None,
        failure: Mapping[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Apply one Runtime-owned AgentSession state transition."""

        _validate_choice("agent session state", target_state, AGENT_SESSION_STATES)
        allowed = {
            AgentSessionState.STARTING.value: {
                AgentSessionState.IDLE.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            },
            AgentSessionState.IDLE.value: {
                AgentSessionState.WORK.value,
                AgentSessionState.WAITING.value,
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            },
            AgentSessionState.WORK.value: {
                AgentSessionState.IDLE.value,
                AgentSessionState.WAITING.value,
                AgentSessionState.SUSPECT.value,
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
            },
            AgentSessionState.WAITING.value: {
                AgentSessionState.WORK.value,
                AgentSessionState.IDLE.value,
                AgentSessionState.SUSPECT.value,
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            },
            AgentSessionState.SUSPECT.value: {
                AgentSessionState.WORK.value,
                AgentSessionState.WAITING.value,
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
            },
        }
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT s.*, a.role AS agent_role
                FROM agent_sessions s
                JOIN team_agents a ON a.id = s.agent_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Agent session not found: {session_id}")
            current = str(row["state"])
            if current != target_state and target_state not in allowed.get(current, set()):
                raise InvalidStateTransitionError(
                    f"Invalid AgentSession transition: {current} -> {target_state}"
                )
            if (
                target_state == AgentSessionState.WORK.value
                and not row["current_attempt_id"]
                and str(row["agent_role"]) != TeamAgentRole.LEAD.value
            ):
                raise InvalidStateTransitionError(
                    "WORK AgentSession requires an active Attempt"
                )
            if target_state == AgentSessionState.IDLE.value and row[
                "current_attempt_id"
            ]:
                raise InvalidStateTransitionError(
                    "IDLE AgentSession cannot retain an active Attempt"
                )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = ?, waiting_reason = ?, failure_json = ?,
                    heartbeat_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    target_state,
                    waiting_reason
                    if target_state == AgentSessionState.WAITING.value
                    else None,
                    _json_dumps(dict(failure)) if failure is not None else None,
                    now,
                    now,
                    session_id,
                ),
            )
            team = self._team_row(connection, str(row["team_run_id"]))
            self._append_team_event(
                connection,
                team,
                "team.session.state_changed",
                {
                    "team_run_id": str(row["team_run_id"]),
                    "session_id": session_id,
                    "from": current,
                    "to": target_state,
                    "reason": waiting_reason,
                },
                agent_id=str(row["agent_id"]),
            )
        self._notify_activity()
        return self.get_agent_session(session_id)

    def claim_task_attempt(
        self,
        team_run_id: str,
        *,
        task_id: str,
        agent_id: str,
        session_id: str,
        expected_task_revision: int,
        command_id: str,
        plan_required: bool | None = None,
        attempt_base_commit: str | None = None,
    ) -> TaskAttemptRecord:
        """Atomically bind one ready Task and one idle Agent to a new Attempt."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "claim_task_attempt"
            )
            if replay is not None:
                return self._attempt_from_connection(
                    connection, str(replay["attempt_id"])
                )
            team = self._team_row(connection, team_run_id)
            if (
                str(team["state"]) != TeamRunState.RUNNING.value
                or team["active_plan_revision"] is None
            ):
                raise InvalidStateTransitionError(
                    "TeamRun needs an approved active plan before scheduling"
                )
            plan = self._team_plan_row(
                connection, team_run_id, int(team["active_plan_revision"])
            )
            if str(plan["status"]) != TeamPlanStatus.APPROVED.value:
                raise InvalidStateTransitionError("Active Team Plan is not approved")
            session = connection.execute(
                """
                SELECT s.*, a.role AS agent_role
                FROM agent_sessions s
                JOIN team_agents a ON a.id = s.agent_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise RecordNotFoundError(f"Agent session not found: {session_id}")
            if (
                str(session["team_run_id"]) != team_run_id
                or str(session["agent_id"]) != agent_id
            ):
                raise StorageConflictError("Agent session does not match TeamRun and agent")
            if str(session["state"]) != AgentSessionState.IDLE.value:
                raise StorageConflictError("Agent session is not idle")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_list_id = ? AND id = ?",
                (str(team["task_list_id"]), str(task_id)),
            ).fetchone()
            if task is None:
                raise RecordNotFoundError(f"Task not found: {task_id}")
            if int(task["revision"]) != int(expected_task_revision):
                raise StorageConflictError("Task revision conflict")
            if str(task["status"]) != TaskStatus.PENDING.value:
                raise StorageConflictError("Only a pending Task can be claimed")
            blockers = connection.execute(
                """
                SELECT d.dependency_requirement, d.blocker_id, t.status
                FROM task_dependencies d
                JOIN tasks t
                  ON t.task_list_id = d.task_list_id AND t.id = d.blocker_id
                WHERE d.task_list_id = ? AND d.blocked_id = ?
                """,
                (str(team["task_list_id"]), str(task_id)),
            ).fetchall()
            for blocker in blockers:
                requirement = str(blocker["dependency_requirement"])
                if requirement == DependencyRequirement.CANDIDATE_INTEGRATED.value:
                    integrated = connection.execute(
                        """
                        SELECT 1 FROM candidates
                        WHERE team_run_id = ? AND task_id = ?
                          AND status = 'committed' AND integrated_at IS NOT NULL
                        LIMIT 1
                        """,
                        (team_run_id, str(blocker["blocker_id"])),
                    ).fetchone()
                    if integrated is None:
                        raise StorageConflictError(
                            "Task requires a candidate integration that is not verified"
                        )
                    continue
                if str(blocker["status"]) != TaskStatus.COMPLETED.value:
                    raise StorageConflictError("Task dependencies are not complete")
            task_metadata = _json_loads(task["metadata_json"], {})
            required = requires_attempt_plan(task_metadata)
            # Callers may assert the expected mode, but cannot grant or bypass approval.
            if plan_required is not None and (
                not isinstance(plan_required, bool) or plan_required != required
            ):
                raise StorageConflictError(
                    "Attempt Plan requirement differs from Task metadata"
                )
            plan_required = required
            self._validate_team_plan_tasks(connection, str(team["task_list_id"]), _json_loads(plan["plan_json"], {}))
            resource_keys = _team_task_resource_keys(task_metadata)
            self._ensure_resources_available(
                connection,
                team_run_id,
                resource_keys,
            )
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 AS value "
                    "FROM task_attempts WHERE task_list_id = ? AND task_id = ?",
                    (str(team["task_list_id"]), str(task_id)),
                ).fetchone()["value"]
            )
            attempt_id = _new_id("attempt")
            lease_token = _new_id("lease")
            attempt_state = (
                TaskAttemptState.PLAN_REQUIRED.value
                if plan_required
                else TaskAttemptState.RUNNING.value
            )
            try:
                connection.execute(
                    """
                    INSERT INTO task_attempts(
                        id, team_run_id, task_list_id, task_id, agent_id,
                        session_id, ordinal, state, team_plan_revision,
                        attempt_base_commit, lease_token, write_enabled,
                        started_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        team_run_id,
                        str(team["task_list_id"]),
                        str(task_id),
                        agent_id,
                        session_id,
                        ordinal,
                        attempt_state,
                        int(team["active_plan_revision"]),
                        str(attempt_base_commit or team["base_commit"]),
                        lease_token,
                        now,
                        now,
                        now,
                    ),
                )
                for resource_kind, resource_key in resource_keys:
                    connection.execute(
                        """
                        INSERT INTO resource_leases(
                            id, team_run_id, attempt_id, resource_kind,
                            resource_key, generation, state, acquired_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            _new_id("resourcelease"),
                            team_run_id,
                            attempt_id,
                            resource_kind,
                            resource_key,
                            int(session["generation"]),
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE tasks
                    SET owner = ?, status = 'in_progress', revision = revision + 1,
                        updated_at = ?
                    WHERE task_list_id = ? AND id = ?
                    """,
                    (agent_id, now, str(team["task_list_id"]), str(task_id)),
                )
                connection.execute(
                    """
                    UPDATE task_lists
                    SET revision = revision + 1, updated_at = ? WHERE id = ?
                    """,
                    (now, str(team["task_list_id"])),
                )
                connection.execute(
                    """
                    UPDATE agent_sessions
                    SET state = 'work', current_attempt_id = ?,
                        heartbeat_at = ?, updated_at = ? WHERE id = ?
                    """,
                    (attempt_id, now, now, session_id),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(
                    "Task or agent already has an active Attempt"
                ) from exc
            self._insert_task_activity(
                connection,
                task_list_id=str(team["task_list_id"]),
                task_id=str(task_id),
                event_type="team_attempt_claimed",
                conversation_id=str(team["conversation_id"]),
                run_id=str(team["root_run_id"]),
                agent_id=agent_id,
                payload={"team_run_id": team_run_id, "attempt_id": attempt_id},
                created_at=now,
            )
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=agent_id,
                recipient_generation=int(session["generation"]),
                message_type="TASK_ASSIGNED",
                payload={
                    "task_revision": int(task["revision"]) + 1,
                    "attempt_ordinal": ordinal,
                    "title": str(task["subject"]),
                    "objective": str(task["description"]),
                    "shared_context": _json_loads(plan["plan_json"], {}).get(
                        "shared_context", ""
                    ),
                    "dependency_results": self._analysis_dependency_results(
                        connection, team, str(task_id)
                    ),
                    "task_kind": str(task_metadata.get("kind") or "analysis"),
                    "acceptance_criteria": task_metadata.get(
                        "acceptance_criteria", []
                    ),
                    "risk_level": str(task_metadata.get("risk_level") or "low"),
                    "write_scopes": task_metadata.get("write_scopes", []),
                    "exclusive_resources": task_metadata.get(
                        "exclusive_resources", []
                    ),
                    "plan_required": bool(plan_required),
                    "team_plan_revision": int(team["active_plan_revision"]),
                    "attempt_base_commit": str(
                        attempt_base_commit or team["base_commit"]
                    ),
                    "budget": task_metadata.get("budget", {}),
                    "validation_commands": task_metadata.get(
                        "validation_commands", []
                    ),
                    "worktree_id": None,
                },
                dedupe_key=f"assign:{attempt_id}",
                task_id=str(task_id),
                attempt_id=attempt_id,
                causation_id=command_id,
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "claim_task_attempt",
                {"attempt_id": attempt_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.attempt.assigned",
                {
                    "team_run_id": team_run_id,
                    "task_id": str(task_id),
                    "attempt_id": attempt_id,
                    "agent_id": agent_id,
                    "state": attempt_state,
                },
                agent_id=agent_id,
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    @staticmethod
    def _ensure_resources_available(
        connection: sqlite3.Connection,
        team_run_id: str,
        requested: Sequence[tuple[str, str]],
    ) -> None:
        if not requested:
            return
        held = connection.execute(
            """
            SELECT resource_kind, resource_key FROM resource_leases
            WHERE team_run_id = ? AND state != 'released'
            """,
            (team_run_id,),
        ).fetchall()
        for requested_kind, requested_key in requested:
            for row in held:
                held_kind = str(row["resource_kind"])
                held_key = str(row["resource_key"])
                if _team_resources_overlap(
                    requested_kind,
                    requested_key,
                    held_kind,
                    held_key,
                ):
                    raise StorageConflictError(
                        f"Team resource is already leased: {requested_kind}:{requested_key}"
                    )

    def get_task_attempt(self, attempt_id: str) -> TaskAttemptRecord:
        with self._lock:
            self._ensure_open()
            return self._attempt_from_connection(self._connection, attempt_id)

    def record_team_base_confirmation(
        self,
        team_run_id: str,
        *,
        base_commit: str,
        source_head: str,
        source_dirty: bool,
        status_hash: str,
        confirmed_by: str,
        command_id: str,
    ) -> dict[str, Any]:
        """Persist the exact clean/dirty source snapshot accepted for Worktrees."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "confirm_team_base"
            )
            if replay is not None:
                return dict(replay)
            team = self._team_row(connection, team_run_id)
            if str(team["base_commit"]) != str(base_commit):
                raise StorageConflictError("Confirmed base does not match TeamRun base")
            result = {
                "team_run_id": team_run_id,
                "base_commit": str(base_commit),
                "source_head": str(source_head),
                "source_dirty": bool(source_dirty),
                "status_hash": str(status_hash),
                "confirmed_by": str(confirmed_by),
                "confirmed_at": now,
            }
            connection.execute(
                """
                INSERT INTO team_base_confirmations(
                    team_run_id, base_commit, source_head, source_dirty,
                    status_hash, confirmed_by, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_run_id) DO UPDATE SET
                    base_commit = excluded.base_commit,
                    source_head = excluded.source_head,
                    source_dirty = excluded.source_dirty,
                    status_hash = excluded.status_hash,
                    confirmed_by = excluded.confirmed_by,
                    confirmed_at = excluded.confirmed_at
                """,
                (
                    team_run_id,
                    str(base_commit),
                    str(source_head),
                    int(source_dirty),
                    str(status_hash),
                    str(confirmed_by),
                    now,
                ),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "confirm_team_base",
                result,
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.base.confirmed",
                result,
            )
        self._notify_activity()
        return result

    def get_team_base_confirmation(self, team_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM team_base_confirmations WHERE team_run_id = ?",
                (team_run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "team_run_id": str(row["team_run_id"]),
            "base_commit": str(row["base_commit"]),
            "source_head": str(row["source_head"]),
            "source_dirty": bool(row["source_dirty"]),
            "status_hash": str(row["status_hash"]),
            "confirmed_by": str(row["confirmed_by"]),
            "confirmed_at": str(row["confirmed_at"]),
        }

    def create_worktree_binding(
        self,
        attempt_id: str,
        *,
        path: str,
        branch: str,
        base_commit: str,
        head_commit: str,
        fingerprint: str,
        generation: int,
        write_scopes: Sequence[str],
        command_id: str,
    ) -> WorktreeBindingRecord:
        """Bind an already-created Worktree and open writes only when allowed."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "bind_worktree"
            )
            if replay is not None:
                return self._worktree_from_connection(
                    connection, str(replay["worktree_id"])
                )
            team = self._team_row(connection, team_run_id)
            if (
                str(team["state"]) != TeamRunState.RUNNING.value
                or team["active_plan_revision"] is None
                or int(team["active_plan_revision"])
                != int(attempt["team_plan_revision"])
            ):
                raise InvalidStateTransitionError(
                    "Worktree requires the Attempt's approved active Team Plan"
                )
            if str(attempt["attempt_base_commit"]) != str(base_commit):
                raise StorageConflictError("Worktree base does not match Attempt base")
            if str(head_commit) != str(base_commit):
                raise StorageConflictError("New Worktree HEAD must equal Attempt base")
            session = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            if session is None or int(session["generation"]) != int(generation):
                raise StorageConflictError("Worktree session generation is stale")
            if str(session["current_attempt_id"] or "") != attempt_id:
                raise StorageConflictError("Worktree session is not bound to Attempt")
            if attempt["cancel_requested_at"] is not None:
                raise InvalidStateTransitionError("Cancelled Attempt cannot bind Worktree")
            normalized_scopes = _normalize_write_scopes(write_scopes)
            binding_id = _new_id("worktree")
            write_enabled = str(attempt["state"]) == TaskAttemptState.RUNNING.value
            try:
                connection.execute(
                    """
                    INSERT INTO worktree_bindings(
                        id, team_run_id, attempt_id, agent_id, session_id,
                        generation, path, branch, base_commit, head_commit,
                        fingerprint, state, write_scopes_json, write_enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        team_run_id,
                        attempt_id,
                        str(attempt["agent_id"]),
                        str(attempt["session_id"]),
                        int(generation),
                        str(path),
                        str(branch),
                        str(base_commit),
                        str(head_commit),
                        str(fingerprint),
                        _json_dumps(normalized_scopes),
                        int(write_enabled),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(
                    "Attempt, Worktree path, or branch is already bound"
                ) from exc
            connection.execute(
                "UPDATE task_attempts SET write_enabled = ?, updated_at = ? WHERE id = ?",
                (int(write_enabled), now, attempt_id),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "bind_worktree",
                {"worktree_id": binding_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.worktree.bound",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "worktree_id": binding_id,
                    "path": str(path),
                    "branch": str(branch),
                    "write_enabled": write_enabled,
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_worktree_binding(binding_id)

    def get_worktree_binding(self, worktree_id: str) -> WorktreeBindingRecord:
        with self._lock:
            return self._worktree_from_connection(self._connection, worktree_id)

    def get_attempt_worktree_binding(
        self, attempt_id: str
    ) -> WorktreeBindingRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _row_to_worktree_binding(row) if row is not None else None

    def list_worktree_bindings(self, team_run_id: str) -> list[WorktreeBindingRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM worktree_bindings WHERE team_run_id = ? "
                "ORDER BY created_at, id",
                (team_run_id,),
            ).fetchall()
        return [_row_to_worktree_binding(row) for row in rows]

    def mark_worktree_cleaned(self, worktree_id: str) -> WorktreeBindingRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM worktree_bindings WHERE id = ?", (worktree_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Worktree binding not found: {worktree_id}")
            if str(row["state"]) == "cleaned":
                return _row_to_worktree_binding(row)
            if str(row["state"]) != "retained":
                raise InvalidStateTransitionError(
                    "Only retained candidate Worktrees can be cleaned"
                )
            connection.execute(
                """
                UPDATE worktree_bindings SET state = 'cleaned', write_enabled = 0,
                    updated_at = ? WHERE id = ?
                """,
                (now, worktree_id),
            )
            team = self._team_row(connection, str(row["team_run_id"]))
            self._append_team_event(
                connection,
                team,
                "team.worktree.cleaned",
                {
                    "team_run_id": str(row["team_run_id"]),
                    "worktree_id": worktree_id,
                    "path": str(row["path"]),
                    "branch_retained": str(row["branch"]),
                },
                agent_id=str(row["agent_id"]),
            )
        self._notify_activity()
        return self.get_worktree_binding(worktree_id)

    def create_attempt_plan(
        self,
        attempt_id: str,
        *,
        summary: str,
        planned_files: Sequence[str],
        planned_commands: Sequence[str],
        planned_tests: Sequence[str],
        write_scopes: Sequence[str],
        risk_level: str,
        created_by: str,
        command_id: str,
    ) -> AttemptPlanRecord:
        """Create a new immutable draft revision after assignment or rejection."""

        now = utc_now_iso()
        scopes = _normalize_write_scopes(write_scopes)
        normalized_files = _normalize_write_scopes(planned_files)
        if scopes and any(
            not _scope_is_within(item, scopes) for item in normalized_files
        ):
            raise StorageConflictError(
                "Planned file is outside the Attempt Plan write scope"
            )
        clean_risk = _normalize_risk_level(risk_level)
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "create_attempt_plan"
            )
            if replay is not None:
                return self._attempt_plan_from_connection(
                    connection, str(replay["attempt_plan_id"])
                )
            if str(attempt["state"]) != TaskAttemptState.PLAN_REQUIRED.value:
                raise InvalidStateTransitionError(
                    "Attempt Plan can only be created while PLAN_REQUIRED"
                )
            if str(attempt["agent_id"]) != str(created_by):
                raise StorageConflictError(
                    "Only the assigned Teammate can create an Attempt Plan"
                )
            pending = connection.execute(
                """
                SELECT 1 FROM attempt_plans
                WHERE attempt_id = ? AND status IN ('draft', 'submitted')
                """,
                (attempt_id,),
            ).fetchone()
            if pending is not None:
                raise StorageConflictError(
                    "Attempt already has a draft or submitted plan revision"
                )
            binding = connection.execute(
                "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if binding is None or str(binding["state"]) != "active":
                raise StorageConflictError("Attempt Plan requires an active Worktree")
            self._validate_attempt_plan_scope(
                connection,
                attempt,
                scopes=scopes,
                risk_level=clean_risk,
            )
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS value "
                    "FROM attempt_plans WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()["value"]
            )
            scope_hash = _attempt_scope_hash(
                task_id=str(attempt["task_id"]),
                team_plan_revision=int(attempt["team_plan_revision"]),
                base_commit=str(attempt["attempt_base_commit"]),
                write_scopes=scopes,
                risk_level=clean_risk,
            )
            plan_id = _new_id("attemptplan")
            connection.execute(
                """
                INSERT INTO attempt_plans(
                    id, team_run_id, attempt_id, revision, status, summary,
                    planned_files_json, planned_commands_json,
                    planned_tests_json, write_scopes_json, scope_hash,
                    risk_level, team_plan_revision, base_commit,
                    worktree_fingerprint, created_by, created_at
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    team_run_id,
                    attempt_id,
                    revision,
                    str(summary).strip(),
                    _json_dumps(normalized_files),
                    _json_dumps([str(item) for item in planned_commands]),
                    _json_dumps([str(item) for item in planned_tests]),
                    _json_dumps(scopes),
                    scope_hash,
                    clean_risk,
                    int(attempt["team_plan_revision"]),
                    str(attempt["attempt_base_commit"]),
                    str(binding["fingerprint"]),
                    str(created_by),
                    now,
                ),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "create_attempt_plan",
                {"attempt_plan_id": plan_id},
                now,
            )
        return self.get_attempt_plan(plan_id)

    def submit_attempt_plan(
        self,
        attempt_id: str,
        revision: int,
        *,
        command_id: str,
    ) -> AttemptPlanRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "submit_attempt_plan"
            )
            if replay is not None:
                return self._attempt_plan_from_connection(
                    connection, str(replay["attempt_plan_id"])
                )
            plan = self._attempt_plan_row(connection, attempt_id, revision)
            if str(plan["status"]) != AttemptPlanStatus.DRAFT.value:
                raise InvalidStateTransitionError(
                    "Only a draft Attempt Plan can be submitted"
                )
            if str(attempt["state"]) != TaskAttemptState.PLAN_REQUIRED.value:
                raise InvalidStateTransitionError("Attempt is not awaiting a plan")
            connection.execute(
                "UPDATE attempt_plans SET status = 'submitted', submitted_at = ? "
                "WHERE id = ?",
                (now, str(plan["id"])),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = 'plan_submitted',
                    write_enabled = 0, updated_at = ? WHERE id = ?
                """,
                (now, attempt_id),
            )
            connection.execute(
                "UPDATE worktree_bindings SET write_enabled = 0, updated_at = ? "
                "WHERE attempt_id = ?",
                (now, attempt_id),
            )
            team = self._team_row(connection, team_run_id)
            lead_session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE agent_id = ? "
                "AND state NOT IN ('lost','failed','shutdown')",
                (str(team["lead_agent_id"]),),
            ).fetchone()
            if lead_session is None:
                raise StorageConflictError("Active Lead Session is missing")
            self._insert_team_message(
                connection,
                team=team,
                sender_type="teammate",
                sender_agent_id=str(attempt["agent_id"]),
                recipient_type="lead",
                recipient_agent_id=str(team["lead_agent_id"]),
                recipient_generation=int(lead_session["generation"]),
                message_type="ATTEMPT_PLAN_SUBMITTED",
                payload={
                    "attempt_plan_revision": int(revision),
                    "summary": str(plan["summary"]),
                    "planned_files": _json_loads(plan["planned_files_json"], []),
                    "planned_commands": _json_loads(
                        plan["planned_commands_json"], []
                    ),
                    "planned_tests": _json_loads(plan["planned_tests_json"], []),
                    "scope_hash": str(plan["scope_hash"]),
                    "risk_level": str(plan["risk_level"]),
                },
                dedupe_key=f"attempt-plan-submitted:{attempt_id}:{revision}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                causation_id=command_id,
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "submit_attempt_plan",
                {"attempt_plan_id": str(plan["id"])},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.attempt_plan.submitted",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "revision": int(revision),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_attempt_plan(str(plan["id"]))

    def decide_attempt_plan(
        self,
        attempt_id: str,
        revision: int,
        *,
        decision: str,
        decided_by: str,
        reason: str,
        command_id: str,
        validated_worktree_fingerprint: str | None = None,
    ) -> AttemptPlanRecord:
        normalized = str(decision).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("Attempt Plan decision must be approve or reject")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "decide_attempt_plan"
            )
            if replay is not None:
                return self._attempt_plan_from_connection(
                    connection, str(replay["attempt_plan_id"])
                )
            plan = self._attempt_plan_row(connection, attempt_id, revision)
            if str(plan["status"]) != AttemptPlanStatus.SUBMITTED.value:
                raise InvalidStateTransitionError(
                    "Only a submitted Attempt Plan can be decided"
                )
            if str(attempt["state"]) != TaskAttemptState.PLAN_SUBMITTED.value:
                raise InvalidStateTransitionError(
                    "Attempt is not awaiting a plan decision"
                )
            team = self._team_row(connection, team_run_id)
            if str(decided_by) not in {str(team["lead_agent_id"]), "user"}:
                raise StorageConflictError(
                    "Only the Lead or user can decide an Attempt Plan"
                )
            session = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            target_status = (
                AttemptPlanStatus.APPROVED.value
                if normalized == "approve"
                else AttemptPlanStatus.REJECTED.value
            )
            if normalized == "approve":
                if not validated_worktree_fingerprint:
                    raise StorageConflictError(
                        "Runtime must validate Worktree binding before approval"
                    )
                self._validate_attempt_plan_for_approval(
                    connection,
                    team,
                    attempt,
                    plan,
                    validated_worktree_fingerprint=validated_worktree_fingerprint,
                )
                attempt_state = TaskAttemptState.RUNNING.value
                write_enabled = 1
                session_state = AgentSessionState.WORK.value
                waiting_reason = None
            else:
                attempt_state = TaskAttemptState.PLAN_REQUIRED.value
                write_enabled = 0
                session_state = AgentSessionState.WORK.value
                waiting_reason = None
            connection.execute(
                """
                UPDATE attempt_plans
                SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?
                WHERE id = ?
                """,
                (
                    target_status,
                    str(decided_by),
                    now,
                    str(reason),
                    str(plan["id"]),
                ),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = ?, write_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_state, write_enabled, now, attempt_id),
            )
            connection.execute(
                """
                UPDATE worktree_bindings
                SET write_enabled = ?,
                    write_scopes_json = CASE WHEN ? = 1 THEN ?
                        ELSE write_scopes_json END,
                    updated_at = ?
                WHERE attempt_id = ? AND state = 'active'
                """,
                (
                    write_enabled,
                    write_enabled,
                    str(plan["write_scopes_json"]),
                    now,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE agent_sessions SET state = ?, waiting_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_state, waiting_reason, now, str(attempt["session_id"])),
            )
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="ATTEMPT_PLAN_DECISION",
                payload={
                    "attempt_plan_revision": int(revision),
                    "decision": "approved" if normalized == "approve" else "rejected",
                    "reason": str(reason),
                    "write_enabled": bool(write_enabled),
                    "approved_scopes": _json_loads(plan["write_scopes_json"], []),
                    "related_team_plan_revision": int(plan["team_plan_revision"]),
                },
                dedupe_key=f"attempt-plan-decision:{attempt_id}:{revision}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                causation_id=command_id,
                priority="control",
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "decide_attempt_plan",
                {"attempt_plan_id": str(plan["id"])},
                now,
            )
            self._append_team_event(
                connection,
                team,
                f"team.attempt_plan.{target_status}",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "revision": int(revision),
                    "decided_by": str(decided_by),
                    "reason": str(reason),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_attempt_plan(str(plan["id"]))

    def get_attempt_plan(self, attempt_plan_id: str) -> AttemptPlanRecord:
        with self._lock:
            return self._attempt_plan_from_connection(
                self._connection, attempt_plan_id
            )

    def list_attempt_plans(self, attempt_id: str) -> list[AttemptPlanRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM attempt_plans WHERE attempt_id = ? ORDER BY revision",
                (attempt_id,),
            ).fetchall()
        return [_row_to_attempt_plan(row) for row in rows]

    @staticmethod
    def _attempt_plan_row(
        connection: sqlite3.Connection, attempt_id: str, revision: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attempt_plans WHERE attempt_id = ? AND revision = ?",
            (attempt_id, int(revision)),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(
                f"Attempt Plan not found: {attempt_id} revision {revision}"
            )
        return row

    @staticmethod
    def _attempt_plan_from_connection(
        connection: sqlite3.Connection, attempt_plan_id: str
    ) -> AttemptPlanRecord:
        row = connection.execute(
            "SELECT * FROM attempt_plans WHERE id = ?", (attempt_plan_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Attempt Plan not found: {attempt_plan_id}")
        return _row_to_attempt_plan(row)

    @staticmethod
    def _validate_attempt_plan_scope(
        connection: sqlite3.Connection,
        attempt: sqlite3.Row,
        *,
        scopes: Sequence[str],
        risk_level: str,
    ) -> None:
        task = connection.execute(
            "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
            (str(attempt["task_list_id"]), str(attempt["task_id"])),
        ).fetchone()
        assert task is not None
        metadata = _json_loads(task["metadata_json"], {})
        task_scopes = _normalize_write_scopes(metadata.get("write_scopes", []))
        approved_scopes = task_scopes
        approved_risk = _normalize_risk_level(
            str(metadata.get("risk_level") or "low")
        )
        team_plan = connection.execute(
            """
            SELECT plan_json FROM team_plan_revisions
            WHERE team_run_id = ? AND revision = ? AND status = 'approved'
            """,
            (
                str(attempt["team_run_id"]),
                int(attempt["team_plan_revision"]),
            ),
        ).fetchone()
        if team_plan is None:
            raise InvalidStateTransitionError("Attempt Team Plan is not approved")
        plan_payload = _json_loads(team_plan["plan_json"], {})
        structured_tasks = [
            item
            for item in plan_payload.get("tasks", [])
            if isinstance(item, Mapping)
        ]
        matching = next(
            (
                item
                for item in structured_tasks
                if str(item.get("task_id") or item.get("id") or "")
                == str(attempt["task_id"])
            ),
            None,
        )
        if structured_tasks and matching is None:
            raise StorageConflictError("Task is missing from approved Team Plan")
        if matching is not None:
            approved_scopes = _normalize_write_scopes(
                matching.get("write_scopes", [])
            )
            approved_risk = _normalize_risk_level(
                str(matching.get("risk_level") or "low")
            )
            if task_scopes != approved_scopes or _normalize_risk_level(
                str(metadata.get("risk_level") or "low")
            ) != approved_risk:
                raise StorageConflictError(
                    "Task scope or risk changed after Team Plan approval"
                )
        if approved_scopes and any(
            not _scope_is_within(scope, approved_scopes) for scope in scopes
        ):
            raise StorageConflictError(
                "Attempt Plan expands the Team Plan write scope"
            )
        if _risk_rank(risk_level) > _risk_rank(approved_risk):
            raise StorageConflictError("Attempt Plan increases approved risk")

    def _validate_attempt_plan_for_approval(
        self,
        connection: sqlite3.Connection,
        team: sqlite3.Row,
        attempt: sqlite3.Row,
        plan: sqlite3.Row,
        *,
        validated_worktree_fingerprint: str,
    ) -> None:
        if (
            str(team["state"]) != TeamRunState.RUNNING.value
            or team["active_plan_revision"] is None
            or int(team["active_plan_revision"]) != int(plan["team_plan_revision"])
            or int(attempt["team_plan_revision"]) != int(plan["team_plan_revision"])
        ):
            raise InvalidStateTransitionError(
                "Attempt Plan belongs to a stale Team Plan revision"
            )
        if str(attempt["attempt_base_commit"]) != str(plan["base_commit"]):
            raise StorageConflictError("Attempt base changed after plan submission")
        scopes = _json_loads(plan["write_scopes_json"], [])
        self._validate_attempt_plan_scope(
            connection,
            attempt,
            scopes=scopes,
            risk_level=str(plan["risk_level"]),
        )
        expected_hash = _attempt_scope_hash(
            task_id=str(attempt["task_id"]),
            team_plan_revision=int(plan["team_plan_revision"]),
            base_commit=str(plan["base_commit"]),
            write_scopes=scopes,
            risk_level=str(plan["risk_level"]),
        )
        if expected_hash != str(plan["scope_hash"]):
            raise StorageConflictError("Attempt Plan scope hash is stale")
        binding = connection.execute(
            "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
            (str(attempt["id"]),),
        ).fetchone()
        if (
            binding is None
            or str(binding["state"]) != "active"
            or str(binding["fingerprint"]) != str(plan["worktree_fingerprint"])
            or str(binding["fingerprint"]) != validated_worktree_fingerprint
            or str(binding["base_commit"]) != str(plan["base_commit"])
        ):
            raise StorageConflictError("Worktree binding changed after plan submission")
        leases = connection.execute(
            "SELECT state FROM resource_leases WHERE attempt_id = ?",
            (str(attempt["id"]),),
        ).fetchall()
        if not leases or any(str(item["state"]) != "active" for item in leases):
            raise StorageConflictError("Attempt resource lease is no longer active")

    def submit_candidate(
        self,
        attempt_id: str,
        *,
        candidate_id: str,
        summary: str,
        changed_files: Sequence[str],
        untracked_files: Sequence[str],
        diff_ref: str,
        diff_hash: str,
        base_commit: str,
        worktree_head: str,
        tests_reported: Sequence[str],
        known_risks: Sequence[str],
        submitted_by: str,
        command_id: str,
    ) -> CandidateRecord:
        """Persist one frozen candidate and revoke Teammate write access."""

        now = utc_now_iso()
        changed = [str(item).replace("\\", "/") for item in changed_files]
        untracked = [str(item).replace("\\", "/") for item in untracked_files]
        all_paths = list(dict.fromkeys([*changed, *untracked]))
        if not all_paths:
            raise ValueError("Candidate must contain at least one changed file")
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "submit_candidate"
            )
            if replay is not None:
                return self._candidate_from_connection(
                    connection, str(replay["candidate_id"])
                )
            if str(attempt["state"]) not in {
                TaskAttemptState.RUNNING.value,
                TaskAttemptState.WAITING.value,
            }:
                raise InvalidStateTransitionError(
                    "Candidate can only be submitted from a working Attempt"
                )
            if str(attempt["agent_id"]) != str(submitted_by):
                raise StorageConflictError("Only the assigned Teammate can submit")
            binding = connection.execute(
                "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if binding is None or str(binding["state"]) != "active":
                raise StorageConflictError("Candidate requires an active Worktree")
            task = connection.execute(
                "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
                (str(attempt["task_list_id"]), str(attempt["task_id"])),
            ).fetchone()
            if task is None:
                raise RecordNotFoundError(f"Task not found: {attempt['task_id']}")
            task_metadata = _json_loads(task["metadata_json"], {})
            user_approval_required = (
                str(task_metadata.get("risk_level") or "low").strip().lower()
                == "high"
            )
            if (
                str(binding["base_commit"]) != str(base_commit)
                or str(binding["head_commit"]) != str(worktree_head)
                or str(attempt["attempt_base_commit"]) != str(base_commit)
            ):
                raise StorageConflictError("Candidate Git baseline is stale")
            scopes = _json_loads(binding["write_scopes_json"], [])
            leases = connection.execute(
                "SELECT resource_kind, resource_key, state FROM resource_leases "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
            repository_lease = any(
                str(item["state"]) == "active"
                and str(item["resource_kind"]) == "repository"
                for item in leases
            )
            if not repository_lease and any(
                not _scope_is_within(item, scopes) for item in all_paths
            ):
                raise StorageConflictError(
                    "Candidate contains files outside the approved write scope"
                )
            active = connection.execute(
                """
                SELECT 1 FROM candidates WHERE attempt_id = ?
                AND status NOT IN ('rework', 'validation_failed', 'committed')
                """,
                (attempt_id,),
            ).fetchone()
            if active is not None:
                raise StorageConflictError("Attempt already has an active Candidate")
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS value "
                    "FROM candidates WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO candidates(
                    id, team_run_id, task_id, attempt_id, revision, status,
                    summary, changed_files_json, untracked_files_json,
                    diff_ref, diff_hash, base_commit, worktree_head,
                    tests_reported_json, known_risks_json, submitted_by,
                    submitted_at, user_approval_required
                ) VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    team_run_id,
                    str(attempt["task_id"]),
                    attempt_id,
                    revision,
                    str(summary).strip(),
                    _json_dumps(changed),
                    _json_dumps(untracked),
                    str(diff_ref),
                    str(diff_hash),
                    str(base_commit),
                    str(worktree_head),
                    _json_dumps([str(item) for item in tests_reported]),
                    _json_dumps([str(item) for item in known_risks]),
                    str(submitted_by),
                    now,
                    int(user_approval_required),
                ),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = 'candidate_submitted',
                    write_enabled = 0, updated_at = ? WHERE id = ?
                """,
                (now, attempt_id),
            )
            connection.execute(
                "UPDATE worktree_bindings SET write_enabled = 0, updated_at = ? "
                "WHERE attempt_id = ?",
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions SET state = 'waiting',
                    waiting_reason = 'candidate_review', updated_at = ? WHERE id = ?
                """,
                (now, str(attempt["session_id"])),
            )
            team = self._team_row(connection, team_run_id)
            lead_session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE agent_id = ? "
                "AND state NOT IN ('lost','failed','shutdown')",
                (str(team["lead_agent_id"]),),
            ).fetchone()
            if lead_session is None:
                raise StorageConflictError("Active Lead Session is missing")
            self._insert_team_message(
                connection,
                team=team,
                sender_type="teammate",
                sender_agent_id=str(attempt["agent_id"]),
                recipient_type="lead",
                recipient_agent_id=str(team["lead_agent_id"]),
                recipient_generation=int(lead_session["generation"]),
                message_type="CANDIDATE_SUBMITTED",
                payload={
                    "candidate_id": candidate_id,
                    "summary": str(summary).strip(),
                    "changed_files": changed,
                    "untracked_files": untracked,
                    "diff_ref": str(diff_ref),
                    "diff_hash": str(diff_hash),
                    "base_commit": str(base_commit),
                    "worktree_head": str(worktree_head),
                    "tests_reported": [str(item) for item in tests_reported],
                    "known_risks": [str(item) for item in known_risks],
                },
                artifact_refs=[str(diff_ref)],
                dedupe_key=f"candidate-submitted:{candidate_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                causation_id=command_id,
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "submit_candidate",
                {"candidate_id": candidate_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.candidate.submitted",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "candidate_id": candidate_id,
                    "revision": revision,
                    "diff_hash": str(diff_hash),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_candidate(candidate_id)

    def complete_analysis_attempt(
        self,
        attempt_id: str,
        *,
        summary: str,
        submitted_by: str,
        command_id: str,
    ) -> TaskAttemptRecord:
        """Complete one read-only analysis Attempt without a Worktree or Candidate."""

        clean_summary = str(summary).strip()
        if not clean_summary:
            raise ValueError("Analysis result summary cannot be empty")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "complete_analysis_attempt"
            )
            if replay is not None:
                return self._attempt_from_connection(connection, attempt_id)
            if (str(attempt["state"]) != TaskAttemptState.RUNNING.value
                    or attempt["cancel_requested_at"] or attempt["result_unknown"]):
                raise InvalidStateTransitionError(
                    "Analysis result requires a working Attempt"
                )
            if str(attempt["agent_id"]) != str(submitted_by):
                raise StorageConflictError(
                    "Only the assigned Teammate can submit the analysis result"
                )
            task = connection.execute(
                "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
                (str(attempt["task_list_id"]), str(attempt["task_id"])),
            ).fetchone()
            if task is None:
                raise RecordNotFoundError(f"Task not found: {attempt['task_id']}")
            metadata = _json_loads(task["metadata_json"], {})
            if str(metadata.get("kind") or "analysis").lower() != "analysis":
                raise InvalidStateTransitionError(
                    "Code Attempts must submit a frozen Candidate"
                )
            connection.execute(
                """
                UPDATE task_attempts
                SET state = 'succeeded', write_enabled = 0,
                    worker_exited_at = COALESCE(worker_exited_at, ?),
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, now, attempt_id),
            )
            connection.execute(
                "UPDATE resource_leases SET state = 'released', released_at = ? "
                "WHERE attempt_id = ? AND state != 'released'",
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'idle', current_attempt_id = NULL,
                    waiting_reason = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, str(attempt["session_id"])),
            )
            connection.execute(
                """
                UPDATE tasks SET status = 'completed', revision = revision + 1,
                    updated_at = ? WHERE task_list_id = ? AND id = ?
                """,
                (now, str(attempt["task_list_id"]), str(attempt["task_id"])),
            )
            connection.execute(
                "UPDATE task_lists SET revision = revision + 1, updated_at = ? "
                "WHERE id = ?",
                (now, str(attempt["task_list_id"])),
            )
            team = self._team_row(connection, team_run_id)
            lead_session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE agent_id = ? "
                "AND state NOT IN ('lost','failed','shutdown')",
                (str(team["lead_agent_id"]),),
            ).fetchone()
            if lead_session is None:
                raise StorageConflictError("Active Lead Session is missing")
            self._insert_team_message(
                connection,
                team=team,
                sender_type="teammate",
                sender_agent_id=str(attempt["agent_id"]),
                recipient_type="lead",
                recipient_agent_id=str(team["lead_agent_id"]),
                recipient_generation=int(lead_session["generation"]),
                message_type="ANALYSIS_RESULT",
                payload={"summary": clean_summary},
                dedupe_key=f"analysis-result:{attempt_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                causation_id=command_id,
                priority="control",
                created_at=now,
            )
            self._advance_team_after_task_completion(connection, team, now)
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "complete_analysis_attempt",
                {"attempt_id": attempt_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.analysis.completed",
                {
                    "team_run_id": team_run_id,
                    "task_id": str(attempt["task_id"]),
                    "attempt_id": attempt_id,
                    "summary": clean_summary,
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def decide_candidate_review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reviewed_by: str,
        reason: str,
        validated_diff_hash: str,
        command_id: str,
    ) -> CandidateRecord:
        normalized = str(decision).strip().lower()
        if normalized not in {"accept", "rework"}:
            raise ValueError("Candidate decision must be accept or rework")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            team_run_id = str(candidate["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "decide_candidate_review"
            )
            if replay is not None:
                return self._candidate_from_connection(connection, candidate_id)
            current_status = str(candidate["status"])
            allowed = {CandidateStatus.SUBMITTED.value}
            if normalized == "rework":
                allowed.add(CandidateStatus.VALIDATION_FAILED.value)
            if current_status not in allowed:
                raise InvalidStateTransitionError(
                    "Candidate is not awaiting this review decision"
                )
            if str(candidate["diff_hash"]) != str(validated_diff_hash):
                raise StorageConflictError("Candidate changed after submission")
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?",
                (str(candidate["attempt_id"]),),
            ).fetchone()
            assert attempt is not None
            team = self._team_row(connection, team_run_id)
            if str(reviewed_by) != str(team["lead_agent_id"]):
                raise StorageConflictError("Only the Team Lead can review Candidate")
            binding = connection.execute(
                "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
                (str(attempt["id"]),),
            ).fetchone()
            if binding is None or str(binding["state"]) != "active":
                raise StorageConflictError("Candidate Worktree is not active")
            leases = connection.execute(
                "SELECT state FROM resource_leases WHERE attempt_id = ?",
                (str(attempt["id"]),),
            ).fetchall()
            if not leases or any(str(item["state"]) != "active" for item in leases):
                raise StorageConflictError("Candidate resource lease is not active")
            if normalized == "accept":
                candidate_status = CandidateStatus.ACCEPTED.value
                requires_user = bool(candidate["user_approval_required"])
                attempt_state = (
                    TaskAttemptState.WAITING.value
                    if requires_user
                    else TaskAttemptState.VALIDATING.value
                )
                write_enabled = 0
                session_state = AgentSessionState.WAITING.value
                waiting_reason = (
                    "user_candidate_approval"
                    if requires_user
                    else "runtime_validation"
                )
            else:
                self._ensure_attempt_plan_still_approved(connection, attempt)
                candidate_status = CandidateStatus.REWORK.value
                attempt_state = TaskAttemptState.RUNNING.value
                write_enabled = 1
                session_state = AgentSessionState.WORK.value
                waiting_reason = None
            connection.execute(
                """
                UPDATE candidates SET status = ?, reviewed_by = ?, reviewed_at = ?,
                    review_reason = ? WHERE id = ?
                """,
                (
                    candidate_status,
                    str(reviewed_by),
                    now,
                    str(reason),
                    candidate_id,
                ),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = ?, write_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_state, write_enabled, now, str(attempt["id"])),
            )
            connection.execute(
                "UPDATE worktree_bindings SET write_enabled = ?, updated_at = ? "
                "WHERE attempt_id = ?",
                (write_enabled, now, str(attempt["id"])),
            )
            connection.execute(
                """
                UPDATE agent_sessions SET state = ?, waiting_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_state, waiting_reason, now, str(attempt["session_id"])),
            )
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="REVIEW_DECISION",
                payload={
                    "candidate_id": candidate_id,
                    "decision": "accepted" if normalized == "accept" else "rework",
                    "reason": str(reason),
                    "write_enabled": bool(write_enabled),
                },
                dedupe_key=f"candidate-review:{candidate_id}:{current_status}",
                task_id=str(attempt["task_id"]),
                attempt_id=str(attempt["id"]),
                causation_id=command_id,
                priority="control",
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "decide_candidate_review",
                {"candidate_id": candidate_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                f"team.candidate.{candidate_status}",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": str(attempt["id"]),
                    "candidate_id": candidate_id,
                    "reviewed_by": str(reviewed_by),
                    "reason": str(reason),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_candidate(candidate_id)

    def decide_candidate_user_approval(
        self,
        candidate_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str,
        command_id: str,
    ) -> CandidateRecord:
        """Record the separate user gate required by a high-risk Candidate."""

        normalized = str(decision).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("Candidate user decision must be approve or reject")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            team_run_id = str(candidate["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "candidate_user_approval"
            )
            if replay is not None:
                return self._candidate_from_connection(connection, candidate_id)
            if not bool(candidate["user_approval_required"]):
                raise InvalidStateTransitionError(
                    "Candidate does not require a separate user approval"
                )
            if str(candidate["status"]) != CandidateStatus.ACCEPTED.value:
                raise InvalidStateTransitionError(
                    "Candidate is not awaiting high-risk user approval"
                )
            if candidate["user_decision"] is not None:
                raise InvalidStateTransitionError(
                    "Candidate user approval is immutable once recorded"
                )
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?",
                (str(candidate["attempt_id"]),),
            ).fetchone()
            assert attempt is not None
            team = self._team_row(connection, team_run_id)
            if normalized == "approve":
                candidate_status = CandidateStatus.ACCEPTED.value
                attempt_state = TaskAttemptState.VALIDATING.value
                session_state = AgentSessionState.WAITING.value
                waiting_reason = "runtime_validation"
                write_enabled = 0
            else:
                self._ensure_attempt_plan_still_approved(connection, attempt)
                candidate_status = CandidateStatus.REWORK.value
                attempt_state = TaskAttemptState.RUNNING.value
                session_state = AgentSessionState.WORK.value
                waiting_reason = None
                write_enabled = 1
            connection.execute(
                """
                UPDATE candidates
                SET status = ?, user_decision = ?, user_decided_by = ?,
                    user_decided_at = ?, user_decision_reason = ?
                WHERE id = ?
                """,
                (
                    candidate_status,
                    "approved" if normalized == "approve" else "rejected",
                    str(decided_by),
                    now,
                    str(reason),
                    candidate_id,
                ),
            )
            connection.execute(
                "UPDATE task_attempts SET state = ?, write_enabled = ?, updated_at = ? "
                "WHERE id = ?",
                (attempt_state, write_enabled, now, str(attempt["id"])),
            )
            connection.execute(
                "UPDATE worktree_bindings SET write_enabled = ?, updated_at = ? "
                "WHERE attempt_id = ?",
                (write_enabled, now, str(attempt["id"])),
            )
            connection.execute(
                "UPDATE agent_sessions SET state = ?, waiting_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (session_state, waiting_reason, now, str(attempt["session_id"])),
            )
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="REVIEW_DECISION",
                payload={
                    "candidate_id": candidate_id,
                    "decision": (
                        "user_approved" if normalized == "approve" else "rework"
                    ),
                    "reason": str(reason),
                    "write_enabled": bool(write_enabled),
                },
                dedupe_key=f"candidate-user-decision:{candidate_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=str(attempt["id"]),
                causation_id=command_id,
                priority="control",
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "candidate_user_approval",
                {"candidate_id": candidate_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                f"team.candidate.user_{normalized}d",
                {
                    "team_run_id": team_run_id,
                    "candidate_id": candidate_id,
                    "decided_by": str(decided_by),
                    "reason": str(reason),
                },
            )
        self._notify_activity()
        return self.get_candidate(candidate_id)

    def _ensure_attempt_plan_still_approved(
        self, connection: sqlite3.Connection, attempt: sqlite3.Row
    ) -> None:
        task = connection.execute(
            "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
            (str(attempt["task_list_id"]), str(attempt["task_id"])),
        ).fetchone()
        assert task is not None
        metadata = _json_loads(task["metadata_json"], {})
        required = requires_attempt_plan(metadata)
        if not required:
            return
        plan = connection.execute(
            """
            SELECT * FROM attempt_plans
            WHERE attempt_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (str(attempt["id"]),),
        ).fetchone()
        if plan is None or str(plan["status"]) != AttemptPlanStatus.APPROVED.value:
            raise StorageConflictError("Attempt Plan is not approved for rework")
        if (
            int(plan["team_plan_revision"]) != int(attempt["team_plan_revision"])
            or str(plan["base_commit"]) != str(attempt["attempt_base_commit"])
        ):
            raise StorageConflictError("Attempt Plan belongs to a stale execution base")
        scopes = _json_loads(plan["write_scopes_json"], [])
        risk_level = str(plan["risk_level"])
        self._validate_attempt_plan_scope(
            connection,
            attempt,
            scopes=scopes,
            risk_level=risk_level,
        )
        expected_hash = _attempt_scope_hash(
            task_id=str(attempt["task_id"]),
            team_plan_revision=int(attempt["team_plan_revision"]),
            base_commit=str(attempt["attempt_base_commit"]),
            write_scopes=scopes,
            risk_level=risk_level,
        )
        if expected_hash != str(plan["scope_hash"]):
            raise StorageConflictError("Attempt Plan scope hash is stale")
        binding = connection.execute(
            "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
            (str(attempt["id"]),),
        ).fetchone()
        if (
            binding is None
            or str(binding["base_commit"]) != str(plan["base_commit"])
        ):
            raise StorageConflictError("Attempt Plan Worktree binding is stale")

    def mark_candidate_validating(self, candidate_id: str) -> CandidateRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            if str(candidate["status"]) == CandidateStatus.VALIDATING.value:
                return _row_to_candidate(candidate)
            if str(candidate["status"]) != CandidateStatus.ACCEPTED.value:
                raise InvalidStateTransitionError("Candidate was not accepted")
            connection.execute(
                "UPDATE candidates SET status = 'validating' WHERE id = ?",
                (candidate_id,),
            )
            connection.execute(
                "UPDATE task_attempts SET state = 'validating', write_enabled = 0, "
                "updated_at = ? WHERE id = ?",
                (now, str(candidate["attempt_id"])),
            )
        return self.get_candidate(candidate_id)

    def begin_validation_run(
        self, candidate_id: str, *, command: str
    ) -> ValidationRunRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            if str(candidate["status"]) != CandidateStatus.VALIDATING.value:
                raise InvalidStateTransitionError("Candidate is not validating")
            validation_id = _new_id("validation")
            connection.execute(
                """
                INSERT INTO validation_runs(
                    id, team_run_id, attempt_id, candidate_id, command,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    validation_id,
                    str(candidate["team_run_id"]),
                    str(candidate["attempt_id"]),
                    candidate_id,
                    str(command),
                    now,
                ),
            )
        return self.get_validation_run(validation_id)

    def finish_validation_run(
        self,
        validation_id: str,
        *,
        status: str,
        exit_code: int,
        output_ref: str,
        duration_ms: int,
    ) -> ValidationRunRecord:
        if status not in {"passed", "failed", "timed_out"}:
            raise ValueError(f"Unsupported validation status: {status}")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM validation_runs WHERE id = ?",
                (validation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Validation run not found: {validation_id}")
            if str(row["status"]) != "running":
                return self._validation_from_connection(connection, validation_id)
            connection.execute(
                """
                UPDATE validation_runs SET status = ?, exit_code = ?,
                    output_ref = ?, duration_ms = ?, finished_at = ? WHERE id = ?
                """,
                (status, int(exit_code), str(output_ref), int(duration_ms), now, validation_id),
            )
        return self.get_validation_run(validation_id)

    def mark_candidate_validation_failed(
        self,
        candidate_id: str,
        *,
        summary: str,
        validation_run_id: str,
    ) -> CandidateRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            if str(candidate["status"]) != CandidateStatus.VALIDATING.value:
                raise InvalidStateTransitionError("Candidate is not validating")
            connection.execute(
                "UPDATE candidates SET status = 'validation_failed' WHERE id = ?",
                (candidate_id,),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = 'validation_failed',
                    write_enabled = 0, updated_at = ? WHERE id = ?
                """,
                (now, str(candidate["attempt_id"])),
            )
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?",
                (str(candidate["attempt_id"]),),
            ).fetchone()
            assert attempt is not None
            team = self._team_row(connection, str(candidate["team_run_id"]))
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="VALIDATION_RESULT",
                payload={
                    "candidate_id": candidate_id,
                    "validation_run_id": str(validation_run_id),
                    "status": "failed",
                    "summary": str(summary),
                },
                dedupe_key=f"validation-result:{candidate_id}:failed",
                task_id=str(attempt["task_id"]),
                attempt_id=str(attempt["id"]),
                created_at=now,
                priority="control",
            )
            lead_session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE agent_id = ? "
                "AND state NOT IN ('lost','failed','shutdown')",
                (str(team["lead_agent_id"]),),
            ).fetchone()
            if lead_session is not None:
                self._insert_team_message(
                    connection,
                    team=team,
                    sender_type="runtime",
                    recipient_type="lead",
                    recipient_agent_id=str(team["lead_agent_id"]),
                    recipient_generation=int(lead_session["generation"]),
                    message_type="VALIDATION_RESULT",
                    payload={
                        "candidate_id": candidate_id,
                        "validation_run_id": str(validation_run_id),
                        "status": "failed",
                        "summary": str(summary),
                    },
                    dedupe_key=f"validation-result:{candidate_id}:failed:lead",
                    task_id=str(attempt["task_id"]),
                    attempt_id=str(attempt["id"]),
                    created_at=now,
                    priority="control",
                )
            self._append_team_event(
                connection,
                team,
                "team.candidate.validation_failed",
                {
                    "team_run_id": str(candidate["team_run_id"]),
                    "candidate_id": candidate_id,
                    "attempt_id": str(candidate["attempt_id"]),
                    "summary": str(summary),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_candidate(candidate_id)

    def complete_candidate_commit(
        self,
        candidate_id: str,
        *,
        commit_hash: str,
        head_commit: str,
        worktree_fingerprint: str,
        validation_run_id: str,
    ) -> CandidateRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            if str(candidate["status"]) == CandidateStatus.COMMITTED.value:
                if str(candidate["commit_hash"]) != str(commit_hash):
                    raise StorageConflictError("Candidate was committed with another hash")
                return _row_to_candidate(candidate)
            if str(candidate["status"]) != CandidateStatus.VALIDATING.value:
                raise InvalidStateTransitionError("Candidate is not ready to commit")
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?",
                (str(candidate["attempt_id"]),),
            ).fetchone()
            assert attempt is not None
            connection.execute(
                """
                UPDATE candidates SET status = 'committed', commit_hash = ?,
                    committed_at = ? WHERE id = ?
                """,
                (str(commit_hash), now, candidate_id),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = 'succeeded', write_enabled = 0,
                    worker_exited_at = COALESCE(worker_exited_at, ?),
                    finished_at = ?, updated_at = ? WHERE id = ?
                """,
                (now, now, now, str(attempt["id"])),
            )
            connection.execute(
                """
                UPDATE worktree_bindings SET state = 'retained', write_enabled = 0,
                    head_commit = ?, fingerprint = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (str(head_commit), str(worktree_fingerprint), now, str(attempt["id"])),
            )
            connection.execute(
                "UPDATE resource_leases SET state = 'released', released_at = ? "
                "WHERE attempt_id = ? AND state != 'released'",
                (now, str(attempt["id"])),
            )
            connection.execute(
                """
                UPDATE agent_sessions SET state = 'idle', current_attempt_id = NULL,
                    waiting_reason = NULL, updated_at = ? WHERE id = ?
                """,
                (now, str(attempt["session_id"])),
            )
            connection.execute(
                """
                UPDATE tasks SET status = 'completed', revision = revision + 1,
                    updated_at = ? WHERE task_list_id = ? AND id = ?
                """,
                (now, str(attempt["task_list_id"]), str(attempt["task_id"])),
            )
            connection.execute(
                "UPDATE task_lists SET revision = revision + 1, updated_at = ? "
                "WHERE id = ?",
                (now, str(attempt["task_list_id"])),
            )
            team = self._team_row(connection, str(candidate["team_run_id"]))
            self._advance_team_after_task_completion(connection, team, now)
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="VALIDATION_RESULT",
                payload={
                    "candidate_id": candidate_id,
                    "validation_run_id": str(validation_run_id),
                    "status": "passed",
                    "summary": "Runtime validation passed and candidate commit was created",
                    "commit_hash": str(commit_hash),
                },
                dedupe_key=f"validation-result:{candidate_id}:passed",
                task_id=str(attempt["task_id"]),
                attempt_id=str(attempt["id"]),
                created_at=now,
                priority="control",
            )
            lead_session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE agent_id = ? "
                "AND state NOT IN ('lost','failed','shutdown')",
                (str(team["lead_agent_id"]),),
            ).fetchone()
            if lead_session is not None:
                self._insert_team_message(
                    connection,
                    team=team,
                    sender_type="runtime",
                    recipient_type="lead",
                    recipient_agent_id=str(team["lead_agent_id"]),
                    recipient_generation=int(lead_session["generation"]),
                    message_type="VALIDATION_RESULT",
                    payload={
                        "candidate_id": candidate_id,
                        "validation_run_id": str(validation_run_id),
                        "status": "passed",
                        "summary": (
                            "Runtime validation passed and candidate commit was created"
                        ),
                        "commit_hash": str(commit_hash),
                    },
                    dedupe_key=f"validation-result:{candidate_id}:passed:lead",
                    task_id=str(attempt["task_id"]),
                    attempt_id=str(attempt["id"]),
                    created_at=now,
                    priority="control",
                )
            self._append_team_event(
                connection,
                team,
                "team.candidate.committed",
                {
                    "team_run_id": str(candidate["team_run_id"]),
                    "candidate_id": candidate_id,
                    "attempt_id": str(attempt["id"]),
                    "commit_hash": str(commit_hash),
                    "manual_integration_required": True,
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_candidate(candidate_id)

    def mark_candidate_commit_unknown(
        self, candidate_id: str, *, reason: str
    ) -> CandidateRecord:
        """Quarantine a Worktree when Git committed but persistence was uncertain."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            connection.execute(
                """
                UPDATE task_attempts SET state = 'orphaned', write_enabled = 0,
                    result_unknown = 1, error_json = ?, updated_at = ?
                WHERE id = ? AND state != 'succeeded'
                """,
                (
                    _json_dumps({"type": "commit_result_unknown", "message": reason}),
                    now,
                    str(candidate["attempt_id"]),
                ),
            )
            connection.execute(
                """
                UPDATE worktree_bindings SET state = 'orphaned', write_enabled = 0,
                    frozen_reason = ?, updated_at = ? WHERE attempt_id = ?
                """,
                (str(reason), now, str(candidate["attempt_id"])),
            )
            connection.execute(
                "UPDATE resource_leases SET state = 'orphaned' "
                "WHERE attempt_id = ? AND state = 'active'",
                (str(candidate["attempt_id"]),),
            )
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> CandidateRecord:
        with self._lock:
            return self._candidate_from_connection(self._connection, candidate_id)

    @staticmethod
    def _candidate_from_connection(
        connection: sqlite3.Connection, candidate_id: str
    ) -> CandidateRecord:
        row = connection.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
        return _row_to_candidate(row)

    def list_candidates(self, team_run_id: str) -> list[CandidateRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM candidates WHERE team_run_id = ? "
                "ORDER BY submitted_at, id",
                (team_run_id,),
            ).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def get_validation_run(self, validation_id: str) -> ValidationRunRecord:
        with self._lock:
            return self._validation_from_connection(
                self._connection, validation_id
            )

    @staticmethod
    def _validation_from_connection(
        connection: sqlite3.Connection, validation_id: str
    ) -> ValidationRunRecord:
        row = connection.execute(
            "SELECT * FROM validation_runs WHERE id = ?", (validation_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Validation run not found: {validation_id}")
        return _row_to_validation_run(row)

    def list_validation_runs(self, candidate_id: str) -> list[ValidationRunRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM validation_runs WHERE candidate_id = ? "
                "ORDER BY started_at, id",
                (candidate_id,),
            ).fetchall()
        return [_row_to_validation_run(row) for row in rows]

    def record_manual_integration_check(
        self,
        team_run_id: str,
        *,
        target_ref: str,
        target_commit: str,
        checks: Sequence[Mapping[str, Any]],
        verified_by: str,
        command_id: str,
    ) -> dict[str, Any]:
        """Record read-only Git verification and unlock integrated dependencies."""

        now = utc_now_iso()
        normalized_checks = [dict(item) for item in checks]
        status_value = (
            "verified"
            if normalized_checks
            and all(bool(item.get("integrated")) for item in normalized_checks)
            else "partial"
        )
        with self._transaction(immediate=True) as connection:
            replay = self._team_command_result(
                connection, team_run_id, command_id, "verify_manual_integration"
            )
            if replay is not None:
                return self.get_manual_integration_check(str(replay["check_id"]))
            team = self._team_row(connection, team_run_id)
            check_id = _new_id("integrationcheck")
            connection.execute(
                """
                INSERT INTO manual_integration_checks(
                    id, team_run_id, target_ref, target_commit, status,
                    checks_json, verified_by, verified_at, command_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    team_run_id,
                    str(target_ref),
                    str(target_commit),
                    status_value,
                    _json_dumps(normalized_checks),
                    str(verified_by),
                    now,
                    command_id,
                ),
            )
            for item in normalized_checks:
                if not bool(item.get("integrated")):
                    continue
                connection.execute(
                    """
                    UPDATE candidates SET integrated_commit = ?, integrated_at = ?
                    WHERE id = ? AND team_run_id = ? AND status = 'committed'
                    """,
                    (str(target_commit), now, str(item["candidate_id"]), team_run_id),
                )
            pending_tasks = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM tasks
                    WHERE task_list_id = ? AND status NOT IN ('completed', 'cancelled')
                    """,
                    (str(team["task_list_id"]),),
                ).fetchone()["value"]
            )
            unintegrated = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM candidates
                    WHERE team_run_id = ? AND status = 'committed'
                      AND integrated_at IS NULL
                    """,
                    (team_run_id,),
                ).fetchone()["value"]
            )
            next_state = (
                TeamRunState.COMPLETED.value
                if pending_tasks == 0 and unintegrated == 0
                else TeamRunState.RUNNING.value
            )
            connection.execute(
                "UPDATE team_runs SET state = ?, updated_at = ? WHERE id = ?",
                (next_state, now, team_run_id),
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "verify_manual_integration",
                {"check_id": check_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.integration.verified",
                {
                    "team_run_id": team_run_id,
                    "check_id": check_id,
                    "target_ref": str(target_ref),
                    "target_commit": str(target_commit),
                    "status": status_value,
                    "pending_tasks": pending_tasks,
                    "unintegrated_candidates": unintegrated,
                    "state": next_state,
                },
            )
        self._notify_activity()
        return self.get_manual_integration_check(check_id)

    def get_manual_integration_check(self, check_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM manual_integration_checks WHERE id = ?", (check_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Integration check not found: {check_id}")
        return {
            "id": str(row["id"]),
            "team_run_id": str(row["team_run_id"]),
            "target_ref": str(row["target_ref"]),
            "target_commit": str(row["target_commit"]),
            "status": str(row["status"]),
            "checks": _json_loads(row["checks_json"], []),
            "verified_by": str(row["verified_by"]),
            "verified_at": str(row["verified_at"]),
        }

    def list_manual_integration_checks(self, team_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            ids = [
                str(row["id"])
                for row in self._connection.execute(
                    "SELECT id FROM manual_integration_checks WHERE team_run_id = ? "
                    "ORDER BY verified_at, id",
                    (team_run_id,),
                ).fetchall()
            ]
        return [self.get_manual_integration_check(item) for item in ids]

    @staticmethod
    def _worktree_from_connection(
        connection: sqlite3.Connection, worktree_id: str
    ) -> WorktreeBindingRecord:
        row = connection.execute(
            "SELECT * FROM worktree_bindings WHERE id = ?", (worktree_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Worktree binding not found: {worktree_id}")
        return _row_to_worktree_binding(row)

    def begin_tool_execution(
        self,
        attempt_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        risk: str,
        is_write: bool,
        input: Mapping[str, Any],
        worktree_id: str | None,
        trace_id: str | None = None,
    ) -> ToolExecutionRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            execution_id = _new_id("toolexec")
            try:
                connection.execute(
                    """
                    INSERT INTO tool_executions(
                        id, team_run_id, agent_id, session_id, task_id,
                        attempt_id, worktree_id, tool_call_id, trace_id,
                        plan_revision, tool_name, risk, status, is_write,
                        input_json, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        execution_id,
                        str(attempt["team_run_id"]),
                        str(attempt["agent_id"]),
                        str(attempt["session_id"]),
                        str(attempt["task_id"]),
                        attempt_id,
                        worktree_id,
                        str(tool_call_id),
                        trace_id,
                        int(attempt["team_plan_revision"]),
                        str(tool_name),
                        str(risk),
                        int(is_write),
                        _json_dumps(dict(input)),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError("Tool call id was already recorded") from exc
        return self.get_tool_execution(execution_id)

    def finish_tool_execution(
        self,
        execution_id: str,
        *,
        status: str,
        output_ref: str | None = None,
        error: str | None = None,
        result_unknown: bool = False,
    ) -> ToolExecutionRecord:
        if status not in {"completed", "failed", "blocked", "scope_violation"}:
            raise ValueError(f"Unsupported tool execution status: {status}")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM tool_executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Tool execution not found: {execution_id}")
            if str(row["status"]) != "running":
                return self._tool_execution_from_connection(connection, execution_id)
            connection.execute(
                """
                UPDATE tool_executions
                SET status = ?, output_ref = ?, error = ?, result_unknown = ?,
                    finished_at = ? WHERE id = ?
                """,
                (status, output_ref, error, int(result_unknown), now, execution_id),
            )
        return self.get_tool_execution(execution_id)

    def get_tool_execution(self, execution_id: str) -> ToolExecutionRecord:
        with self._lock:
            return self._tool_execution_from_connection(
                self._connection, execution_id
            )

    @staticmethod
    def _tool_execution_from_connection(
        connection: sqlite3.Connection, execution_id: str
    ) -> ToolExecutionRecord:
        row = connection.execute(
            "SELECT * FROM tool_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Tool execution not found: {execution_id}")
        return _row_to_tool_execution(row)

    def list_tool_executions(self, attempt_id: str) -> list[ToolExecutionRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tool_executions WHERE attempt_id = ? "
                "ORDER BY started_at, id",
                (attempt_id,),
            ).fetchall()
        return [_row_to_tool_execution(row) for row in rows]

    def freeze_attempt_for_scope_violation(
        self,
        attempt_id: str,
        *,
        execution_id: str,
        tool_call_id: str,
        attempted: str,
        allowed_scopes: Sequence[str],
        reason: str,
    ) -> TaskAttemptRecord:
        """Atomically revoke writes while retaining the Attempt and its leases."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            if str(attempt["state"]) in {
                TaskAttemptState.SUCCEEDED.value,
                TaskAttemptState.FAILED.value,
                TaskAttemptState.CANCELLED.value,
                TaskAttemptState.ORPHANED.value,
            }:
                return _row_to_task_attempt(attempt)
            execution = connection.execute(
                "SELECT * FROM tool_executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None or str(execution["attempt_id"]) != attempt_id:
                raise RecordNotFoundError(
                    f"Tool execution not found in Attempt: {execution_id}"
                )
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'scope_violation', error = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (str(reason), now, execution_id),
            )
            connection.execute(
                """
                UPDATE task_attempts SET state = 'waiting', write_enabled = 0,
                    updated_at = ? WHERE id = ?
                """,
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE worktree_bindings
                SET state = 'frozen', write_enabled = 0, frozen_reason = ?,
                    updated_at = ? WHERE attempt_id = ?
                """,
                (str(reason), now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'waiting', waiting_reason = 'scope_violation',
                    updated_at = ? WHERE id = ?
                """,
                (now, str(attempt["session_id"])),
            )
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            team = self._team_row(connection, str(attempt["team_run_id"]))
            payload = {
                "attempted": str(attempted),
                "allowed_scopes": list(allowed_scopes),
                "tool_call_id": str(tool_call_id),
                "action_taken": "attempt_paused_worktree_frozen",
                "reason": str(reason),
            }
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="SCOPE_VIOLATION",
                payload=payload,
                dedupe_key=f"scope-violation:{attempt_id}:{tool_call_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                created_at=now,
                priority="control",
            )
            self._append_team_event(
                connection,
                team,
                "team.scope_violation",
                {"team_run_id": str(attempt["team_run_id"]), **payload},
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def freeze_attempt_for_unknown_write(
        self,
        attempt_id: str,
        *,
        execution_id: str,
        tool_call_id: str,
        reason: str,
    ) -> TaskAttemptRecord:
        """Quarantine a write whose handler exited without a trustworthy result."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            if str(attempt["state"]) in {
                TaskAttemptState.SUCCEEDED.value,
                TaskAttemptState.FAILED.value,
                TaskAttemptState.CANCELLED.value,
                TaskAttemptState.ORPHANED.value,
            }:
                return _row_to_task_attempt(attempt)
            execution = connection.execute(
                "SELECT * FROM tool_executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if execution is None or str(execution["attempt_id"]) != attempt_id:
                raise RecordNotFoundError(
                    f"Tool execution not found in Attempt: {execution_id}"
                )
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'failed', result_unknown = 1, error = ?,
                    finished_at = ? WHERE id = ? AND status = 'running'
                """,
                (str(reason), now, execution_id),
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET state = 'waiting', write_enabled = 0, result_unknown = 1,
                    error_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    _json_dumps(
                        {"type": "unknown_write_result", "message": str(reason)}
                    ),
                    now,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE worktree_bindings
                SET state = 'frozen', write_enabled = 0, frozen_reason = ?,
                    updated_at = ? WHERE attempt_id = ?
                """,
                ("unknown_write_result", now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'waiting', waiting_reason = 'unknown_write_result',
                    updated_at = ? WHERE id = ?
                """,
                (now, str(attempt["session_id"])),
            )
            session = connection.execute(
                "SELECT generation FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            team = self._team_row(connection, str(attempt["team_run_id"]))
            payload = {
                "reason_code": "unknown_write_result",
                "reason": str(reason),
                "effective_scope": "attempt",
            }
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="SYSTEM_ERROR",
                payload=payload,
                dedupe_key=f"unknown-write:{attempt_id}:{tool_call_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                created_at=now,
                priority="control",
            )
            self._append_team_event(
                connection,
                team,
                "team.attempt.recovery_required",
                {
                    "team_run_id": str(attempt["team_run_id"]),
                    "attempt_id": attempt_id,
                    "reason_code": "unknown_write_result",
                    "tool_call_id": str(tool_call_id),
                    "result_unknown": True,
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def resume_task_attempt(
        self,
        attempt_id: str,
        *,
        resumed_by: str,
        reason: str,
        command_id: str,
        validated_worktree_fingerprint: str | None,
        acknowledge_unknown_result: bool = False,
        replacement_session_id: str | None = None,
        replacement_worktree_fingerprint: str | None = None,
    ) -> TaskAttemptRecord:
        """Atomically reopen a verified waiting Attempt without replaying tools."""

        clean_reason = str(reason).strip()
        if not clean_reason:
            raise ValueError("Attempt recovery reason is required")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "resume_task_attempt"
            )
            if replay is not None:
                return self._attempt_from_connection(connection, attempt_id)
            team = self._team_row(connection, team_run_id)
            if (
                str(team["state"]) != TeamRunState.RUNNING.value
                or team["active_plan_revision"] is None
            ):
                raise InvalidStateTransitionError(
                    "TeamRun must be running with an approved active plan"
                )
            attempt_error = _json_loads(attempt["error_json"], {})
            if attempt_error.get("type") == "team_plan_change_required":
                raise StorageConflictError(
                    "This Attempt needs a user-approved plan change; safety recovery cannot change its scope"
                )
            legacy_restart = (
                str(attempt["state"]) == TaskAttemptState.ORPHANED.value
                and str(attempt_error.get("type") or "") == "service_restart"
            )
            if (
                str(attempt["state"]) != TaskAttemptState.WAITING.value
                and not legacy_restart
            ):
                raise InvalidStateTransitionError(
                    "Only a waiting or service-restart orphaned Attempt can be resumed"
                )
            active_revision = int(team["active_plan_revision"])
            if active_revision != int(attempt["team_plan_revision"]):
                raise StorageConflictError("Attempt Team Plan revision is no longer active")
            if str(attempt["attempt_base_commit"]) != str(team["base_commit"]):
                raise StorageConflictError("Attempt base commit no longer matches TeamRun")
            plan = self._team_plan_row(connection, team_run_id, active_revision)
            if str(plan["status"]) != TeamPlanStatus.APPROVED.value:
                raise StorageConflictError("Active Team Plan is not approved")
            self._ensure_attempt_plan_still_approved(connection, attempt)
            if bool(attempt["result_unknown"]) and not acknowledge_unknown_result:
                raise StorageConflictError(
                    "Unknown write result must be acknowledged before recovery"
                )
            original_session = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            if original_session is None:
                raise RecordNotFoundError(
                    f"Agent session not found: {attempt['session_id']}"
                )
            replacing_session = replacement_session_id is not None
            if replacing_session:
                if (
                    str(original_session["state"]) != AgentSessionState.LOST.value
                    or str(attempt_error.get("type") or "") != "service_restart"
                ):
                    raise StorageConflictError(
                        "Only a service-restart LOST Session can be replaced"
                    )
                session = connection.execute(
                    "SELECT * FROM agent_sessions WHERE id = ?",
                    (str(replacement_session_id),),
                ).fetchone()
                if session is None:
                    raise RecordNotFoundError(
                        f"Replacement Agent session not found: {replacement_session_id}"
                    )
                if (
                    str(session["team_run_id"]) != team_run_id
                    or str(session["agent_id"]) != str(attempt["agent_id"])
                    or str(session["state"]) != AgentSessionState.IDLE.value
                ):
                    raise StorageConflictError(
                        "Replacement Session is not an idle generation of this Agent"
                    )
            else:
                session = original_session
                if str(session["state"]) != AgentSessionState.WAITING.value:
                    raise StorageConflictError("Attempt Session is not waiting")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_list_id = ? AND id = ?",
                (str(attempt["task_list_id"]), str(attempt["task_id"])),
            ).fetchone()
            if task is None:
                raise RecordNotFoundError(f"Task not found: {attempt['task_id']}")
            if (
                str(task["status"]) != TaskStatus.IN_PROGRESS.value
                or str(task["owner"] or "") != str(attempt["agent_id"])
            ):
                raise StorageConflictError("Task is no longer owned by this Attempt")
            assignment = connection.execute(
                """
                SELECT payload_json FROM team_messages
                WHERE attempt_id = ? AND type = 'TASK_ASSIGNED'
                ORDER BY created_at, id LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            if assignment is None or int(
                _json_loads(assignment["payload_json"], {}).get("task_revision", -1)
            ) != int(task["revision"]):
                raise StorageConflictError("Task revision changed after Attempt assignment")
            other_task_attempt = connection.execute(
                """
                SELECT 1 FROM task_attempts
                WHERE task_list_id = ? AND task_id = ? AND id != ?
                  AND state NOT IN ('succeeded','failed','cancelled','orphaned')
                LIMIT 1
                """,
                (str(attempt["task_list_id"]), str(attempt["task_id"]), attempt_id),
            ).fetchone()
            other_agent_attempt = connection.execute(
                """
                SELECT 1 FROM task_attempts
                WHERE agent_id = ? AND id != ?
                  AND state NOT IN ('succeeded','failed','cancelled','orphaned')
                LIMIT 1
                """,
                (str(attempt["agent_id"]), attempt_id),
            ).fetchone()
            if other_task_attempt is not None or other_agent_attempt is not None:
                raise StorageConflictError("Task or Agent already has another active Attempt")
            metadata = _json_loads(task["metadata_json"], {})
            is_code = str(metadata.get("kind") or "analysis").lower() == "code"
            binding = connection.execute(
                "SELECT * FROM worktree_bindings WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if is_code:
                if binding is None:
                    raise StorageConflictError("Code Attempt has no Worktree binding")
                recoverable_binding_states = {"active", "frozen"}
                if replacing_session:
                    recoverable_binding_states.add("orphaned")
                if str(binding["state"]) not in recoverable_binding_states:
                    raise StorageConflictError("Worktree binding is not recoverable")
                if not validated_worktree_fingerprint or str(
                    binding["fingerprint"]
                ) != str(validated_worktree_fingerprint):
                    raise StorageConflictError(
                        "Worktree fingerprint was not freshly validated"
                    )
                if (
                    str(binding["session_id"]) != str(original_session["id"])
                    or int(binding["generation"])
                    != int(original_session["generation"])
                ):
                    raise StorageConflictError("Worktree Session binding is stale")
                if replacing_session and not replacement_worktree_fingerprint:
                    raise StorageConflictError(
                        "Replacement Worktree fingerprint was not validated"
                    )
            expected_resources = _team_task_resource_keys(metadata)
            leases = connection.execute(
                "SELECT * FROM resource_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
            lease_states = {"active", "orphaned"} if replacing_session else {"active"}
            if expected_resources and (
                not leases
                or any(str(item["state"]) not in lease_states for item in leases)
            ):
                raise StorageConflictError("Attempt resource lease is not recoverable")
            held_elsewhere = connection.execute(
                """
                SELECT resource_kind, resource_key FROM resource_leases
                WHERE team_run_id = ? AND attempt_id != ? AND state != 'released'
                """,
                (team_run_id, attempt_id),
            ).fetchall()
            for requested_kind, requested_key in expected_resources:
                if any(
                    _team_resources_overlap(
                        requested_kind,
                        requested_key,
                        str(item["resource_kind"]),
                        str(item["resource_key"]),
                    )
                    for item in held_elsewhere
                ):
                    raise StorageConflictError(
                        f"Team resource is already leased: {requested_kind}:{requested_key}"
                    )
            if str(attempt_error.get("type") or "") == "service_restart":
                reason_code = "service_restart"
            elif bool(attempt["result_unknown"]):
                reason_code = "unknown_write_result"
            else:
                reason_code = str(session["waiting_reason"] or "manual_recovery")
            write_enabled = int(is_code)
            connection.execute(
                """
                UPDATE task_attempts
                SET session_id = ?, state = 'running', write_enabled = ?,
                    result_unknown = 0, worker_exited_at = NULL,
                    finished_at = NULL, error_json = NULL, updated_at = ?
                WHERE id = ?
                """,
                (str(session["id"]), write_enabled, now, attempt_id),
            )
            if binding is not None:
                if replacing_session:
                    connection.execute(
                        """
                        UPDATE worktree_bindings
                        SET session_id = ?, generation = ?, fingerprint = ?,
                            state = 'active', write_enabled = ?,
                            frozen_reason = NULL, updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (
                            str(session["id"]),
                            int(session["generation"]),
                            str(replacement_worktree_fingerprint),
                            write_enabled,
                            now,
                            attempt_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE worktree_bindings
                        SET state = 'active', write_enabled = ?, frozen_reason = NULL,
                            updated_at = ? WHERE attempt_id = ?
                        """,
                        (write_enabled, now, attempt_id),
                    )
            if replacing_session:
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET generation = ?, state = 'active', released_at = NULL
                    WHERE attempt_id = ?
                    """,
                    (int(session["generation"]), attempt_id),
                )
            recovery_checkpoint_id = None
            if replacing_session:
                checkpoint = connection.execute(
                    """
                    SELECT * FROM agent_session_checkpoints
                    WHERE session_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (str(original_session["id"]),),
                ).fetchone()
                if checkpoint is not None:
                    recovery_checkpoint_id = _new_id("sessioncp")
                    checkpoint_metadata = _json_loads(
                        checkpoint["metadata_json"], {}
                    )
                    checkpoint_metadata.update(
                        {
                            "recovered_from_session_id": str(
                                original_session["id"]
                            ),
                            "recovered_from_generation": int(
                                original_session["generation"]
                            ),
                        }
                    )
                    revision = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(revision), 0) + 1 AS value "
                            "FROM agent_session_checkpoints WHERE session_id = ?",
                            (str(session["id"]),),
                        ).fetchone()["value"]
                    )
                    connection.execute(
                        """
                        INSERT INTO agent_session_checkpoints(
                            id, session_id, generation, revision, messages_json,
                            context_json, metadata_json, safe_boundary, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            recovery_checkpoint_id,
                            str(session["id"]),
                            int(session["generation"]),
                            revision,
                            str(checkpoint["messages_json"]),
                            str(checkpoint["context_json"]),
                            _json_dumps(checkpoint_metadata),
                            "recovery_checkpoint",
                            now,
                        ),
                    )
                pending_messages = connection.execute(
                    """
                    SELECT id FROM team_messages
                    WHERE team_run_id = ? AND recipient_agent_id = ?
                      AND recipient_generation = ? AND acked_at IS NULL
                    ORDER BY CASE priority WHEN 'control' THEN 0 ELSE 1 END,
                             sequence_no, id
                    """,
                    (
                        team_run_id,
                        str(attempt["agent_id"]),
                        int(original_session["generation"]),
                    ),
                ).fetchall()
                next_sequence = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence_no), 0) AS value
                        FROM team_messages
                        WHERE team_run_id = ? AND recipient_agent_id = ?
                          AND recipient_generation = ?
                        """,
                        (
                            team_run_id,
                            str(attempt["agent_id"]),
                            int(session["generation"]),
                        ),
                    ).fetchone()["value"]
                )
                for pending_message in pending_messages:
                    next_sequence += 1
                    connection.execute(
                        """
                        UPDATE team_messages
                        SET recipient_generation = ?, sequence_no = ?,
                            delivered_at = NULL, last_delivery_error = NULL
                        WHERE id = ?
                        """,
                        (
                            int(session["generation"]),
                            next_sequence,
                            str(pending_message["id"]),
                        ),
                    )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'work', current_attempt_id = ?, waiting_reason = NULL,
                    checkpoint_id = COALESCE(?, checkpoint_id),
                    heartbeat_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    attempt_id,
                    recovery_checkpoint_id,
                    now,
                    now,
                    str(session["id"]),
                ),
            )
            payload = {
                "reason_code": reason_code,
                "reason": clean_reason,
                "do_not_replay": bool(attempt["result_unknown"]),
                "runtime_checks": [
                    "team_plan",
                    "task_revision",
                    "attempt_plan",
                    "resource_leases",
                    "worktree_binding" if is_code else "analysis_task",
                ],
            }
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="ATTEMPT_RESUMED",
                payload=payload,
                dedupe_key=f"attempt-resumed:{command_id}",
                task_id=str(attempt["task_id"]),
                attempt_id=attempt_id,
                created_at=now,
                priority="control",
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "resume_task_attempt",
                {"attempt_id": attempt_id, "session_id": str(session["id"])},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.attempt.resumed",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "resumed_by": str(resumed_by),
                    "reason": clean_reason,
                    "reason_code": reason_code,
                    "result_unknown_acknowledged": bool(attempt["result_unknown"]),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def team_command_recorded(
        self, team_run_id: str, command_id: str, kind: str
    ) -> bool:
        """Return whether an idempotent Team command already committed."""

        with self._lock:
            row = self._connection.execute(
                "SELECT team_run_id, kind FROM team_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            return False
        if str(row["team_run_id"]) != team_run_id or str(row["kind"]) != kind:
            raise StorageConflictError(
                "Command id was already used for another operation"
            )
        return True

    def heartbeat_agent_session(
        self, session_id: str, *, activity: str | None = None,
    ) -> AgentSessionRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Agent session not found: {session_id}")
            if str(row["state"]) in {
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            }:
                raise InvalidStateTransitionError(
                    "Terminal AgentSession cannot renew its heartbeat"
                )
            connection.execute(
                "UPDATE agent_sessions SET heartbeat_at = ?, updated_at = ?, "
                "waiting_reason = CASE WHEN state = 'work' AND ? IS NOT NULL "
                "THEN ? ELSE waiting_reason END WHERE id = ?",
                (now, now, activity, activity, session_id),
            )
        return self.get_agent_session(session_id)

    def request_attempt_cancel(
        self,
        attempt_id: str,
        *,
        requested_by: str,
        reason: str,
        command_id: str,
    ) -> TaskAttemptRecord:
        """Revoke permissions and request cancellation without releasing resources."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            team_run_id = str(attempt["team_run_id"])
            replay = self._team_command_result(
                connection, team_run_id, command_id, "cancel_attempt"
            )
            if replay is not None:
                return self._attempt_from_connection(connection, attempt_id)
            if str(attempt["state"]) in {
                TaskAttemptState.SUCCEEDED.value,
                TaskAttemptState.FAILED.value,
                TaskAttemptState.CANCELLED.value,
                TaskAttemptState.ORPHANED.value,
            }:
                raise InvalidStateTransitionError("Terminal Attempt cannot be cancelled")
            connection.execute(
                """
                UPDATE task_attempts
                SET write_enabled = 0, cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    updated_at = ? WHERE id = ?
                """,
                (now, now, attempt_id),
            )
            connection.execute(
                """
                UPDATE worktree_bindings
                SET write_enabled = 0, state = CASE
                        WHEN state = 'active' THEN 'frozen' ELSE state END,
                    frozen_reason = COALESCE(frozen_reason, 'cancel_requested'),
                    updated_at = ? WHERE attempt_id = ?
                """,
                (now, attempt_id),
            )
            session = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (str(attempt["session_id"]),),
            ).fetchone()
            assert session is not None
            team = self._team_row(connection, team_run_id)
            self._insert_team_message(
                connection,
                team=team,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=str(attempt["agent_id"]),
                recipient_generation=int(session["generation"]),
                message_type="CANCEL",
                payload={
                    "reason_code": "cancel_requested",
                    "reason": str(reason),
                    "requested_by": str(requested_by),
                    "effective_scope": "attempt",
                    "grace_deadline": None,
                },
                dedupe_key=f"cancel:attempt:{attempt_id}:{command_id}",
                attempt_id=attempt_id,
                task_id=str(attempt["task_id"]),
                causation_id=command_id,
                priority="control",
                created_at=now,
            )
            self._record_team_command(
                connection,
                team_run_id,
                command_id,
                "cancel_attempt",
                {"attempt_id": attempt_id},
                now,
            )
            self._append_team_event(
                connection,
                team,
                "team.attempt.cancel_requested",
                {
                    "team_run_id": team_run_id,
                    "attempt_id": attempt_id,
                    "requested_by": str(requested_by),
                    "reason": str(reason),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def mark_attempt_suspect(
        self,
        attempt_id: str,
        *,
        reason: str,
    ) -> TaskAttemptRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            if str(attempt["state"]) in {
                TaskAttemptState.SUCCEEDED.value,
                TaskAttemptState.FAILED.value,
                TaskAttemptState.CANCELLED.value,
                TaskAttemptState.ORPHANED.value,
            }:
                return _row_to_task_attempt(attempt)
            connection.execute(
                "UPDATE task_attempts SET write_enabled = 0, updated_at = ? WHERE id = ?",
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE worktree_bindings SET write_enabled = 0,
                    state = CASE WHEN state = 'active' THEN 'frozen' ELSE state END,
                    frozen_reason = COALESCE(frozen_reason, 'session_suspect'),
                    updated_at = ? WHERE attempt_id = ?
                """,
                (now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'suspect', waiting_reason = ?, updated_at = ?
                WHERE id = ? AND state NOT IN ('lost', 'failed', 'shutdown')
                """,
                (str(reason), now, str(attempt["session_id"])),
            )
            team = self._team_row(connection, str(attempt["team_run_id"]))
            self._append_team_event(
                connection,
                team,
                "team.session.suspect",
                {
                    "team_run_id": str(attempt["team_run_id"]),
                    "attempt_id": attempt_id,
                    "session_id": str(attempt["session_id"]),
                    "reason": str(reason),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def pause_attempt_after_worker_exit(
        self,
        attempt_id: str,
        *,
        reason: str,
    ) -> TaskAttemptRecord:
        return self._finish_attempt_worker(
            attempt_id,
            outcome="waiting",
            reason=reason,
        )

    def finalize_cancelled_attempt(
        self,
        attempt_id: str,
        *,
        reason: str,
    ) -> TaskAttemptRecord:
        return self._finish_attempt_worker(
            attempt_id,
            outcome="cancelled",
            reason=reason,
        )

    def fail_attempt_after_worker_exit(
        self,
        attempt_id: str,
        *,
        error: Mapping[str, Any],
    ) -> TaskAttemptRecord:
        return self._finish_attempt_worker(
            attempt_id,
            outcome="failed",
            reason=str(error.get("message") or "Agent worker failed"),
            error=error,
        )

    def orphan_attempt_after_worker_exit(
        self,
        attempt_id: str,
        *,
        reason: str,
    ) -> TaskAttemptRecord:
        return self._finish_attempt_worker(
            attempt_id,
            outcome="orphaned",
            reason=reason,
        )

    def _finish_attempt_worker(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason: str,
        error: Mapping[str, Any] | None = None,
    ) -> TaskAttemptRecord:
        if outcome not in {"waiting", "cancelled", "failed", "orphaned"}:
            raise ValueError(f"Unsupported worker outcome: {outcome}")
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
            current = str(attempt["state"])
            if current in {
                TaskAttemptState.SUCCEEDED.value,
                TaskAttemptState.FAILED.value,
                TaskAttemptState.CANCELLED.value,
                TaskAttemptState.ORPHANED.value,
            }:
                return _row_to_task_attempt(attempt)
            session_state = AgentSessionState.WAITING.value
            attempt_state = (
                current
                if current
                in {
                    TaskAttemptState.PLAN_REQUIRED.value,
                    TaskAttemptState.PLAN_SUBMITTED.value,
                    TaskAttemptState.CANDIDATE_SUBMITTED.value,
                    TaskAttemptState.VALIDATING.value,
                    TaskAttemptState.VALIDATION_FAILED.value,
                }
                else TaskAttemptState.WAITING.value
            )
            release_resources = False
            task_reset = False
            task_cancel = False
            result_unknown = int(attempt["result_unknown"])
            model_timeout = reason in {"model_response_timeout", "model_call_timeout"}
            if model_timeout:
                # Do not resurrect already acknowledged historical unknown results.
                # A still-running write, however, cannot be considered safe.
                pending_write = connection.execute(
                    "SELECT 1 FROM tool_executions WHERE attempt_id = ? "
                    "AND is_write = 1 AND status = 'running' LIMIT 1", (attempt_id,),
                ).fetchone()
                if pending_write is not None:
                    result_unknown = 1
                    connection.execute(
                        "UPDATE tool_executions SET result_unknown = 1 "
                        "WHERE attempt_id = ? AND is_write = 1 AND status = 'running'",
                        (attempt_id,),
                    )
                error = {"type": reason, "message": reason}
            if outcome == "cancelled":
                attempt_state = TaskAttemptState.CANCELLED.value
                session_state = AgentSessionState.IDLE.value
                release_resources = True
                task_cancel = True
            elif outcome == "failed":
                attempt_state = TaskAttemptState.FAILED.value
                session_state = AgentSessionState.IDLE.value
                release_resources = True
                task_reset = True
            elif outcome == "orphaned":
                attempt_state = TaskAttemptState.ORPHANED.value
                session_state = AgentSessionState.LOST.value
                result_unknown = 1
            connection.execute(
                """
                UPDATE task_attempts
                SET state = ?, write_enabled = 0, result_unknown = ?,
                    worker_exited_at = ?, finished_at = CASE
                        WHEN ? = 'waiting' THEN finished_at ELSE ? END,
                    error_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    attempt_state,
                    result_unknown,
                    now,
                    outcome,
                    now,
                    _json_dumps(dict(error)) if error is not None else None,
                    now,
                    attempt_id,
                ),
            )
            binding_state = (
                "orphaned"
                if outcome == "orphaned"
                else ("frozen" if model_timeout or outcome in {"cancelled", "failed"} else None)
            )
            connection.execute(
                """
                UPDATE worktree_bindings
                SET write_enabled = 0,
                    state = COALESCE(?, state),
                    frozen_reason = CASE WHEN ? IS NULL THEN frozen_reason
                        ELSE COALESCE(frozen_reason, ?) END,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (binding_state, binding_state, str(reason), now, attempt_id),
            )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = ?, current_attempt_id = CASE
                        WHEN ? IN ('cancelled', 'failed') THEN NULL
                        ELSE current_attempt_id END,
                    waiting_reason = ?, heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    session_state,
                    outcome,
                    str(reason),
                    now,
                    now,
                    str(attempt["session_id"]),
                ),
            )
            if release_resources:
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET state = 'released', released_at = ?
                    WHERE attempt_id = ? AND state != 'released'
                    """,
                    (now, attempt_id),
                )
            elif outcome == "orphaned":
                connection.execute(
                    """
                    UPDATE resource_leases SET state = 'orphaned'
                    WHERE attempt_id = ? AND state = 'active'
                    """,
                    (attempt_id,),
                )
            if task_reset:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending', owner = NULL, revision = revision + 1,
                        updated_at = ? WHERE task_list_id = ? AND id = ?
                    """,
                    (now, str(attempt["task_list_id"]), str(attempt["task_id"])),
                )
                connection.execute(
                    """
                    UPDATE task_lists SET revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, str(attempt["task_list_id"])),
                )
            elif task_cancel:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', revision = revision + 1,
                        updated_at = ? WHERE task_list_id = ? AND id = ?
                    """,
                    (now, str(attempt["task_list_id"]), str(attempt["task_id"])),
                )
                connection.execute(
                    """
                    UPDATE task_lists SET revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, str(attempt["task_list_id"])),
                )
            team = self._team_row(connection, str(attempt["team_run_id"]))
            self._append_team_event(
                connection,
                team,
                f"team.attempt.{attempt_state}",
                {
                    "team_run_id": str(attempt["team_run_id"]),
                    "attempt_id": attempt_id,
                    "previous_state": current,
                    "state": attempt_state,
                    "worker_exited": True,
                    "reason": str(reason),
                },
                agent_id=str(attempt["agent_id"]),
            )
        self._notify_activity()
        return self.get_task_attempt(attempt_id)

    def list_task_attempts(
        self,
        team_run_id: str,
        *,
        task_id: str | None = None,
    ) -> list[TaskAttemptRecord]:
        conditions = ["team_run_id = ?"]
        parameters: list[Any] = [team_run_id]
        if task_id is not None:
            conditions.append("task_id = ?")
            parameters.append(str(task_id))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM task_attempts WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at, id",
                parameters,
            ).fetchall()
        return [_row_to_task_attempt(row) for row in rows]

    def list_task_scheduling(
        self,
        team_run_id: str,
        *,
        allow_code: bool = False,
    ) -> list[TaskSchedulingRecord]:
        """Explain dependency readiness separately from current schedulability."""

        with self._lock:
            team = self._team_row(self._connection, team_run_id)
            tasks = self._connection.execute(
                "SELECT * FROM tasks WHERE task_list_id = ? "
                "ORDER BY CAST(id AS INTEGER), id",
                (str(team["task_list_id"]),),
            ).fetchall()
            active_attempts = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM task_attempts
                    WHERE team_run_id = ?
                      AND state NOT IN ('succeeded', 'failed', 'cancelled', 'orphaned')
                    """,
                    (team_run_id,),
                ).fetchone()["value"]
            )
            idle_agents = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM agent_sessions s
                    JOIN team_agents a ON a.id = s.agent_id
                    WHERE s.team_run_id = ? AND s.state = 'idle'
                      AND a.role = 'teammate'
                    """,
                    (team_run_id,),
                ).fetchone()["value"]
            )
            usage = self._connection.execute(
                """
                SELECT COUNT(*) AS model_calls,
                       COALESCE(SUM(
                           COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                           + COALESCE(cache_creation_input_tokens, 0)
                           + COALESCE(cache_read_input_tokens, 0)
                       ), 0) AS tokens
                FROM model_calls WHERE run_id = ?
                """,
                (str(team["root_run_id"]),),
            ).fetchone()
            held_resources = self._connection.execute(
                """
                SELECT resource_kind, resource_key FROM resource_leases
                WHERE team_run_id = ? AND state != 'released'
                """,
                (team_run_id,),
            ).fetchall()
            planned_task_ids: set[str] | None = None
            if team["active_plan_revision"] is not None:
                active_plan = self._connection.execute(
                    "SELECT plan_json FROM team_plan_revisions "
                    "WHERE team_run_id = ? AND revision = ? AND status = 'approved'",
                    (team_run_id, int(team["active_plan_revision"])),
                ).fetchone()
                if active_plan is not None:
                    plan_payload = _json_loads(active_plan["plan_json"], {})
                    structured_tasks = plan_payload.get("tasks")
                    if isinstance(structured_tasks, list):
                        structured_task_ids = {
                            str(item.get("task_id") or item.get("id") or "")
                            for item in structured_tasks
                            if isinstance(item, Mapping)
                        }
                        # Older first-phase records used descriptive string lists.
                        # Enforce the whitelist only for the structured plan format.
                        if structured_task_ids:
                            planned_task_ids = structured_task_ids
            results: list[TaskSchedulingRecord] = []
            for task in tasks:
                dependency_reasons: list[str] = []
                blockers = self._connection.execute(
                    """
                    SELECT d.dependency_requirement, d.blocker_id, t.status
                    FROM task_dependencies d
                    JOIN tasks t
                      ON t.task_list_id = d.task_list_id AND t.id = d.blocker_id
                    WHERE d.task_list_id = ? AND d.blocked_id = ?
                    """,
                    (str(team["task_list_id"]), str(task["id"])),
                ).fetchall()
                for blocker in blockers:
                    if (
                        str(blocker["dependency_requirement"])
                        == DependencyRequirement.CANDIDATE_INTEGRATED.value
                    ):
                        integrated = self._connection.execute(
                            """
                            SELECT 1 FROM candidates
                            WHERE team_run_id = ? AND task_id = ?
                              AND status = 'committed' AND integrated_at IS NOT NULL
                            LIMIT 1
                            """,
                            (team_run_id, str(blocker["blocker_id"])),
                        ).fetchone()
                        if integrated is None:
                            dependency_reasons.append("candidate_not_integrated")
                    elif str(blocker["status"]) != TaskStatus.COMPLETED.value:
                        dependency_reasons.append("dependency_not_completed")
                reasons = list(dict.fromkeys(dependency_reasons))
                try:
                    validate_task_execution(_json_loads(task["metadata_json"], {}))
                except ValueError:
                    reasons.append("task_configuration_conflict")
                if str(task["status"]) != TaskStatus.PENDING.value:
                    reasons.append(f"task_status:{task['status']}")
                if str(team["state"]) != TeamRunState.RUNNING.value:
                    reasons.append(f"team_status:{team['state']}")
                if team["active_plan_revision"] is None:
                    reasons.append("team_plan_not_approved")
                if (
                    planned_task_ids is not None
                    and str(task["id"]) not in planned_task_ids
                ):
                    reasons.append("task_not_in_active_team_plan")
                if active_attempts >= int(team["max_teammates"]):
                    reasons.append("team_concurrency_exhausted")
                if idle_agents <= 0:
                    reasons.append("no_idle_teammate")
                if team["model_call_budget"] is not None and int(
                    usage["model_calls"]
                ) >= int(team["model_call_budget"]):
                    reasons.append("model_call_budget_exhausted")
                if team["token_budget"] is not None and int(usage["tokens"]) >= int(
                    team["token_budget"]
                ):
                    reasons.append("token_budget_exhausted")
                if team["deadline_at"] and _timestamp_has_passed(
                    str(team["deadline_at"])
                ):
                    reasons.append("team_deadline_exceeded")
                metadata = _json_loads(task["metadata_json"], {})
                if str(metadata.get("kind") or "analysis").lower() == "code" and not allow_code:
                    reasons.append("team_write_disabled")
                try:
                    requested = _team_task_resource_keys(metadata)
                except ValueError:
                    requested = []  # Not schedulable; retain a readable diagnostic in the snapshot.
                    if "task_configuration_conflict" not in reasons:
                        reasons.append("task_configuration_conflict")
                if any(
                    _team_resources_overlap(
                        requested_kind,
                        requested_key,
                        str(held["resource_kind"]),
                        str(held["resource_key"]),
                    )
                    for requested_kind, requested_key in requested
                    for held in held_resources
                ):
                    reasons.append("resource_conflict")
                results.append(
                    TaskSchedulingRecord(
                        task_id=str(task["id"]),
                        dependency_ready=not dependency_reasons,
                        schedulable=not reasons,
                        reasons=tuple(dict.fromkeys(reasons)),
                    )
                )
        return results

    def list_resource_leases(
        self,
        team_run_id: str,
        *,
        attempt_id: str | None = None,
    ) -> list[ResourceLeaseRecord]:
        conditions = ["team_run_id = ?"]
        parameters: list[Any] = [team_run_id]
        if attempt_id is not None:
            conditions.append("attempt_id = ?")
            parameters.append(attempt_id)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM resource_leases WHERE {' AND '.join(conditions)} "
                "ORDER BY acquired_at, id",
                parameters,
            ).fetchall()
        return [_row_to_resource_lease(row) for row in rows]

    def set_task_dependency_requirement(
        self,
        task_list_id: str,
        *,
        blocker_id: str,
        blocked_id: str,
        requirement: str,
    ) -> None:
        _validate_choice(
            "dependency requirement", requirement, DEPENDENCY_REQUIREMENTS
        )
        with self._transaction(immediate=True) as connection:
            blocked = connection.execute(
                "SELECT status FROM tasks WHERE task_list_id = ? AND id = ?",
                (task_list_id, str(blocked_id)),
            ).fetchone()
            if blocked is None:
                raise RecordNotFoundError(f"Task not found: {blocked_id}")
            if str(blocked["status"]) != TaskStatus.PENDING.value:
                raise StorageConflictError(
                    "Dependency requirement can only change for a pending Task"
                )
            cursor = connection.execute(
                """
                UPDATE task_dependencies SET dependency_requirement = ?
                WHERE task_list_id = ? AND blocker_id = ? AND blocked_id = ?
                """,
                (requirement, task_list_id, str(blocker_id), str(blocked_id)),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError("Task dependency not found")

    def get_task_dependency_requirement(
        self, task_list_id: str, *, blocker_id: str, blocked_id: str
    ) -> DependencyRequirement:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT dependency_requirement FROM task_dependencies
                WHERE task_list_id = ? AND blocker_id = ? AND blocked_id = ?
                """,
                (task_list_id, str(blocker_id), str(blocked_id)),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("Task dependency not found")
        return DependencyRequirement(str(row["dependency_requirement"]))

    def list_team_messages(
        self,
        team_run_id: str,
        *,
        recipient_agent_id: str | None = None,
    ) -> list[TeamMessageRecord]:
        conditions = ["team_run_id = ?"]
        parameters: list[Any] = [team_run_id]
        if recipient_agent_id is not None:
            conditions.append("recipient_agent_id = ?")
            parameters.append(recipient_agent_id)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM team_messages WHERE {' AND '.join(conditions)} "
                "ORDER BY recipient_agent_id, recipient_generation, sequence_no",
                parameters,
            ).fetchall()
        return [_row_to_team_message(row) for row in rows]

    def get_team_message(self, message_id: str) -> TeamMessageRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM team_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return _row_to_team_message(row) if row else None

    def send_team_message(
        self,
        team_run_id: str,
        *,
        sender_type: str,
        recipient_type: str,
        recipient_agent_id: str,
        recipient_generation: int,
        message_type: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        sender_agent_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        payload_version: int = 1,
        artifact_refs: Sequence[str] = (),
        correlation_id: str | None = None,
        causation_id: str | None = None,
        priority: str = "normal",
    ) -> TeamMessageRecord:
        """Persist one validated message, returning an existing duplicate."""

        normalized_sender = str(sender_type).strip().lower()
        normalized_recipient = str(recipient_type).strip().lower()
        normalized_type = str(message_type).strip().upper()
        normalized_priority = str(priority).strip().lower()
        if normalized_priority not in {"control", "normal"}:
            raise ValueError("Message priority must be control or normal")
        validate_team_message(
            sender_type=normalized_sender,
            recipient_type=normalized_recipient,
            message_type=normalized_type,
            payload_version=payload_version,
            payload=payload,
        )
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM team_messages WHERE team_run_id = ? AND dedupe_key = ?",
                (team_run_id, str(dedupe_key)),
            ).fetchone()
            if existing is not None:
                existing_record = _row_to_team_message(existing)
                same_message = (
                    existing_record.sender_type == normalized_sender
                    and existing_record.sender_agent_id == sender_agent_id
                    and existing_record.recipient_type == normalized_recipient
                    and existing_record.recipient_agent_id == recipient_agent_id
                    and existing_record.recipient_generation
                    == int(recipient_generation)
                    and existing_record.type == normalized_type
                    and existing_record.payload_version == payload_version
                    and existing_record.payload == dict(payload)
                    and existing_record.task_id
                    == (str(task_id) if task_id is not None else None)
                    and existing_record.attempt_id == attempt_id
                    and existing_record.correlation_id == correlation_id
                    and existing_record.causation_id == causation_id
                    and existing_record.artifact_refs
                    == tuple(str(item) for item in artifact_refs)
                    and existing_record.priority == normalized_priority
                )
                if not same_message:
                    raise StorageConflictError(
                        "Message dedupe key was reused with a different envelope"
                    )
                return existing_record
            team = self._team_row(connection, team_run_id)
            recipient = connection.execute(
                "SELECT * FROM team_agents WHERE id = ? AND team_run_id = ?",
                (recipient_agent_id, team_run_id),
            ).fetchone()
            if recipient is None:
                raise RecordNotFoundError(
                    f"Recipient agent not found in TeamRun: {recipient_agent_id}"
                )
            if str(recipient["role"]) != normalized_recipient:
                raise StorageConflictError("Recipient type does not match agent role")
            session = connection.execute(
                """
                SELECT 1 FROM agent_sessions
                WHERE team_run_id = ? AND agent_id = ? AND generation = ?
                  AND state NOT IN ('lost', 'failed', 'shutdown')
                """,
                (team_run_id, recipient_agent_id, int(recipient_generation)),
            ).fetchone()
            if session is None:
                raise StorageConflictError(
                    "Recipient generation is not the current active session"
                )
            if normalized_sender == "runtime":
                if sender_agent_id is not None:
                    raise ValueError("Runtime messages do not have sender_agent_id")
            else:
                sender = connection.execute(
                    "SELECT role FROM team_agents WHERE id = ? AND team_run_id = ?",
                    (sender_agent_id, team_run_id),
                ).fetchone()
                if sender is None or str(sender["role"]) != normalized_sender:
                    raise StorageConflictError("Sender identity does not match sender type")
            if task_id is not None:
                task = connection.execute(
                    "SELECT 1 FROM tasks WHERE task_list_id = ? AND id = ?",
                    (str(team["task_list_id"]), str(task_id)),
                ).fetchone()
                if task is None:
                    raise RecordNotFoundError(f"Task not found in TeamRun: {task_id}")
            if attempt_id is not None:
                attempt = connection.execute(
                    "SELECT task_id FROM task_attempts WHERE id = ? AND team_run_id = ?",
                    (attempt_id, team_run_id),
                ).fetchone()
                if attempt is None:
                    raise RecordNotFoundError(
                        f"Attempt not found in TeamRun: {attempt_id}"
                    )
                if task_id is not None and str(attempt["task_id"]) != str(task_id):
                    raise StorageConflictError("Message Task and Attempt do not match")
            message_id = self._insert_team_message(
                connection,
                team=team,
                sender_type=normalized_sender,
                sender_agent_id=sender_agent_id,
                recipient_type=normalized_recipient,
                recipient_agent_id=recipient_agent_id,
                recipient_generation=int(recipient_generation),
                message_type=normalized_type,
                payload=payload,
                dedupe_key=str(dedupe_key),
                created_at=now,
                task_id=str(task_id) if task_id is not None else None,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                artifact_refs=artifact_refs,
                priority=normalized_priority,
            )
            connection.execute(
                "UPDATE team_messages SET payload_version = ? WHERE id = ?",
                (payload_version, message_id),
            )
            self._append_team_event(
                connection,
                team,
                "team.message.created",
                {
                    "team_run_id": team_run_id,
                    "message_id": message_id,
                    "type": normalized_type,
                    "recipient_agent_id": recipient_agent_id,
                    "recipient_generation": int(recipient_generation),
                },
                agent_id=sender_agent_id,
            )
            row = connection.execute(
                "SELECT * FROM team_messages WHERE id = ?", (message_id,)
            ).fetchone()
        self._notify_activity()
        assert row is not None
        return _row_to_team_message(row)

    def answer_team_question(
        self, team_run_id: str, *, question_id: str, lead_agent_id: str,
        answer: str, scope_changed: bool,
    ) -> TeamMessageRecord:
        """Persist an answer and wake only its own question wait, in one transaction."""
        if not str(answer).strip():
            raise ValueError("A Lead answer cannot be empty")
        payload = {"answer": str(answer), "scope_changed": bool(scope_changed)}
        validate_team_message(
            sender_type="lead", recipient_type="teammate", message_type="ANSWER",
            payload_version=1, payload=payload,
        )
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            team = self._team_row(connection, team_run_id)
            if lead_agent_id != str(team["lead_agent_id"]):
                raise StorageConflictError("Only this Team's Lead can answer its questions")
            question = connection.execute(
                "SELECT * FROM team_messages WHERE id = ? AND team_run_id = ? "
                "AND type = 'QUESTION' AND sender_type = 'teammate'",
                (question_id, team_run_id),
            ).fetchone()
            if question is None:
                raise RecordNotFoundError("QUESTION not found in this TeamRun")
            existing = connection.execute(
                "SELECT * FROM team_messages WHERE team_run_id = ? AND dedupe_key = ?",
                (team_run_id, f"answer:{question_id}"),
            ).fetchone()
            if existing is not None:
                record = _row_to_team_message(existing)
                if (record.type != "ANSWER" or record.correlation_id != question_id
                        or record.sender_agent_id != lead_agent_id or record.payload != payload):
                    raise StorageConflictError("This question already has a different answer")
                # Replay is an audit read, never a second wakeup.
                return record
            attempt, session = self._question_execution(connection, team, question)
            expected_reason = f"waiting_for_lead_answer:{question_id}"
            if session["state"] == "waiting" and session["waiting_reason"] != expected_reason:
                raise StorageConflictError("Session is not waiting for this question")
            question_payload = _json_loads(question["payload_json"], {})
            if scope_changed and not question_payload.get("blocking"):
                raise StorageConflictError("A plan change requires a blocking question, not a progress clarification")
            message_id = self._insert_team_message(
                connection, team=team, sender_type="lead", sender_agent_id=lead_agent_id,
                recipient_type="teammate", recipient_agent_id=str(session["agent_id"]),
                recipient_generation=int(session["generation"]), message_type="ANSWER",
                payload=payload, dedupe_key=f"answer:{question_id}", created_at=now,
                task_id=str(attempt["task_id"]), attempt_id=str(attempt["id"]),
                correlation_id=question_id, priority="control",
            )
            if scope_changed:
                # This is a plan decision, not permission to edit the old plan or
                # to resume through the safety-recovery API. Retain all leases/files.
                connection.execute(
                    "UPDATE task_attempts SET state = 'waiting', write_enabled = 0, "
                    "error_json = ?, updated_at = ? WHERE id = ?",
                    (_json_dumps({"type": "team_plan_change_required", "question_id": question_id}),
                     now, str(attempt["id"])),
                )
                connection.execute(
                    "UPDATE worktree_bindings SET write_enabled = 0, updated_at = ? WHERE attempt_id = ?",
                    (now, str(attempt["id"])),
                )
                connection.execute(
                    "UPDATE agent_sessions SET state = 'waiting', waiting_reason = "
                    "'team_plan_change_required', updated_at = ? WHERE id = ?",
                    (now, str(session["id"])),
                )
            else:
                self._resume_answered_question(connection, team, str(session["id"]), now)
            self._append_team_event(
                connection, team, "team.question.answered",
                {"question_id": question_id, "message_id": message_id,
                 "attempt_id": str(attempt["id"]), "scope_changed": bool(scope_changed)},
                agent_id=lead_agent_id,
            )
            row = connection.execute("SELECT * FROM team_messages WHERE id = ?", (message_id,)).fetchone()
        self._notify_activity()
        return _row_to_team_message(row)

    def _question_execution(
        self, connection: sqlite3.Connection, team: sqlite3.Row, question: sqlite3.Row,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        """Check identity and durable permissions; filesystem checks remain in the tool gate."""
        attempt = connection.execute(
            "SELECT * FROM task_attempts WHERE id = ? AND team_run_id = ?",
            (question["attempt_id"], str(team["id"])),
        ).fetchone()
        if (attempt is None or attempt["agent_id"] != question["sender_agent_id"]
                or attempt["task_id"] != question["task_id"]):
            raise StorageConflictError("Question no longer belongs to this Task/Agent Attempt")
        session = connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (attempt["session_id"],),
        ).fetchone()
        payload = _json_loads(question["payload_json"], {})
        if (session is None or session["id"] != payload.get("session_id")
                or session["generation"] != payload.get("generation")
                or session["current_attempt_id"] != attempt["id"]
                or session["state"] not in {"work", "waiting"}):
            raise StorageConflictError("Question belongs to a stale Session generation")
        if (attempt["state"] not in {"running", "plan_required", "review_rejected"}
                or attempt["cancel_requested_at"] or attempt["result_unknown"]):
            raise StorageConflictError("An answer cannot resume a frozen, cancelled or finished Attempt")
        if (team["state"] != "running" or team["active_plan_revision"] != attempt["team_plan_revision"]
                or team["base_commit"] != attempt["attempt_base_commit"]):
            raise StorageConflictError("Question Team Plan or base commit is no longer active")
        plan = self._team_plan_row(connection, str(team["id"]), int(attempt["team_plan_revision"]))
        if plan["status"] != "approved":
            raise StorageConflictError("Question Team Plan is not approved")
        self._validate_team_plan_tasks(connection, str(team["task_list_id"]), _json_loads(plan["plan_json"], {}))
        task = connection.execute(
            "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
            (attempt["task_list_id"], attempt["task_id"]),
        ).fetchone()
        if task is None:
            raise StorageConflictError("Question Task no longer exists")
        metadata = _json_loads(task["metadata_json"], {})
        validate_task_execution(metadata)
        required = set(_team_task_resource_keys(metadata))
        leases = connection.execute(
            "SELECT * FROM resource_leases WHERE attempt_id = ?", (attempt["id"],),
        ).fetchall()
        held = {(row["resource_kind"], row["resource_key"]) for row in leases
                if row["state"] == "active" and row["generation"] == session["generation"]}
        if not required.issubset(held):
            raise StorageConflictError("Question Attempt has lost its resource leases")
        binding = connection.execute(
            "SELECT * FROM worktree_bindings WHERE attempt_id = ?", (attempt["id"],),
        ).fetchone()
        if metadata.get("kind") == "code" and binding is None:
            raise StorageConflictError("Code Question has no Worktree binding")
        if binding is not None and (binding["state"] != "active"
                or binding["session_id"] != session["id"] or binding["generation"] != session["generation"]):
            raise StorageConflictError("An answer cannot restore an invalid Worktree binding")
        if attempt["write_enabled"]:
            self._ensure_attempt_plan_still_approved(connection, attempt)
        return attempt, session

    def _resume_answered_question(
        self, connection: sqlite3.Connection, team: sqlite3.Row, session_id: str, now: str,
    ) -> None:
        session = connection.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        reason = str(session["waiting_reason"] or "")
        if session["state"] != "waiting" or not reason.startswith("waiting_for_lead_answer:"):
            return
        question_id = reason.partition(":")[2]
        question = connection.execute(
            "SELECT * FROM team_messages WHERE id = ? AND team_run_id = ? AND type = 'QUESTION'",
            (question_id, str(team["id"])),
        ).fetchone()
        answer = connection.execute(
            "SELECT * FROM team_messages WHERE team_run_id = ? AND dedupe_key = ? AND type = 'ANSWER'",
            (str(team["id"]), f"answer:{question_id}"),
        ).fetchone()
        if question is None or answer is None or _json_loads(answer["payload_json"], {}).get("scope_changed"):
            return
        if (answer["correlation_id"] != question_id or answer["recipient_agent_id"] != session["agent_id"]
                or answer["recipient_generation"] != session["generation"]):
            return
        try:
            self._question_execution(connection, team, question)
        except (StorageConflictError, ValueError):
            return  # A safety pause wins over a late answer/checkpoint.
        connection.execute(
            "UPDATE agent_sessions SET state = 'work', waiting_reason = NULL, updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        self._append_team_event(
            connection, team, "team.question.resumed",
            {"question_id": question_id, "message_id": str(answer["id"]), "session_id": session_id},
            agent_id=str(session["agent_id"]),
        )

    def fetch_unacked_team_messages(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[TeamMessageRecord]:
        """Mark pending messages delivered to the exact Session generation."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            session = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise RecordNotFoundError(f"Agent session not found: {session_id}")
            if str(session["state"]) in {
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            }:
                return []
            rows = connection.execute(
                """
                SELECT * FROM team_messages
                WHERE team_run_id = ? AND recipient_agent_id = ?
                  AND recipient_generation = ? AND acked_at IS NULL
                ORDER BY CASE priority WHEN 'control' THEN 0 ELSE 1 END,
                         sequence_no
                LIMIT ?
                """,
                (
                    str(session["team_run_id"]),
                    str(session["agent_id"]),
                    int(session["generation"]),
                    _positive_limit(limit),
                ),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE team_messages
                    SET delivered_at = COALESCE(delivered_at, ?),
                        delivery_attempts = delivery_attempts + 1
                    WHERE id = ?
                    """,
                    [(now, str(row["id"])) for row in rows],
                )
                rows = connection.execute(
                    f"SELECT * FROM team_messages WHERE id IN "
                    f"({','.join('?' for _ in rows)}) "
                    "ORDER BY CASE priority WHEN 'control' THEN 0 ELSE 1 END, sequence_no",
                    [str(row["id"]) for row in rows],
                ).fetchall()
        return [_row_to_team_message(row) for row in rows]

    def save_agent_session_checkpoint(
        self,
        session_id: str,
        *,
        messages: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        safe_boundary: str,
        metadata: Mapping[str, Any] | None = None,
        acknowledged_message_ids: Sequence[str] = (),
        target_state: str | None = None,
        waiting_reason: str | None = None,
    ) -> AgentSessionCheckpointRecord:
        """Persist a safe checkpoint and message ACK side effects atomically."""

        if not str(safe_boundary).strip():
            raise ValueError("safe_boundary is required")
        if target_state is not None:
            _validate_choice("agent session state", target_state, AGENT_SESSION_STATES)
        now = utc_now_iso()
        checkpoint_id = _new_id("sessioncp")
        with self._transaction(immediate=True) as connection:
            session = connection.execute(
                "SELECT s.*, a.role AS agent_role FROM agent_sessions s "
                "JOIN team_agents a ON a.id = s.agent_id WHERE s.id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise RecordNotFoundError(f"Agent session not found: {session_id}")
            if str(session["state"]) in {
                AgentSessionState.LOST.value,
                AgentSessionState.FAILED.value,
                AgentSessionState.SHUTDOWN.value,
            }:
                raise InvalidStateTransitionError(
                    "Terminal AgentSession cannot create a checkpoint"
                )
            next_state = target_state or str(session["state"])
            if session["waiting_reason"] == "team_plan_change_required":
                # A worker finishing its yield must not erase the Lead's earlier decision.
                next_state = AgentSessionState.WAITING.value
                waiting_reason = "team_plan_change_required"
            if (
                next_state == AgentSessionState.WORK.value
                and not session["current_attempt_id"]
                and str(session["agent_role"]) != TeamAgentRole.LEAD.value
            ):
                raise InvalidStateTransitionError(
                    "WORK AgentSession requires an active Attempt"
                )
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS value "
                    "FROM agent_session_checkpoints WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO agent_session_checkpoints(
                    id, session_id, generation, revision, messages_json,
                    context_json, metadata_json, safe_boundary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    int(session["generation"]),
                    revision,
                    _json_dumps([dict(item) for item in messages]),
                    _json_dumps(dict(context)),
                    _json_dumps(dict(metadata or {})),
                    str(safe_boundary),
                    now,
                ),
            )
            seen: set[str] = set()
            for message_id in acknowledged_message_ids:
                normalized_id = str(message_id)
                if normalized_id in seen:
                    continue
                seen.add(normalized_id)
                message = connection.execute(
                    "SELECT * FROM team_messages WHERE id = ?", (normalized_id,)
                ).fetchone()
                if message is None:
                    raise RecordNotFoundError(
                        f"Team message not found: {normalized_id}"
                    )
                if (
                    str(message["team_run_id"]) != str(session["team_run_id"])
                    or str(message["recipient_agent_id"]) != str(session["agent_id"])
                    or int(message["recipient_generation"])
                    != int(session["generation"])
                ):
                    raise StorageConflictError(
                        "Message does not belong to this AgentSession generation"
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO team_message_consumptions(
                        message_id, consumer_agent_id, consumer_generation,
                        status, result_ref, processed_at
                    ) VALUES (?, ?, ?, 'processed', ?, ?)
                    """,
                    (
                        normalized_id,
                        str(session["agent_id"]),
                        int(session["generation"]),
                        checkpoint_id,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE team_messages SET acked_at = COALESCE(acked_at, ?) WHERE id = ?",
                    (now, normalized_id),
                )
            connection.execute(
                """
                UPDATE agent_sessions
                SET checkpoint_id = ?, state = ?, heartbeat_at = ?,
                    waiting_reason = ?, updated_at = ? WHERE id = ?
                """,
                (
                    checkpoint_id,
                    next_state,
                    now,
                    waiting_reason if next_state == AgentSessionState.WAITING.value else None,
                    now,
                    session_id,
                ),
            )
            team = self._team_row(connection, str(session["team_run_id"]))
            self._append_team_event(
                connection,
                team,
                "team.session.checkpointed",
                {
                    "team_run_id": str(session["team_run_id"]),
                    "session_id": session_id,
                    "checkpoint_id": checkpoint_id,
                    "revision": revision,
                    "safe_boundary": str(safe_boundary),
                    "acked_messages": len(seen),
                    "state": next_state,
                },
                agent_id=str(session["agent_id"]),
            )
            self._resume_answered_question(connection, team, session_id, now)
            row = connection.execute(
                "SELECT * FROM agent_session_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
        self._notify_activity()
        assert row is not None
        return _row_to_agent_session_checkpoint(row)

    def get_latest_agent_session_checkpoint(
        self, session_id: str
    ) -> AgentSessionCheckpointRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM agent_session_checkpoints
                WHERE session_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _row_to_agent_session_checkpoint(row) if row else None

    @staticmethod
    def _team_row(
        connection: sqlite3.Connection, team_run_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM team_runs WHERE id = ?", (team_run_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
        return row

    @staticmethod
    def _analysis_dependency_results(
        connection: sqlite3.Connection, team: sqlite3.Row, task_id: str,
    ) -> list[dict[str, Any]]:
        """Include only completed direct dependencies' reports, not other agents' chats."""
        rows = connection.execute(
            "SELECT m.id, m.task_id, m.payload_json FROM team_messages m "
            "JOIN task_attempts a ON a.id = m.attempt_id "
            "JOIN task_dependencies d ON d.task_list_id = ? AND d.blocker_id = m.task_id "
            "WHERE d.blocked_id = ? AND m.team_run_id = ? AND m.type = 'ANALYSIS_RESULT' "
            "AND a.state = 'succeeded' AND a.team_plan_revision = ? ORDER BY m.created_at, m.id",
            (str(team["task_list_id"]), task_id, str(team["id"]), team["active_plan_revision"]),
        ).fetchall()
        return [{"task_id": str(row["task_id"]), "message_id": str(row["id"]),
                 "summary": _json_loads(row["payload_json"], {}).get("summary", "")}
                for row in rows]

    def validate_team_plan_tasks(self, task_list_id: str, plan: Mapping[str, Any]) -> None:
        """Preflight API input before creating a Team; transactions recheck at approval/claim."""
        with self._lock:
            self._validate_team_plan_tasks(self._connection, task_list_id, plan)

    @staticmethod
    def _validate_team_plan_tasks(
        connection: sqlite3.Connection, task_list_id: str, plan: Mapping[str, Any]
    ) -> None:
        # Old plans contain descriptive strings, not task references. Their actual
        # Task metadata is still checked at claim time; never infer write permission.
        shared_context = plan.get("shared_context", "")
        if not isinstance(shared_context, str) or len(shared_context) > 8000:
            raise ValueError("Team shared_context must be text of at most 8000 characters")
        for item in plan.get("tasks", []):
            if not isinstance(item, Mapping) or not item.get("task_id"):
                continue
            row = connection.execute(
                "SELECT metadata_json FROM tasks WHERE task_list_id = ? AND id = ?",
                (task_list_id, str(item["task_id"])),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Team Task not found: {item['task_id']}")
            metadata = _json_loads(row["metadata_json"], {})
            validate_task_execution(metadata)
            validate_task_execution({**metadata, **item})
            for key in ("kind", "write_scopes", "risk_level", "plan_required"):
                if key not in item:
                    continue
                default = {
                    "write_scopes": [], "kind": "analysis",
                    "risk_level": "low", "plan_required": False,
                }[key]
                actual, expected = metadata.get(key, default), item[key]
                if key == "write_scopes":
                    actual = [path.replace("\\", "/").strip().strip("/") for path in actual]
                    expected = [path.replace("\\", "/").strip().strip("/") for path in expected]
                if actual != expected:
                    raise StorageConflictError(
                        f"Team Task {item['task_id']} {key} differs from the submitted plan"
                    )

    @staticmethod
    def _advance_team_after_task_completion(
        connection: sqlite3.Connection, team: sqlite3.Row, now: str
    ) -> None:
        revision = team["active_plan_revision"]
        if revision is None:
            return
        plan = connection.execute(
            "SELECT plan_json FROM team_plan_revisions "
            "WHERE team_run_id = ? AND revision = ?",
            (str(team["id"]), int(revision)),
        ).fetchone()
        if plan is None:
            return
        task_ids = [
            str(item.get("task_id"))
            for item in _json_loads(plan["plan_json"], {}).get("tasks", [])
            if isinstance(item, Mapping) and str(item.get("task_id") or "").strip()
        ]
        if not task_ids:
            return
        placeholders = ",".join("?" for _ in task_ids)
        remaining = int(
            connection.execute(
                f"SELECT COUNT(*) AS value FROM tasks WHERE task_list_id = ? "
                f"AND id IN ({placeholders}) "
                "AND status NOT IN ('completed', 'cancelled')",
                (str(team["task_list_id"]), *task_ids),
            ).fetchone()["value"]
        )
        if remaining:
            return
        candidate_count = int(
            connection.execute(
                "SELECT COUNT(*) AS value FROM candidates "
                "WHERE team_run_id = ? AND status = 'committed'",
                (str(team["id"]),),
            ).fetchone()["value"]
        )
        target = (
            TeamRunState.READY_FOR_MANUAL_INTEGRATION.value
            if candidate_count
            else TeamRunState.COMPLETED.value
        )
        connection.execute(
            "UPDATE team_runs SET state = ?, updated_at = ? "
            "WHERE id = ? AND state = 'running'",
            (target, now, str(team["id"])),
        )

    @staticmethod
    def _team_plan_row(
        connection: sqlite3.Connection, team_run_id: str, revision: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM team_plan_revisions WHERE team_run_id = ? AND revision = ?",
            (team_run_id, int(revision)),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(
                f"Team Plan revision not found: {team_run_id}:r{revision}"
            )
        return row

    @classmethod
    def _team_plan_from_connection(
        cls, connection: sqlite3.Connection, team_run_id: str, revision: int
    ) -> TeamPlanRevisionRecord:
        return _row_to_team_plan_revision(
            cls._team_plan_row(connection, team_run_id, revision)
        )

    @staticmethod
    def _attempt_from_connection(
        connection: sqlite3.Connection, attempt_id: str
    ) -> TaskAttemptRecord:
        row = connection.execute(
            "SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Task Attempt not found: {attempt_id}")
        return _row_to_task_attempt(row)

    @staticmethod
    def _team_command_result(
        connection: sqlite3.Connection,
        team_run_id: str,
        command_id: str,
        kind: str,
    ) -> JsonObject | None:
        row = connection.execute(
            "SELECT * FROM team_commands WHERE command_id = ?", (command_id,)
        ).fetchone()
        if row is None:
            return None
        if str(row["team_run_id"]) != team_run_id or str(row["kind"]) != kind:
            raise StorageConflictError("Command id was already used for another operation")
        return _json_loads(row["result_json"], {})

    @staticmethod
    def _record_team_command(
        connection: sqlite3.Connection,
        team_run_id: str,
        command_id: str,
        kind: str,
        result: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO team_commands(
                command_id, team_run_id, kind, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (command_id, team_run_id, kind, _json_dumps(dict(result)), created_at),
        )

    @classmethod
    def _append_team_event(
        cls,
        connection: sqlite3.Connection,
        team: sqlite3.Row,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        agent_id: str | None = None,
    ) -> RunEvent:
        return cls._append_event_in_transaction(
            connection,
            RunEvent(
                type=event_type,
                run_id=str(team["root_run_id"]),
                conversation_id=str(team["conversation_id"]),
                agent_id=agent_id or str(team["lead_agent_id"]),
                payload=dict(payload),
            ),
        )

    @staticmethod
    def _insert_team_message(
        connection: sqlite3.Connection,
        *,
        team: sqlite3.Row,
        sender_type: str,
        recipient_type: str,
        recipient_agent_id: str,
        recipient_generation: int,
        message_type: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        created_at: str,
        sender_agent_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        artifact_refs: Sequence[str] = (),
        priority: str = "normal",
    ) -> str:
        message_id = _new_id("teammsg")
        sequence_no = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(sequence_no), 0) + 1 AS value
                FROM team_messages
                WHERE team_run_id = ? AND recipient_agent_id = ?
                  AND recipient_generation = ?
                """,
                (
                    str(team["id"]),
                    recipient_agent_id,
                    recipient_generation,
                ),
            ).fetchone()["value"]
        )
        connection.execute(
            """
            INSERT INTO team_messages(
                id, team_run_id, sender_type, sender_agent_id,
                recipient_type, recipient_agent_id, recipient_generation,
                task_id, attempt_id, type, payload_version, payload_json,
                artifact_refs_json, correlation_id, causation_id, dedupe_key,
                sequence_no, priority, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                str(team["id"]),
                sender_type,
                sender_agent_id,
                recipient_type,
                recipient_agent_id,
                recipient_generation,
                task_id,
                attempt_id,
                message_type,
                _json_dumps(dict(payload)),
                _json_dumps([str(item) for item in artifact_refs]),
                correlation_id,
                causation_id,
                dedupe_key,
                sequence_no,
                priority,
                created_at,
            ),
        )
        return message_id

    # Messages ----------------------------------------------------------

    def create_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: Any,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> MessageRecord:
        identifier = message_id or _new_id("msg")
        clean_role = str(role).strip()
        if not clean_role:
            raise ValueError("Message role cannot be empty")
        now = created_at or utc_now_iso()
        try:
            with self._transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, run_id, role, content_json,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        conversation_id,
                        run_id,
                        clean_role,
                        _json_dumps(content),
                        _json_dumps(dict(metadata or {})),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise self._integrity_error(exc, "message", identifier) from exc
        return self._require_message(identifier)

    def get_message(self, message_id: str) -> MessageRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return _row_to_message(row) if row else None

    def _require_message(self, message_id: str) -> MessageRecord:
        record = self.get_message(message_id)
        if record is None:
            raise RecordNotFoundError(f"Message not found: {message_id}")
        return record

    def list_messages(
        self,
        conversation_id: str,
        *,
        run_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[MessageRecord]:
        where = "conversation_id = ?"
        parameters: list[Any] = [conversation_id]
        if run_id is not None:
            where += " AND run_id = ?"
            parameters.append(run_id)
        parameters.extend((_positive_limit(limit), max(0, offset)))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM messages WHERE {where}
                ORDER BY created_at, rowid
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def update_message(
        self,
        message_id: str,
        *,
        content: Any | object = _UNSET,
        metadata: Mapping[str, Any] | object = _UNSET,
    ) -> MessageRecord:
        assignments: list[str] = []
        parameters: list[Any] = []
        if content is not _UNSET:
            assignments.append("content_json = ?")
            parameters.append(_json_dumps(content))
        if metadata is not _UNSET:
            assignments.append("metadata_json = ?")
            parameters.append(_json_dumps(dict(metadata)))  # type: ignore[arg-type]
        if not assignments:
            return self._require_message(message_id)
        parameters.append(message_id)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE messages SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Message not found: {message_id}")
        return self._require_message(message_id)

    def delete_message(self, message_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE id = ?", (message_id,)
            )
        return cursor.rowcount > 0

    # Runs --------------------------------------------------------------

    def create_run(
        self,
        conversation_id: str,
        *,
        run_id: str | None = None,
        status: str = "queued",
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> RunRecord:
        _validate_choice("run status", status, RUN_STATUSES)
        identifier = run_id or _new_id("run")
        now = created_at or utc_now_iso()
        queue_position: int | None = None
        try:
            with self._transaction(immediate=True) as connection:
                if status == "queued":
                    row = connection.execute(
                        """
                        SELECT COALESCE(MAX(queue_position), 0) + 1 AS position
                        FROM runs WHERE status = 'queued'
                        """
                    ).fetchone()
                    queue_position = int(row["position"])
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, conversation_id, status, queue_position, created_at,
                        updated_at, started_at, finished_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        conversation_id,
                        status,
                        queue_position,
                        now,
                        now,
                        now if status == "running" else None,
                        now if status in TERMINAL_RUN_STATUSES else None,
                        _json_dumps(dict(metadata or {})),
                    ),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise self._integrity_error(exc, "run", identifier) from exc
        return self._require_run(identifier)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row else None

    def _require_run(self, run_id: str) -> RunRecord:
        record = self.get_run(run_id)
        if record is None:
            raise RecordNotFoundError(f"Run not found: {run_id}")
        return record

    def list_runs(
        self,
        *,
        conversation_id: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            parameters.append(conversation_id)
        if statuses:
            for status in statuses:
                _validate_choice("run status", status, RUN_STATUSES)
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(statuses)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend((_positive_limit(limit), max(0, offset)))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM runs {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error: Any | object = _UNSET,
        metadata: Mapping[str, Any] | object = _UNSET,
        at: str | None = None,
    ) -> RunRecord:
        _validate_choice("run status", status, RUN_STATUSES)
        now = at or utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            old_status = str(row["status"])
            if old_status != status and not _valid_run_transition(old_status, status):
                raise InvalidStateTransitionError(
                    f"Cannot transition run {run_id} from {old_status} to {status}"
                )
            old_position = row["queue_position"]
            assignments = ["status = ?", "updated_at = ?"]
            parameters: list[Any] = [status, now]
            if status == "running" and row["started_at"] is None:
                assignments.append("started_at = ?")
                parameters.append(now)
            if status in TERMINAL_RUN_STATUSES:
                assignments.extend(("finished_at = ?", "queue_position = NULL"))
                parameters.append(row["finished_at"] or now)
            elif old_status == "queued" and status != "queued":
                assignments.append("queue_position = NULL")
            if error is not _UNSET:
                assignments.append("error_json = ?")
                parameters.append(_json_dumps(error) if error is not None else None)
            if metadata is not _UNSET:
                assignments.append("metadata_json = ?")
                parameters.append(_json_dumps(dict(metadata)))  # type: ignore[arg-type]
            parameters.append(run_id)
            connection.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", parameters
            )
            if old_status == "queued" and status != "queued":
                self._dequeue_after(connection, old_position)
        return self._require_run(run_id)

    def start_run(self, run_id: str) -> RunRecord:
        return self.update_run_status(run_id, "running")

    def request_run_cancel(self, run_id: str) -> RunRecord:
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            status = str(row["status"])
            if status in TERMINAL_RUN_STATUSES:
                return _row_to_run(row)
            if status == "queued":
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'cancelled', queue_position = NULL,
                        cancel_requested_at = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, now, run_id),
                )
                self._dequeue_after(connection, row["queue_position"])
            else:
                connection.execute(
                    """
                    UPDATE runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, run_id),
                )
        return self._require_run(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        run = self._require_run(run_id)
        return run.cancel_requested_at is not None or run.status == "cancelled"

    def delete_run(self, run_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, queue_position FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            if row["status"] == "queued":
                self._dequeue_after(connection, row["queue_position"])
        return True

    def mark_incomplete_runs_interrupted(
        self,
        *,
        statuses: Sequence[str] = ("queued", "running"),
        reason: str = "Run interrupted during service startup.",
        at: str | None = None,
    ) -> int:
        chosen = tuple(statuses)
        if not chosen:
            return 0
        if any(status not in ACTIVE_RUN_STATUSES for status in chosen):
            raise ValueError("Only queued/running runs may be recovered as interrupted")
        now = at or utc_now_iso()
        placeholders = ",".join("?" for _ in chosen)
        error = _json_dumps({"type": "service_restart", "message": reason})
        recovered: list[sqlite3.Row] = []
        with self._transaction(immediate=True) as connection:
            recovered = connection.execute(
                f"SELECT id, conversation_id, next_event_seq FROM runs WHERE status IN ({placeholders})",
                chosen,
            ).fetchall()
            cursor = connection.execute(
                f"""
                UPDATE runs
                SET status = 'interrupted', queue_position = NULL,
                    finished_at = COALESCE(finished_at, ?), updated_at = ?,
                    error_json = COALESCE(error_json, ?)
                WHERE status IN ({placeholders})
                """,
                (now, now, error, *chosen),
            )
            for row in recovered:
                seq = int(row["next_event_seq"]) + 1
                connection.execute(
                    """
                    INSERT INTO events(
                        id, run_id, conversation_id, seq, type, occurred_at,
                        agent_id, payload_json
                    ) VALUES (?, ?, ?, ?, 'run.interrupted', ?, 'agent_root', ?)
                    """,
                    (
                        _new_id("evt"),
                        row["id"],
                        row["conversation_id"],
                        seq,
                        now,
                        _json_dumps({"status": "interrupted", "reason": reason}),
                    ),
                )
                connection.execute(
                    "UPDATE runs SET next_event_seq = ? WHERE id = ?",
                    (seq, row["id"]),
                )
        if recovered:
            with self._event_condition:
                self._event_condition.notify_all()
        return cursor.rowcount

    def recover_incomplete_team_runtime(self) -> int:
        """Freeze interrupted workers while preserving their recoverable Attempts."""

        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            attempts = connection.execute(
                """
                SELECT a.* FROM task_attempts a
                JOIN agent_sessions s ON s.id = a.session_id
                WHERE a.state NOT IN ('succeeded', 'failed', 'cancelled', 'orphaned')
                  AND (
                    s.state IN ('starting', 'work', 'suspect')
                    OR EXISTS (
                        SELECT 1 FROM tool_executions x
                        WHERE x.attempt_id = a.id AND x.status = 'running'
                    )
                  )
                """
            ).fetchall()
            attempt_ids = [str(row["id"]) for row in attempts]
            if attempt_ids:
                placeholders = ",".join("?" for _ in attempt_ids)
                connection.execute(
                    f"""
                    UPDATE task_attempts
                    SET state = 'waiting', write_enabled = 0, result_unknown = 1,
                        worker_exited_at = ?, finished_at = NULL, updated_at = ?,
                        error_json = ?
                    WHERE id IN ({placeholders})
                    """,
                    (
                        now,
                        now,
                        _json_dumps(
                            {
                                "type": "service_restart",
                                "message": "Attempt interrupted by service restart",
                            }
                        ),
                        *attempt_ids,
                    ),
                )
                connection.execute(
                    f"""
                    UPDATE worktree_bindings
                    SET state = 'frozen', write_enabled = 0,
                        frozen_reason = 'service_restart', updated_at = ?
                    WHERE attempt_id IN ({placeholders})
                    """,
                    (now, *attempt_ids),
                )
                connection.execute(
                    f"""
                    UPDATE tool_executions
                    SET status = 'failed', result_unknown = CASE
                            WHEN is_write = 1 THEN 1 ELSE result_unknown END,
                        error = COALESCE(error, 'service_restart'), finished_at = ?
                    WHERE status = 'running' AND attempt_id IN ({placeholders})
                    """,
                    (now, *attempt_ids),
                )
                team_ids = sorted({str(row["team_run_id"]) for row in attempts})
                for team_run_id in team_ids:
                    team = self._team_row(connection, team_run_id)
                    self._append_team_event(
                        connection,
                        team,
                        "team.runtime.recovered",
                        {
                            "team_run_id": team_run_id,
                            "interrupted_attempts": sum(
                                1
                                for attempt in attempts
                                if str(attempt["team_run_id"]) == team_run_id
                            ),
                            "action": "waiting_for_manual_recovery",
                        },
                    )
            connection.execute(
                """
                UPDATE agent_sessions
                SET state = 'lost', updated_at = ?,
                    waiting_reason = 'service_restart'
                WHERE state IN ('starting', 'work', 'suspect')
                   OR (
                        state IN ('idle', 'waiting')
                        AND EXISTS (
                            SELECT 1 FROM team_agents a
                            WHERE a.id = agent_sessions.agent_id
                              AND a.role = 'lead'
                        )
                   )
                """,
                (now,),
            )
            recovered_leads = self._recover_active_lead_sessions(connection, now)
        if attempts or recovered_leads:
            self._notify_activity()
        return len(attempts)

    def _recover_active_lead_sessions(
        self, connection: sqlite3.Connection, now: str
    ) -> int:
        """Create one fresh Lead Session for each non-terminal TeamRun."""

        placeholders = ",".join("?" for _ in _TERMINAL_TEAM_RUN_STATES)
        teams = connection.execute(
            f"""
            SELECT id, lead_agent_id FROM team_runs
            WHERE state NOT IN ({placeholders})
            """,
            tuple(sorted(_TERMINAL_TEAM_RUN_STATES)),
        ).fetchall()
        recovered = 0
        for team in teams:
            lead_agent_id = str(team["lead_agent_id"])
            active = connection.execute(
                "SELECT 1 FROM agent_sessions WHERE agent_id = ? "
                "AND state IN ('starting','idle','work','waiting','suspect')",
                (lead_agent_id,),
            ).fetchone()
            if active is not None:
                continue
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
                    "FROM agent_sessions WHERE agent_id = ?",
                    (lead_agent_id,),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO agent_sessions(
                    id, team_run_id, agent_id, generation, state,
                    heartbeat_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'idle', ?, ?, ?)
                """,
                (
                    _new_id("session"),
                    str(team["id"]),
                    lead_agent_id,
                    generation,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE team_messages SET recipient_generation = ?
                WHERE team_run_id = ? AND recipient_type = 'lead'
                  AND recipient_agent_id = ? AND acked_at IS NULL
                """,
                (generation, str(team["id"]), lead_agent_id),
            )
            recovered += 1
        return recovered

    @staticmethod
    def _dequeue_after(
        connection: sqlite3.Connection, old_position: int | None
    ) -> None:
        if old_position is None:
            return
        connection.execute(
            """
            UPDATE runs SET queue_position = queue_position - 1
            WHERE status = 'queued' AND queue_position > ?
            """,
            (old_position,),
        )

    # Events ------------------------------------------------------------

    def append_event(self, event: RunEvent) -> RunEvent:
        """Append an event and atomically assign its run-local sequence.

        ``event.seq`` is intentionally ignored.  The returned event contains the
        persisted sequence.  Re-appending the same event id is idempotent.
        """

        if not isinstance(event, RunEvent):
            raise TypeError("append_event expects a RunEvent")
        if not event.run_id:
            raise ValueError("Event run_id is required")
        with self._transaction(immediate=True) as connection:
            persisted = self._append_event_in_transaction(connection, event)
        with self._event_condition:
            self._event_condition.notify_all()
        return persisted

    @staticmethod
    def _append_event_in_transaction(
        connection: sqlite3.Connection,
        event: RunEvent,
    ) -> RunEvent:
        """Append an event using the caller's transaction.

        Team commands use this helper so a lifecycle transition and its audit
        event either both commit or both roll back.
        """

        existing = connection.execute(
            "SELECT * FROM events WHERE id = ?", (event.id,)
        ).fetchone()
        if existing is not None:
            persisted = _row_to_event(existing)
            if persisted.run_id != event.run_id:
                raise StorageConflictError(
                    f"Event id already belongs to another run: {event.id}"
                )
            return persisted

        run_row = connection.execute(
            "SELECT conversation_id, next_event_seq FROM runs WHERE id = ?",
            (event.run_id,),
        ).fetchone()
        if run_row is None:
            raise RecordNotFoundError(f"Run not found: {event.run_id}")
        conversation_id = str(run_row["conversation_id"])
        if event.conversation_id and event.conversation_id != conversation_id:
            raise StorageConflictError("Event conversation_id does not match its run")
        seq = int(run_row["next_event_seq"]) + 1
        persisted = replace(
            event,
            seq=seq,
            conversation_id=conversation_id,
            payload=redact_payload(event.payload),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, run_id, conversation_id, seq, type, occurred_at,
                turn_id, agent_id, parent_agent_id, iteration, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persisted.id,
                persisted.run_id,
                persisted.conversation_id,
                persisted.seq,
                persisted.type,
                persisted.occurred_at,
                persisted.turn_id,
                persisted.agent_id,
                persisted.parent_agent_id,
                persisted.iteration,
                _json_dumps(persisted.payload),
            ),
        )
        connection.execute(
            "UPDATE runs SET next_event_seq = ?, updated_at = ? WHERE id = ?",
            (seq, persisted.occurred_at, persisted.run_id),
        )
        return persisted

    def get_event(self, event_id: str) -> RunEvent | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return _row_to_event(row) if row else None

    def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[RunEvent]:
        parameters: list[Any] = [run_id, max(0, int(after_seq))]
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(_positive_limit(limit))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def wait_for_events(
        self,
        run_id: str,
        after_seq: int = 0,
        timeout: float = 15.0,
    ) -> list[RunEvent]:
        """Wait for replayable events without holding the database lock."""

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            events = self.list_events(run_id, after_seq=after_seq)
            if events or timeout <= 0 or self._closed:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            # Periodic wakeups also observe writes made by another process.
            with self._event_condition:
                self._event_condition.wait(timeout=min(remaining, 0.25))

    def delete_event(self, event_id: str) -> bool:
        """Delete an event for administrative cleanup; sequence gaps are retained."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return cursor.rowcount > 0

    # Approvals ---------------------------------------------------------

    def create_approval(
        self,
        run_id: str,
        *,
        tool_name: str,
        tool_input: Mapping[str, Any],
        reason: str,
        approval_id: str | None = None,
        event_seq: int | None = None,
        tool_call_id: str | None = None,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> ApprovalRecord:
        identifier = approval_id or _new_id("approval")
        now = created_at or utc_now_iso()
        with self._transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT conversation_id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            try:
                connection.execute(
                    """
                    INSERT INTO approvals(
                        id, conversation_id, run_id, event_seq, tool_call_id,
                        tool_name, tool_input_json, reason, status, created_at,
                        expires_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        identifier,
                        run["conversation_id"],
                        run_id,
                        event_seq,
                        tool_call_id,
                        tool_name,
                        _json_dumps(dict(tool_input)),
                        reason,
                        now,
                        expires_at,
                        _json_dumps(dict(metadata or {})),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(
                    f"Approval already exists: {identifier}"
                ) from exc
        return self._require_approval(identifier)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _row_to_approval(row) if row else None

    def _require_approval(self, approval_id: str) -> ApprovalRecord:
        record = self.get_approval(approval_id)
        if record is None:
            raise RecordNotFoundError(f"Approval not found: {approval_id}")
        return record

    def list_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            conditions.append("run_id = ?")
            parameters.append(run_id)
        if status is not None:
            _validate_choice("approval status", status, APPROVAL_STATUSES)
            conditions.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(_positive_limit(limit))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM approvals {where}
                ORDER BY created_at, id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        resolved_at: str | None = None,
    ) -> ApprovalRecord:
        """Resolve once; later calls return the immutable existing decision."""

        _validate_choice("approval decision", decision, APPROVAL_DECISIONS)
        status = "allowed" if decision == "allow" else "denied"
        now = resolved_at or utc_now_iso()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Approval not found: {approval_id}")
            if row["status"] == "pending":
                connection.execute(
                    """
                    UPDATE approvals
                    SET status = ?, decision = ?, resolved_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (status, decision, now, approval_id),
                )
        return self._require_approval(approval_id)

    def expire_pending_approvals(
        self, *, now: str | None = None, approval_id: str | None = None
    ) -> list[ApprovalRecord]:
        timestamp = now or utc_now_iso()
        with self._transaction(immediate=True) as connection:
            id_filter = " AND id = ?" if approval_id is not None else ""
            expiry_filter = "" if approval_id is not None else " AND expires_at IS NOT NULL AND expires_at <= ?"
            parameters: tuple[Any, ...] = (
                (approval_id,) if approval_id is not None else (timestamp,)
            )
            rows = connection.execute(
                f"""
                SELECT id FROM approvals
                WHERE status = 'pending'{expiry_filter}{id_filter}
                """,
                parameters,
            ).fetchall()
            identifiers = [str(row["id"]) for row in rows]
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                connection.execute(
                    f"""
                    UPDATE approvals
                    SET status = 'expired', decision = 'deny', resolved_at = ?
                    WHERE id IN ({placeholders}) AND status = 'pending'
                    """,
                    (timestamp, *identifiers),
                )
        return [self._require_approval(identifier) for identifier in identifiers]

    def delete_approval(self, approval_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM approvals WHERE id = ?", (approval_id,)
            )
        return cursor.rowcount > 0

    # Model calls and usage --------------------------------------------

    def create_model_call(
        self,
        run_id: str,
        *,
        model: str,
        call_kind: str = "main",
        agent_id: str = "agent_root",
        parent_agent_id: str | None = None,
        provider: str = "anthropic-compatible",
        status: str = "running",
        usage: TokenUsage | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        model_call_id: str | None = None,
        started_at: str | None = None,
    ) -> ModelCallRecord:
        _validate_choice("model call status", status, MODEL_CALL_STATUSES)
        identifier = model_call_id or _new_id("call")
        now = started_at or utc_now_iso()
        normalized = _normalize_usage(usage)
        with self._transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT conversation_id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            try:
                connection.execute(
                    """
                    INSERT INTO model_calls(
                        id, conversation_id, run_id, agent_id, parent_agent_id,
                        provider, model, call_kind, status, started_at,
                        completed_at, input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        usage_available, estimated, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        run["conversation_id"],
                        run_id,
                        agent_id,
                        parent_agent_id,
                        provider,
                        model,
                        call_kind,
                        status,
                        now,
                        now if status != "running" else None,
                        normalized["input_tokens"],
                        normalized["output_tokens"],
                        normalized["cache_creation_input_tokens"],
                        normalized["cache_read_input_tokens"],
                        int(normalized["available"]),
                        int(normalized["estimated"]),
                        _json_dumps(dict(metadata or {})),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(
                    f"Model call already exists: {identifier}"
                ) from exc
        return self._require_model_call(identifier)

    def get_model_call(self, model_call_id: str) -> ModelCallRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM model_calls WHERE id = ?", (model_call_id,)
            ).fetchone()
        return _row_to_model_call(row) if row else None

    def _require_model_call(self, model_call_id: str) -> ModelCallRecord:
        record = self.get_model_call(model_call_id)
        if record is None:
            raise RecordNotFoundError(f"Model call not found: {model_call_id}")
        return record

    def complete_model_call(
        self,
        model_call_id: str,
        *,
        usage: TokenUsage | Mapping[str, Any] | None = None,
        status: str = "completed",
        error: Any | None = None,
        duration_ms: int | None = None,
        completed_at: str | None = None,
        metadata: Mapping[str, Any] | object = _UNSET,
    ) -> ModelCallRecord:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Completed model call status must be terminal")
        now = completed_at or utc_now_iso()
        normalized = _normalize_usage(usage)
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM model_calls WHERE id = ?", (model_call_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Model call not found: {model_call_id}")
            if duration_ms is None:
                duration_ms = _duration_ms(str(row["started_at"]), now)
            assignments = [
                "status = ?",
                "completed_at = ?",
                "duration_ms = ?",
                "input_tokens = ?",
                "output_tokens = ?",
                "cache_creation_input_tokens = ?",
                "cache_read_input_tokens = ?",
                "usage_available = ?",
                "estimated = ?",
                "error_json = ?",
            ]
            parameters: list[Any] = [
                status,
                now,
                duration_ms,
                normalized["input_tokens"],
                normalized["output_tokens"],
                normalized["cache_creation_input_tokens"],
                normalized["cache_read_input_tokens"],
                int(normalized["available"]),
                int(normalized["estimated"]),
                _json_dumps(error) if error is not None else None,
            ]
            if metadata is not _UNSET:
                assignments.append("metadata_json = ?")
                parameters.append(_json_dumps(dict(metadata)))  # type: ignore[arg-type]
            parameters.append(model_call_id)
            connection.execute(
                f"UPDATE model_calls SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
        return self._require_model_call(model_call_id)

    def record_model_call(
        self,
        run_id: str,
        *,
        model: str,
        usage: TokenUsage | Mapping[str, Any] | None,
        call_kind: str = "main",
        agent_id: str = "agent_root",
        parent_agent_id: str | None = None,
        provider: str = "anthropic-compatible",
        status: str = "completed",
        error: Any | None = None,
        duration_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        model_call_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> ModelCallRecord:
        call = self.create_model_call(
            run_id,
            model=model,
            call_kind=call_kind,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            provider=provider,
            metadata=metadata,
            model_call_id=model_call_id,
            started_at=started_at,
        )
        return self.complete_model_call(
            call.id,
            usage=usage,
            status=status,
            error=error,
            duration_ms=duration_ms,
            completed_at=completed_at,
        )

    def list_model_calls(
        self,
        *,
        run_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 1000,
    ) -> list[ModelCallRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            conditions.append("run_id = ?")
            parameters.append(run_id)
        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            parameters.append(conversation_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(_positive_limit(limit))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM model_calls {where}
                ORDER BY started_at, id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_row_to_model_call(row) for row in rows]

    def delete_model_call(self, model_call_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM model_calls WHERE id = ?", (model_call_id,)
            )
        return cursor.rowcount > 0

    def aggregate_usage(
        self,
        *,
        run_id: str | None = None,
        conversation_id: str | None = None,
        include_breakdown: bool = True,
    ) -> JsonObject:
        conditions: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            conditions.append("run_id = ?")
            parameters.append(run_id)
        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            parameters.append(conversation_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT * FROM model_calls {where}", parameters
            ).fetchall()
        result = _usage_totals(rows)
        if include_breakdown:
            result["by_model"] = _usage_groups(rows, "model")
            result["by_call_kind"] = _usage_groups(rows, "call_kind")
            result["by_agent"] = _usage_groups(rows, "agent_id")
        return result

    # Checkpoints -------------------------------------------------------

    def save_checkpoint(
        self,
        run_id: str,
        *,
        messages: Sequence[Mapping[str, Any]],
        todos: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        checkpoint_id: str | None = None,
        created_at: str | None = None,
    ) -> CheckpointRecord:
        """Persist the recovery state for a terminal run."""

        identifier = checkpoint_id or _new_id("checkpoint")
        now = created_at or utc_now_iso()
        with self._transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT conversation_id, status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            if run["status"] not in TERMINAL_RUN_STATUSES:
                raise InvalidStateTransitionError(
                    f"Cannot checkpoint non-terminal run {run_id}"
                )
            self._upsert_checkpoint(
                connection,
                checkpoint_id=identifier,
                conversation_id=str(run["conversation_id"]),
                run_id=run_id,
                run_status=str(run["status"]),
                messages=messages,
                todos=todos,
                context=context,
                metadata=metadata,
                created_at=now,
            )
        return self._require_checkpoint_for_run(run_id)

    def finish_run_with_checkpoint(
        self,
        run_id: str,
        *,
        status: str,
        messages: Sequence[Mapping[str, Any]],
        todos: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        error: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        checkpoint_metadata: Mapping[str, Any] | None = None,
        checkpoint_id: str | None = None,
        finished_at: str | None = None,
    ) -> tuple[RunRecord, CheckpointRecord]:
        """Atomically mark a run terminal and persist its recovery snapshot."""

        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish_run_with_checkpoint requires a terminal status")
        now = finished_at or utc_now_iso()
        identifier = checkpoint_id or _new_id("checkpoint")
        with self._transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RecordNotFoundError(f"Run not found: {run_id}")
            old_status = str(run["status"])
            if old_status != status and not _valid_run_transition(old_status, status):
                raise InvalidStateTransitionError(
                    f"Cannot transition run {run_id} from {old_status} to {status}"
                )
            run_metadata = (
                _json_dumps(dict(metadata))
                if metadata is not None
                else str(run["metadata_json"])
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, queue_position = NULL, updated_at = ?,
                    finished_at = COALESCE(finished_at, ?), error_json = ?,
                    metadata_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    now,
                    _json_dumps(error) if error is not None else None,
                    run_metadata,
                    run_id,
                ),
            )
            if old_status == "queued":
                self._dequeue_after(connection, run["queue_position"])
            self._upsert_checkpoint(
                connection,
                checkpoint_id=identifier,
                conversation_id=str(run["conversation_id"]),
                run_id=run_id,
                run_status=status,
                messages=messages,
                todos=todos,
                context=context,
                metadata=checkpoint_metadata,
                created_at=now,
            )
        return self._require_run(run_id), self._require_checkpoint_for_run(run_id)

    @staticmethod
    def _upsert_checkpoint(
        connection: sqlite3.Connection,
        *,
        checkpoint_id: str,
        conversation_id: str,
        run_id: str,
        run_status: str,
        messages: Sequence[Mapping[str, Any]],
        todos: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        metadata: Mapping[str, Any] | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO checkpoints(
                id, conversation_id, run_id, run_status, messages_json,
                todos_json, context_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                run_status = excluded.run_status,
                messages_json = excluded.messages_json,
                todos_json = excluded.todos_json,
                context_json = excluded.context_json,
                metadata_json = excluded.metadata_json,
                created_at = excluded.created_at
            """,
            (
                checkpoint_id,
                conversation_id,
                run_id,
                run_status,
                _json_dumps(list(messages)),
                _json_dumps(list(todos)),
                _json_dumps(dict(context)),
                _json_dumps(dict(metadata or {})),
                created_at,
            ),
        )

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return _row_to_checkpoint(row) if row else None

    def get_checkpoint_for_run(self, run_id: str) -> CheckpointRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_checkpoint(row) if row else None

    def _require_checkpoint_for_run(self, run_id: str) -> CheckpointRecord:
        record = self.get_checkpoint_for_run(run_id)
        if record is None:
            raise RecordNotFoundError(f"Checkpoint not found for run: {run_id}")
        return record

    def get_latest_checkpoint(
        self, conversation_id: str
    ) -> CheckpointRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return _row_to_checkpoint(row) if row else None

    def list_checkpoints(
        self, conversation_id: str, *, limit: int = 100
    ) -> list[CheckpointRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM checkpoints WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (conversation_id, _positive_limit(limit)),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _integrity_error(
        error: sqlite3.IntegrityError, entity: str, identifier: str
    ) -> StorageError:
        message = str(error).casefold()
        if "foreign key" in message:
            return RecordNotFoundError(
                f"Referenced record for {entity} does not exist: {identifier}"
            )
        if "one_active_run_per_conversation" in message or (
            entity == "run" and "unique constraint" in message
        ):
            return StorageConflictError(
                "Conversation already has an active run or run id is duplicated"
            )
        return StorageConflictError(f"Duplicate {entity}: {identifier}")


class SQLiteEventSink:
    """Adapt the repository to the framework-neutral ``EventSink`` protocol."""

    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def emit(self, event: RunEvent) -> None:
        self.repository.append_event(event)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _positive_limit(value: int) -> int:
    return max(1, min(int(value), 10_000))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _validate_choice(name: str, value: str, choices: frozenset[str]) -> None:
    if value not in choices:
        options = ", ".join(sorted(choices))
        raise ValueError(f"Invalid {name} {value!r}; expected one of: {options}")


def _valid_run_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current == "queued":
        return target in {"running", *TERMINAL_RUN_STATUSES}
    if current == "running":
        return target in TERMINAL_RUN_STATUSES
    return False


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(exclude_none=True)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _team_task_resource_keys(metadata: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return normalized exclusive resources declared by one existing Task."""

    result: list[tuple[str, str]] = []
    task_kind = str(metadata.get("kind") or "analysis").strip().lower()
    write_scopes = metadata.get("write_scopes") or []
    if not isinstance(write_scopes, Sequence) or isinstance(write_scopes, str):
        raise ValueError("Task write_scopes must be a list")
    for raw_scope in write_scopes:
        scope = str(raw_scope).replace("\\", "/").strip().strip("/")
        if not scope or any(part in {"", ".", ".."} for part in scope.split("/")):
            raise ValueError(f"Invalid Task write scope: {raw_scope}")
        result.append(("path", scope.casefold()))
    if task_kind == "code" and not result:
        result.append(("repository", "*"))
    exclusive = metadata.get("exclusive_resources") or []
    if not isinstance(exclusive, Sequence) or isinstance(exclusive, str):
        raise ValueError("Task exclusive_resources must be a list")
    for raw_resource in exclusive:
        resource = str(raw_resource).strip().casefold()
        if not resource:
            raise ValueError("Task exclusive resource cannot be empty")
        result.append(("logical", resource))
    return list(dict.fromkeys(result))


def _normalize_write_scopes(scopes: Sequence[str]) -> list[str]:
    result: list[str] = []
    for raw_scope in scopes:
        scope = str(raw_scope).replace("\\", "/").strip().strip("/")
        if not scope or any(part in {"", ".", ".."} for part in scope.split("/")):
            raise ValueError(f"Invalid write scope: {raw_scope}")
        result.append(scope)
    return list(dict.fromkeys(result))


def _normalize_risk_level(value: str) -> str:
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {"medium_high": "high", "medium-low": "medium"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"low", "medium", "high"}:
        raise ValueError(f"Unsupported risk level: {value}")
    return normalized


def _risk_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}[_normalize_risk_level(value)]


def _scope_is_within(scope: str, approved_scopes: Sequence[str]) -> bool:
    normalized = str(scope).replace("\\", "/").strip("/").casefold()
    return any(
        normalized == approved.casefold()
        or normalized.startswith(approved.casefold().rstrip("/") + "/")
        for approved in approved_scopes
    )


def _attempt_scope_hash(
    *,
    task_id: str,
    team_plan_revision: int,
    base_commit: str,
    write_scopes: Sequence[str],
    risk_level: str,
) -> str:
    payload = {
        "task_id": str(task_id),
        "team_plan_revision": int(team_plan_revision),
        "base_commit": str(base_commit),
        "write_scopes": sorted(_normalize_write_scopes(write_scopes)),
        "risk_level": _normalize_risk_level(risk_level),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _team_resources_overlap(
    first_kind: str,
    first_key: str,
    second_kind: str,
    second_key: str,
) -> bool:
    if first_kind == "repository" or second_kind == "repository":
        return True
    if first_kind != second_kind:
        return False
    if first_kind != "path":
        return first_key == second_key
    first = first_key.rstrip("/")
    second = second_key.rstrip("/")
    return first == second or first.startswith(second + "/") or second.startswith(
        first + "/"
    )


def _row_to_conversation(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        id=str(row["id"]),
        title=str(row["title"]),
        workspace=str(row["workspace"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
        active_task_list_id=row["active_task_list_id"],
    )


def _row_to_task_list(row: sqlite3.Row) -> TaskListRecord:
    return TaskListRecord(
        id=str(row["id"]),
        workspace=str(row["workspace"]),
        name=str(row["name"]),
        scope=TaskListScope(str(row["scope"])),
        origin_conversation_id=row["origin_conversation_id"],
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
    )


def _row_to_task_activity(row: sqlite3.Row) -> TaskActivityRecord:
    return TaskActivityRecord(
        id=int(row["id"]),
        task_list_id=str(row["task_list_id"]),
        task_id=str(row["task_id"]),
        event_type=str(row["event_type"]),
        conversation_id=row["conversation_id"],
        run_id=row["run_id"],
        agent_id=row["agent_id"],
        payload=_json_loads(row["payload_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_team_run(row: sqlite3.Row) -> TeamRunRecord:
    return TeamRunRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        root_run_id=str(row["root_run_id"]),
        task_list_id=str(row["task_list_id"]),
        lead_agent_id=str(row["lead_agent_id"]),
        base_commit=str(row["base_commit"]),
        state=TeamRunState(str(row["state"])),
        active_plan_revision=row["active_plan_revision"],
        max_teammates=int(row["max_teammates"]),
        token_budget=row["token_budget"],
        model_call_budget=row["model_call_budget"],
        deadline_at=row["deadline_at"],
        metadata=_json_loads(row["metadata_json"], {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_team_plan_revision(row: sqlite3.Row) -> TeamPlanRevisionRecord:
    return TeamPlanRevisionRecord(
        team_run_id=str(row["team_run_id"]),
        revision=int(row["revision"]),
        status=TeamPlanStatus(str(row["status"])),
        plan=_json_loads(row["plan_json"], {}),
        plan_hash=str(row["plan_hash"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        submitted_at=row["submitted_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
    )


def _row_to_team_agent(row: sqlite3.Row) -> TeamAgentRecord:
    return TeamAgentRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        role=TeamAgentRole(str(row["role"])),
        name=str(row["name"]),
        model=str(row["model"]),
        capabilities=tuple(
            str(item) for item in _json_loads(row["capabilities_json"], [])
        ),
        created_at=str(row["created_at"]),
    )


def _row_to_agent_session(row: sqlite3.Row) -> AgentSessionRecord:
    return AgentSessionRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        agent_id=str(row["agent_id"]),
        generation=int(row["generation"]),
        state=AgentSessionState(str(row["state"])),
        current_attempt_id=row["current_attempt_id"],
        checkpoint_id=row["checkpoint_id"],
        heartbeat_at=str(row["heartbeat_at"]),
        waiting_reason=row["waiting_reason"],
        failure=_json_loads(row["failure_json"], None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_agent_session_checkpoint(
    row: sqlite3.Row,
) -> AgentSessionCheckpointRecord:
    return AgentSessionCheckpointRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        generation=int(row["generation"]),
        revision=int(row["revision"]),
        messages=_json_loads(row["messages_json"], []),
        context=_json_loads(row["context_json"], {}),
        metadata=_json_loads(row["metadata_json"], {}),
        safe_boundary=str(row["safe_boundary"]),
        created_at=str(row["created_at"]),
    )


def _row_to_task_attempt(row: sqlite3.Row) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        task_list_id=str(row["task_list_id"]),
        task_id=str(row["task_id"]),
        agent_id=str(row["agent_id"]),
        session_id=str(row["session_id"]),
        ordinal=int(row["ordinal"]),
        state=TaskAttemptState(str(row["state"])),
        team_plan_revision=int(row["team_plan_revision"]),
        attempt_base_commit=str(row["attempt_base_commit"]),
        lease_token=str(row["lease_token"]),
        write_enabled=bool(row["write_enabled"]),
        result_unknown=bool(row["result_unknown"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested_at=row["cancel_requested_at"],
        worker_exited_at=row["worker_exited_at"],
        error=_json_loads(row["error_json"], None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_team_message(row: sqlite3.Row) -> TeamMessageRecord:
    return TeamMessageRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        sender_type=str(row["sender_type"]),
        sender_agent_id=row["sender_agent_id"],
        recipient_type=str(row["recipient_type"]),
        recipient_agent_id=str(row["recipient_agent_id"]),
        recipient_generation=int(row["recipient_generation"]),
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        type=str(row["type"]),
        payload_version=int(row["payload_version"]),
        payload=_json_loads(row["payload_json"], {}),
        artifact_refs=tuple(
            str(item) for item in _json_loads(row["artifact_refs_json"], [])
        ),
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        dedupe_key=str(row["dedupe_key"]),
        sequence_no=int(row["sequence_no"]),
        priority=str(row["priority"]),
        created_at=str(row["created_at"]),
        delivered_at=row["delivered_at"],
        acked_at=row["acked_at"],
        delivery_attempts=int(row["delivery_attempts"]),
        last_delivery_error=row["last_delivery_error"],
    )


def _row_to_resource_lease(row: sqlite3.Row) -> ResourceLeaseRecord:
    return ResourceLeaseRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        attempt_id=str(row["attempt_id"]),
        resource_kind=str(row["resource_kind"]),
        resource_key=str(row["resource_key"]),
        generation=int(row["generation"]),
        state=str(row["state"]),
        acquired_at=str(row["acquired_at"]),
        released_at=row["released_at"],
    )


def _row_to_worktree_binding(row: sqlite3.Row) -> WorktreeBindingRecord:
    return WorktreeBindingRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        attempt_id=str(row["attempt_id"]),
        agent_id=str(row["agent_id"]),
        session_id=str(row["session_id"]),
        generation=int(row["generation"]),
        path=str(row["path"]),
        branch=str(row["branch"]),
        base_commit=str(row["base_commit"]),
        head_commit=str(row["head_commit"]),
        fingerprint=str(row["fingerprint"]),
        state=str(row["state"]),
        write_scopes=tuple(
            str(item) for item in _json_loads(row["write_scopes_json"], [])
        ),
        write_enabled=bool(row["write_enabled"]),
        frozen_reason=row["frozen_reason"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_tool_execution(row: sqlite3.Row) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        agent_id=str(row["agent_id"]),
        session_id=str(row["session_id"]),
        task_id=str(row["task_id"]),
        attempt_id=str(row["attempt_id"]),
        worktree_id=row["worktree_id"],
        tool_call_id=str(row["tool_call_id"]),
        trace_id=row["trace_id"],
        plan_revision=row["plan_revision"],
        tool_name=str(row["tool_name"]),
        risk=str(row["risk"]),
        status=str(row["status"]),
        is_write=bool(row["is_write"]),
        result_unknown=bool(row["result_unknown"]),
        input=_json_loads(row["input_json"], {}),
        output_ref=row["output_ref"],
        error=row["error"],
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
    )


def _row_to_attempt_plan(row: sqlite3.Row) -> AttemptPlanRecord:
    return AttemptPlanRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        attempt_id=str(row["attempt_id"]),
        revision=int(row["revision"]),
        status=AttemptPlanStatus(str(row["status"])),
        summary=str(row["summary"]),
        planned_files=tuple(
            str(item) for item in _json_loads(row["planned_files_json"], [])
        ),
        planned_commands=tuple(
            str(item) for item in _json_loads(row["planned_commands_json"], [])
        ),
        planned_tests=tuple(
            str(item) for item in _json_loads(row["planned_tests_json"], [])
        ),
        write_scopes=tuple(
            str(item) for item in _json_loads(row["write_scopes_json"], [])
        ),
        scope_hash=str(row["scope_hash"]),
        risk_level=str(row["risk_level"]),
        team_plan_revision=int(row["team_plan_revision"]),
        base_commit=str(row["base_commit"]),
        worktree_fingerprint=str(row["worktree_fingerprint"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        submitted_at=row["submitted_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
    )


def _row_to_candidate(row: sqlite3.Row) -> CandidateRecord:
    return CandidateRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        task_id=str(row["task_id"]),
        attempt_id=str(row["attempt_id"]),
        revision=int(row["revision"]),
        status=CandidateStatus(str(row["status"])),
        summary=str(row["summary"]),
        changed_files=tuple(
            str(item) for item in _json_loads(row["changed_files_json"], [])
        ),
        untracked_files=tuple(
            str(item) for item in _json_loads(row["untracked_files_json"], [])
        ),
        diff_ref=str(row["diff_ref"]),
        diff_hash=str(row["diff_hash"]),
        base_commit=str(row["base_commit"]),
        worktree_head=str(row["worktree_head"]),
        tests_reported=tuple(
            str(item) for item in _json_loads(row["tests_reported_json"], [])
        ),
        known_risks=tuple(
            str(item) for item in _json_loads(row["known_risks_json"], [])
        ),
        submitted_by=str(row["submitted_by"]),
        submitted_at=str(row["submitted_at"]),
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        review_reason=row["review_reason"],
        user_approval_required=bool(row["user_approval_required"]),
        user_decision=row["user_decision"],
        user_decided_by=row["user_decided_by"],
        user_decided_at=row["user_decided_at"],
        user_decision_reason=row["user_decision_reason"],
        commit_hash=row["commit_hash"],
        committed_at=row["committed_at"],
        integrated_commit=row["integrated_commit"],
        integrated_at=row["integrated_at"],
    )


def _row_to_validation_run(row: sqlite3.Row) -> ValidationRunRecord:
    return ValidationRunRecord(
        id=str(row["id"]),
        team_run_id=str(row["team_run_id"]),
        attempt_id=str(row["attempt_id"]),
        candidate_id=str(row["candidate_id"]),
        command=str(row["command"]),
        status=str(row["status"]),
        exit_code=row["exit_code"],
        output_ref=row["output_ref"],
        duration_ms=row["duration_ms"],
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
    )


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=row["run_id"],
        role=str(row["role"]),
        content=_json_loads(row["content_json"], ""),
        metadata=_json_loads(row["metadata_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        status=str(row["status"]),
        queue_position=row["queue_position"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested_at=row["cancel_requested_at"],
        error=_json_loads(row["error_json"], None),
        metadata=_json_loads(row["metadata_json"], {}),
        next_event_seq=int(row["next_event_seq"]),
    )


def _row_to_event(row: sqlite3.Row) -> RunEvent:
    return RunEvent(
        id=str(row["id"]),
        seq=int(row["seq"]),
        type=str(row["type"]),
        occurred_at=str(row["occurred_at"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        turn_id=str(row["turn_id"]),
        agent_id=str(row["agent_id"]),
        parent_agent_id=row["parent_agent_id"],
        iteration=row["iteration"],
        payload=_json_loads(row["payload_json"], {}),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        event_seq=row["event_seq"],
        tool_call_id=row["tool_call_id"],
        tool_name=str(row["tool_name"]),
        tool_input=_json_loads(row["tool_input_json"], {}),
        reason=str(row["reason"]),
        status=str(row["status"]),
        decision=row["decision"],
        created_at=str(row["created_at"]),
        expires_at=row["expires_at"],
        resolved_at=row["resolved_at"],
        metadata=_json_loads(row["metadata_json"], {}),
    )


def _row_to_model_call(row: sqlite3.Row) -> ModelCallRecord:
    return ModelCallRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        agent_id=str(row["agent_id"]),
        parent_agent_id=row["parent_agent_id"],
        provider=str(row["provider"]),
        model=str(row["model"]),
        call_kind=str(row["call_kind"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        completed_at=row["completed_at"],
        duration_ms=row["duration_ms"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        cache_read_input_tokens=row["cache_read_input_tokens"],
        usage_available=bool(row["usage_available"]),
        estimated=bool(row["estimated"]),
        error=_json_loads(row["error_json"], None),
        metadata=_json_loads(row["metadata_json"], {}),
    )


def _row_to_checkpoint(row: sqlite3.Row) -> CheckpointRecord:
    return CheckpointRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        run_status=str(row["run_status"]),
        messages=_json_loads(row["messages_json"], []),
        todos=_json_loads(row["todos_json"], []),
        context=_json_loads(row["context_json"], {}),
        metadata=_json_loads(row["metadata_json"], {}),
        created_at=str(row["created_at"]),
    )


def _normalize_usage(
    usage: TokenUsage | Mapping[str, Any] | None,
) -> JsonObject:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "available": False,
            "estimated": False,
        }
    source = usage.to_dict() if isinstance(usage, TokenUsage) else dict(usage)
    available = bool(source.get("available", True))
    result: JsonObject = {
        "available": available,
        "estimated": bool(source.get("estimated", False)),
    }
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if not available:
            result[name] = None
            continue
        value = source.get(name, 0)
        try:
            result[name] = max(0, int(value or 0))
        except (TypeError, ValueError):
            result[name] = 0
    return result


def _usage_totals(rows: Sequence[sqlite3.Row]) -> JsonObject:
    result: JsonObject = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
        "model_calls": len(rows),
        "available_calls": 0,
        "unavailable_calls": 0,
        "estimated": False,
    }
    for row in rows:
        if not bool(row["usage_available"]):
            result["unavailable_calls"] += 1
            continue
        result["available_calls"] += 1
        result["estimated"] = result["estimated"] or bool(row["estimated"])
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            result[name] += int(row[name] or 0)
    result["total_tokens"] = sum(
        int(result[name])
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    result["prompt_input_tokens"] = sum(
        int(result[name])
        for name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    result["cache_hit_ratio"] = (
        int(result["cache_read_input_tokens"]) / int(result["prompt_input_tokens"])
        if result["prompt_input_tokens"]
        else 0.0
    )
    return result


def _backup_legacy_database(source: Path, destination: Path) -> None:
    """Copy a legacy SQLite database safely without altering the source files."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.migrating")
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro",
            uri=True,
        )
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None
        if destination.exists():
            temporary.unlink()
            return
        temporary.replace(destination)
    except Exception as exc:
        raise RuntimeError(f"Unable to migrate legacy CodeAgent database: {exc}") from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        if temporary.exists():
            temporary.unlink()


def _usage_groups(rows: Sequence[sqlite3.Row], column: str) -> JsonObject:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row[column]), []).append(row)
    return {name: _usage_totals(items) for name, items in sorted(groups.items())}


def _duration_ms(started_at: str, completed_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return max(0, int((end - start).total_seconds() * 1000))
    except ValueError:
        return None


def _timestamp_has_passed(value: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp <= datetime.now(UTC)
    except ValueError:
        return True


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "APPROVAL_DECISIONS",
    "InvalidStateTransitionError",
    "RecordNotFoundError",
    "Repository",
    "RUN_STATUSES",
    "SQLiteEventSink",
    "SQLiteRepository",
    "StorageConflictError",
    "StorageError",
    "TERMINAL_RUN_STATUSES",
]
