"""Framework-neutral observability models for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    """Return a stable UTC timestamp suitable for persistence and transport."""

    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Identifiers carried by every event emitted during one agent run."""

    conversation_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    agent_id: str = "agent_root"
    parent_agent_id: str | None = None

    def child(self, *, agent_id: str) -> "ExecutionContext":
        return ExecutionContext(
            conversation_id=self.conversation_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            agent_id=agent_id,
            parent_agent_id=self.agent_id,
        )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalized token usage for one provider model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    provider: str = "anthropic-compatible"
    model: str = ""
    call_kind: str = "main"
    estimated: bool = False
    available: bool = True

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
            "model": self.model,
            "call_kind": self.call_kind,
            "estimated": self.estimated,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Aggregated usage without provider- or call-specific metadata."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model_calls: int = 0
    unavailable_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def delta(self, previous: "TokenTotals") -> "TokenTotals":
        return TokenTotals(
            input_tokens=max(0, self.input_tokens - previous.input_tokens),
            output_tokens=max(0, self.output_tokens - previous.output_tokens),
            cache_creation_input_tokens=max(
                0,
                self.cache_creation_input_tokens
                - previous.cache_creation_input_tokens,
            ),
            cache_read_input_tokens=max(
                0,
                self.cache_read_input_tokens - previous.cache_read_input_tokens,
            ),
            model_calls=max(0, self.model_calls - previous.model_calls),
            unavailable_calls=max(
                0, self.unavailable_calls - previous.unavailable_calls
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
            "model_calls": self.model_calls,
            "unavailable_calls": self.unavailable_calls,
            "available": self.unavailable_calls == 0 and self.model_calls > 0,
        }


class UsageTracker:
    """Thread-safe aggregate shared by parent, child, and side-query clients."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._totals = TokenTotals()

    def record(self, usage: TokenUsage | None) -> None:
        if usage is None:
            return
        with self._lock:
            current = self._totals
            if not usage.available:
                self._totals = TokenTotals(
                    input_tokens=current.input_tokens,
                    output_tokens=current.output_tokens,
                    cache_creation_input_tokens=current.cache_creation_input_tokens,
                    cache_read_input_tokens=current.cache_read_input_tokens,
                    model_calls=current.model_calls + 1,
                    unavailable_calls=current.unavailable_calls + 1,
                )
                return
            self._totals = TokenTotals(
                input_tokens=current.input_tokens + usage.input_tokens,
                output_tokens=current.output_tokens + usage.output_tokens,
                cache_creation_input_tokens=(
                    current.cache_creation_input_tokens
                    + usage.cache_creation_input_tokens
                ),
                cache_read_input_tokens=(
                    current.cache_read_input_tokens + usage.cache_read_input_tokens
                ),
                model_calls=current.model_calls + 1,
                unavailable_calls=current.unavailable_calls,
            )

    def snapshot(self) -> TokenTotals:
        with self._lock:
            return self._totals


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One immutable event in an append-only execution stream."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    seq: int = 0
    occurred_at: str = field(default_factory=utc_now_iso)
    conversation_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    agent_id: str = "agent_root"
    parent_agent_id: str | None = None
    iteration: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "iteration": self.iteration,
            "payload": self.payload,
        }
