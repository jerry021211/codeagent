"""Durable SQLite row history used by the read-only Team observer.

Triggers capture every column, including writes outside repository helpers. They
run in the writer's transaction: rolled-back changes never become history.
This is diagnostic history, not another task/event delivery state machine.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from codeagent.events import redact_payload


# Explicit coverage: do not silently start copying unrelated new tables.
TABLE_SCOPES = {
    "team_runs": ("team", "id"),
    **{name: ("team", "team_run_id") for name in (
        "team_plan_revisions", "team_agents", "agent_sessions", "task_attempts",
        "resource_leases", "team_messages", "team_commands", "team_base_confirmations",
        "worktree_bindings", "tool_executions", "attempt_plans", "candidates",
        "validation_runs", "manual_integration_checks",
    )},
    "agent_session_checkpoints": ("session", "session_id"),
    "team_message_consumptions": ("message", "message_id"),
    "task_lists": ("task_list", "id"),
    "tasks": ("task_list", "task_list_id"),
    "task_dependencies": ("task_list", "task_list_id"),
    "task_activity": ("task_list", "task_list_id"),
    "conversations": ("conversation", "id"),
    "runs": ("run", "id"),
    "user_questions": ("run", "run_id"),
    "messages": ("conversation", "conversation_id"),
    **{name: ("run", "run_id") for name in (
        "events", "approvals", "model_calls", "checkpoints",
    )},
}


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_json(columns: list[str], prefix: str) -> str:
    return "json_object(" + ",".join(
        f"'{name}',{prefix}.{_quoted(name)}" for name in columns
    ) + ")"


def install_observation(connection: sqlite3.Connection) -> None:
    """Install once per schema; initial snapshots are not invented transitions."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS database_changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            table_name TEXT NOT NULL,
            operation TEXT NOT NULL,
            record_key TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_id TEXT,
            before_json TEXT,
            after_json TEXT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS database_changes_scope
        ON database_changes(scope_kind, scope_id, seq)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS database_changes_table
        ON database_changes(table_name, seq)
    """)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table, (kind, scope) in TABLE_SCOPES.items():
            info = connection.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
            columns = [row["name"] for row in info]
            keys = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
            installed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"observe_{table}_insert",),
            ).fetchone()
            if not installed:
                connection.execute(f"""
                    INSERT INTO database_changes
                        (occurred_at,table_name,operation,record_key,scope_kind,scope_id,after_json)
                    SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, 'snapshot',
                        {_row_json(keys, 'r')}, ?, r.{_quoted(scope)}, {_row_json(columns, 'r')}
                    FROM {_quoted(table)} r
                """, (table, kind))
            for operation in ("insert", "update", "delete"):
                row = "OLD" if operation == "delete" else "NEW"
                old = "NULL" if operation == "insert" else _row_json(columns, "OLD")
                new = "NULL" if operation == "delete" else _row_json(columns, "NEW")
                # BEFORE DELETE also records cascades without depending on a parent lookup.
                timing = "BEFORE" if operation == "delete" else "AFTER"
                when = ""
                if operation == "update":
                    when = "WHEN " + " OR ".join(
                        f"OLD.{_quoted(name)} IS NOT NEW.{_quoted(name)}" for name in columns
                    )
                connection.execute(f"DROP TRIGGER IF EXISTS observe_{table}_{operation}")
                connection.execute(f"""
                    CREATE TRIGGER observe_{table}_{operation}
                    {timing} {operation.upper()} ON {_quoted(table)} {when}
                    BEGIN
                        INSERT INTO database_changes
                            (occurred_at,table_name,operation,record_key,scope_kind,scope_id,before_json,after_json)
                        VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'), '{table}', '{operation}',
                            {_row_json(keys, row)}, '{kind}', {row}.{_quoted(scope)}, {old}, {new});
                    END
                """)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _scope_query(team: Any) -> tuple[str, dict[str, Any]]:
    # Follow historical identities, not just live rows, so deleted sessions and
    # consumed/deleted messages remain visible. Conversation runs are shared
    # context: include them from creation, not only when later tagged with a Team
    # ID (late association would skip older rows behind the polling cursor).
    predicate = """
        (scope_kind='team' AND scope_id=:team)
        OR (scope_kind='task_list' AND scope_id=:tasks)
        OR (scope_kind='conversation' AND scope_id=:conversation)
        OR (scope_kind='session' AND scope_id IN (
            SELECT json_extract(record_key,'$.id') FROM database_changes
            WHERE table_name='agent_sessions' AND scope_kind='team' AND scope_id=:team
        ))
        OR (scope_kind='message' AND scope_id IN (
            SELECT json_extract(record_key,'$.id') FROM database_changes
            WHERE table_name='team_messages' AND scope_kind='team' AND scope_id=:team
        ))
        OR (scope_kind='run' AND (scope_id=:run OR scope_id IN (
            SELECT json_extract(record_key,'$.id') FROM database_changes
            WHERE table_name='runs' AND
                json_extract(COALESCE(after_json,before_json),'$.conversation_id')=:conversation
        )))
    """
    return predicate, {"team": team.id, "tasks": team.task_list_id,
                       "conversation": team.conversation_id, "run": team.root_run_id}


def _decode(row: sqlite3.Row, *, detail: bool) -> dict[str, Any]:
    before = json.loads(row["before_json"]) if row["before_json"] else None
    after = json.loads(row["after_json"]) if row["after_json"] else None
    old, new = before or {}, after or {}
    fields = [key for key in dict.fromkeys([*old, *new])
              if key not in old or key not in new or old[key] != new[key]]
    current = new or old
    result = {key: row[key] for key in ("seq", "occurred_at", "table_name", "operation")}
    result.update(record_key=json.loads(row["record_key"]), changed_fields=fields)
    result["identity"] = {key: current[key] for key in (
        "task_id", "agent_id", "session_id", "attempt_id", "tool_call_id",
        "sender_agent_id", "recipient_agent_id", "generation", "type", "tool_name",
    ) if current.get(key) is not None}
    own_identity = {"tasks": "task_id", "team_agents": "agent_id",
                    "agent_sessions": "session_id", "task_attempts": "attempt_id"}
    if row["table_name"] in own_identity:
        result["identity"][own_identity[row["table_name"]]] = current["id"]
    result["transitions"] = {
        key: {"before": old.get(key), "after": new.get(key)}
        for key in ("state", "status", "write_enabled", "result_unknown", "active_plan_revision",
                    "waiting_reason", "delivered_at", "acked_at", "commit_hash", "exit_code")
        if key in fields and old.get(key) != new.get(key)
    }
    if detail:
        # The database retains exact values. JSON columns are decoded only for
        # display/redaction; bounded previews are explicitly marked by redact_payload.
        def display(data: dict[str, Any] | None) -> Any:
            if data is None:
                return None
            output = {}
            for key, value in data.items():
                if key.endswith("_json") and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                output[key] = _display_value(key, value)
            return output
        result.update(before=display(before), after=display(after))
    return result


def _display_value(key: str, value: Any, depth: int = 0) -> Any:
    """Reuse secret masking without silently dropping arrays after item 200."""
    if depth > 64:
        return {"truncated": True, "reason": "预览嵌套超过 64 层；完整值保留在数据库"}
    if redact_payload({key: None}).get(key) == "[REDACTED]":
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: _display_value(name, item, depth + 1) for name, item in value.items()}
    if isinstance(value, list):
        return [_display_value("item", item, depth + 1) for item in value]
    sanitized = redact_payload({key: value}, max_bytes=128 * 1024)
    return sanitized.get(key, sanitized)


def read_changes(connection: sqlite3.Connection, team: Any, *, after: int = 0,
                 limit: int = 100, table: str | None = None,
                 seq: int | None = None) -> dict[str, Any]:
    predicate, params = _scope_query(team)
    params.update(after=after, limit=limit + 1)
    where = f"({predicate})"
    if table:
        where += " AND table_name=:table"
        params["table"] = table
    if seq is not None:
        where += " AND seq=:seq"
        params["seq"] = seq
    rows = connection.execute(
        f"SELECT * FROM database_changes WHERE {where} AND seq>:after ORDER BY seq LIMIT :limit",
        params,
    ).fetchall()
    items = [_decode(row, detail=seq is not None) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["seq"] if items else after,
            "has_more": len(rows) > limit, "tables": list(TABLE_SCOPES),
            "coverage": "所有列的已提交行变化；snapshot 是启用监控时的初始值，不是历史操作。共享任务列表及会话运行记录包含该列表/会话的其他活动。"}
