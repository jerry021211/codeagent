"""Versioned Agent Team message contracts for the first delivery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


MAX_MESSAGE_PAYLOAD_BYTES = 64 * 1024

_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "TASK_ASSIGNED": frozenset(
        {
            "task_revision",
            "attempt_ordinal",
            "title",
            "objective",
            "plan_required",
            "team_plan_revision",
            "attempt_base_commit",
        }
    ),
    "ATTEMPT_PLAN_SUBMITTED": frozenset(
        {"attempt_plan_revision", "summary", "scope_hash"}
    ),
    "ATTEMPT_PLAN_DECISION": frozenset(
        {"attempt_plan_revision", "decision", "reason"}
    ),
    "PROGRESS": frozenset({"stage", "summary"}),
    "QUESTION": frozenset({"question", "blocking"}),
    "ANSWER": frozenset({"answer", "scope_changed"}),
    "CANDIDATE_SUBMITTED": frozenset(
        {"candidate_id", "summary", "diff_ref", "diff_hash", "base_commit"}
    ),
    "REVIEW_DECISION": frozenset({"candidate_id", "decision", "reason"}),
    "VALIDATION_RESULT": frozenset(
        {"candidate_id", "validation_run_id", "status", "summary"}
    ),
    "SCOPE_VIOLATION": frozenset(
        {"allowed_scopes", "tool_call_id", "action_taken"}
    ),
    "CANCEL": frozenset({"reason_code", "reason", "effective_scope"}),
    "SHUTDOWN": frozenset({"reason_code", "reason", "effective_scope"}),
    "SYSTEM_ERROR": frozenset({"reason_code", "reason", "effective_scope"}),
    "ATTEMPT_RESUMED": frozenset(
        {"reason_code", "reason", "do_not_replay", "runtime_checks"}
    ),
    "USER_INSTRUCTION": frozenset({"content", "run_id"}),
    "ANALYSIS_RESULT": frozenset({"summary"}),
}

_ALLOWED_ROUTES: dict[str, dict[str, frozenset[str]]] = {
    "runtime": {
        "lead": frozenset(
            {
                "PROGRESS",
                "QUESTION",
                "CANDIDATE_SUBMITTED",
                "SCOPE_VIOLATION",
                "VALIDATION_RESULT",
                "CANCEL",
                "SHUTDOWN",
                "SYSTEM_ERROR",
                "USER_INSTRUCTION",
            }
        ),
        "teammate": frozenset(
            {
                "TASK_ASSIGNED",
                "ATTEMPT_PLAN_DECISION",
                "REVIEW_DECISION",
                "VALIDATION_RESULT",
                "SCOPE_VIOLATION",
                "CANCEL",
                "SHUTDOWN",
                "SYSTEM_ERROR",
                "ATTEMPT_RESUMED",
            }
        ),
    },
    "teammate": {
        "lead": frozenset(
            {
                "ATTEMPT_PLAN_SUBMITTED",
                "PROGRESS",
                "QUESTION",
                "CANDIDATE_SUBMITTED",
                "ANALYSIS_RESULT",
            }
        )
    },
    "lead": {"teammate": frozenset({"ANSWER"})},
}


def validate_team_message(
    *,
    sender_type: str,
    recipient_type: str,
    message_type: str,
    payload_version: int,
    payload: Mapping[str, Any],
) -> None:
    """Validate routing and the small versioned payload envelope."""

    sender = str(sender_type).strip().lower()
    recipient = str(recipient_type).strip().lower()
    kind = str(message_type).strip().upper()
    if payload_version != 1:
        raise ValueError(f"Unsupported message payload version: {payload_version}")
    allowed = _ALLOWED_ROUTES.get(sender, {}).get(recipient, frozenset())
    if kind not in allowed:
        raise ValueError(
            f"Message route is not allowed in phase 1: {sender}->{recipient}:{kind}"
        )
    required = _REQUIRED_FIELDS.get(kind)
    if required is None:
        raise ValueError(f"Unknown Agent Team message type: {kind}")
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(
            f"Message {kind} is missing required fields: {', '.join(missing)}"
        )
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_PAYLOAD_BYTES:
        raise ValueError("Agent Team message payload exceeds 64 KiB")


__all__ = ["MAX_MESSAGE_PAYLOAD_BYTES", "validate_team_message"]
