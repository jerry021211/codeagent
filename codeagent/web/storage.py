"""Thread-safe SQLite persistence for the local CodeAgent web runtime.

The repository owns all SQL and JSON serialization.  Callers work with typed
records and framework-neutral methods, which keeps the scheduler and HTTP API
independent from SQLite and makes a future persistence adapter straightforward.
"""

from __future__ import annotations

import json
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
from codeagent.web.models import (
    ApprovalRecord,
    CheckpointRecord,
    ConversationRecord,
    JsonObject,
    MessageRecord,
    ModelCallRecord,
    RunRecord,
)


ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
RUN_STATUSES = ACTIVE_RUN_STATUSES | TERMINAL_RUN_STATUSES
APPROVAL_DECISIONS = frozenset({"allow", "deny"})
APPROVAL_STATUSES = frozenset({"pending", "allowed", "denied", "expired"})
MODEL_CALL_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})

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

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        recover_incomplete: bool = True,
    ) -> "SQLiteRepository":
        return cls(
            Path(workspace) / ".codeagent" / "state.db",
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
            archived_at TEXT
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
            self._connection.execute("PRAGMA user_version = 2")

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
        return max(0, cursor.rowcount)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
        return cursor.rowcount > 0

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
                raise StorageConflictError(
                    "Event conversation_id does not match its run"
                )
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
        with self._event_condition:
            self._event_condition.notify_all()
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


def _row_to_conversation(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        id=str(row["id"]),
        title=str(row["title"]),
        workspace=str(row["workspace"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
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
    return result


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
