"""Persistence records used by the local CodeAgent web application.

These models deliberately contain no FastAPI or SQLite behavior.  They are the
small contract between the persistence adapter, the scheduler, and any HTTP
transport layered on top of it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


JsonObject = dict[str, Any]


class RecordMixin:
    """Provide a JSON-friendly representation for API adapters."""

    def to_dict(self) -> JsonObject:
        return asdict(self)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ConversationRecord(RecordMixin):
    id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    archived_at: str | None = None
    active_task_list_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord(RecordMixin):
    id: str
    conversation_id: str
    role: str
    content: Any
    created_at: str
    run_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunRecord(RecordMixin):
    id: str
    conversation_id: str
    status: str
    created_at: str
    updated_at: str
    queue_position: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested_at: str | None = None
    error: Any | None = None
    metadata: JsonObject = field(default_factory=dict)
    next_event_seq: int = 0


@dataclass(frozen=True, slots=True)
class ApprovalRecord(RecordMixin):
    id: str
    conversation_id: str
    run_id: str
    tool_name: str
    tool_input: JsonObject
    reason: str
    status: str
    created_at: str
    event_seq: int | None = None
    tool_call_id: str | None = None
    expires_at: str | None = None
    resolved_at: str | None = None
    decision: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelCallRecord(RecordMixin):
    id: str
    conversation_id: str
    run_id: str
    agent_id: str
    model: str
    call_kind: str
    status: str
    started_at: str
    provider: str = "anthropic-compatible"
    parent_agent_id: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    usage_available: bool = False
    estimated: bool = False
    error: Any | None = None
    metadata: JsonObject = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        if not self.usage_available:
            return None
        return sum(
            value or 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_creation_input_tokens,
                self.cache_read_input_tokens,
            )
        )

    def to_dict(self) -> JsonObject:
        # ``dataclass(slots=True)`` rebuilds the class object, which makes
        # zero-argument ``super()`` unreliable on some supported Python 3.11
        # builds.  Calling the stateless mixin explicitly avoids that trap.
        result = RecordMixin.to_dict(self)
        result["total_tokens"] = self.total_tokens
        return result


@dataclass(frozen=True, slots=True)
class CheckpointRecord(RecordMixin):
    id: str
    conversation_id: str
    run_id: str
    run_status: str
    messages: list[JsonObject]
    todos: list[JsonObject]
    context: JsonObject
    created_at: str
    metadata: JsonObject = field(default_factory=dict)


__all__ = [
    "ApprovalRecord",
    "CheckpointRecord",
    "ConversationRecord",
    "JsonObject",
    "MessageRecord",
    "ModelCallRecord",
    "RecordMixin",
    "RunRecord",
]
