"""Recovery state and decision models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from codeagent.messages import Message
from codeagent.models import ModelResponse


class RecoveryReason(str, Enum):
    """Normalized reasons produced from exceptions or model responses."""

    COMPLETED = "completed"
    NEXT_TURN = "next_turn"
    RATE_LIMIT_RETRY = "rate_limit_retry"
    OVERLOADED_RETRY = "overloaded_retry"
    TRANSIENT_RETRY = "transient_retry"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    FALLBACK_MODEL = "fallback_model"
    NON_RETRYABLE_ERROR = "non_retryable_error"
    UNKNOWN_RETRY = "unknown_retry"


class RecoveryAction(str, Enum):
    """Action chosen by the recovery policy."""

    CONTINUE = "continue"
    RETRY = "retry"
    BACKOFF_RETRY = "backoff_retry"
    COMPACT_RETRY = "compact_retry"
    ESCALATE_TOKENS = "escalate_tokens"
    CONTINUATION = "continuation"
    SWITCH_MODEL = "switch_model"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    """Runtime settings for LLM error recovery."""

    enabled: bool = True
    max_retries: int = 10
    base_delay_ms: int = 500
    max_delay_ms: int = 32_000
    jitter_ratio: float = 0.25
    max_continuations: int = 3
    escalated_max_tokens: int = 64_000
    overload_fallback_after: int = 3
    fallback_model: str = ""
    side_query_max_retries: int = 2
    trace: bool = False
    sleep_enabled: bool = True


@dataclass(slots=True)
class RecoveryState:
    """Mutable state for one agent run or one side-query run."""

    current_model: str
    current_max_tokens: int
    retry_count: int = 0
    consecutive_overloaded: int = 0
    reactive_compact_attempted: bool = False
    max_tokens_escalated: bool = False
    continuation_count: int = 0
    fallback_used: bool = False
    last_reason: RecoveryReason | None = None
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Policy decision for one recovery transition."""

    reason: RecoveryReason
    action: RecoveryAction
    retryable: bool = False
    message: str = ""
    delay_seconds: float = 0.0


@dataclass(slots=True)
class RecoveryCallResult:
    """Result of a recovered model call."""

    response: ModelResponse | None = None
    messages: list[Message] | None = None
    failed: bool = False
    error: str = ""
    reason: RecoveryReason | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None and not self.failed


@dataclass(slots=True)
class RecoveryResponseResult:
    """Result after inspecting a successful model response."""

    response: ModelResponse | None = None
    messages: list[Message] | None = None
    retry: bool = False
    failed: bool = False
    error: str = ""
    reason: RecoveryReason | None = None


ModelCall = Callable[[str, int, list[Message]], ModelResponse]
CompactFn = Callable[[list[Message]], list[Message] | None]
RecoveryLog = Callable[[str], None]
