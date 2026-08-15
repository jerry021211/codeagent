"""Recovery policy: reason + state -> decision."""

from __future__ import annotations

from codeagent.recovery.backoff import retry_delay_seconds
from codeagent.recovery.models import (
    RecoveryAction,
    RecoveryConfig,
    RecoveryDecision,
    RecoveryReason,
    RecoveryState,
)


class RecoveryPolicy:
    """Choose recovery actions while preventing infinite loops."""

    def __init__(self, config: RecoveryConfig) -> None:
        self.config = config

    def decide_exception(
        self,
        reason: RecoveryReason,
        state: RecoveryState,
        *,
        retry_after: float | None = None,
        side_query: bool = False,
    ) -> RecoveryDecision:
        if not self.config.enabled:
            return RecoveryDecision(reason, RecoveryAction.FAIL, message="disabled")

        max_retries = (
            self.config.side_query_max_retries if side_query else self.config.max_retries
        )
        if reason == RecoveryReason.NON_RETRYABLE_ERROR:
            return RecoveryDecision(reason, RecoveryAction.FAIL, message="non-retryable")
        if reason == RecoveryReason.REACTIVE_COMPACT_RETRY:
            if side_query or state.reactive_compact_attempted:
                return RecoveryDecision(reason, RecoveryAction.FAIL, message="compact exhausted")
            return RecoveryDecision(
                reason,
                RecoveryAction.COMPACT_RETRY,
                retryable=True,
                message="reactive compact",
            )

        if reason == RecoveryReason.OVERLOADED_RETRY:
            if self._should_switch_model(state, side_query=side_query):
                return RecoveryDecision(
                    RecoveryReason.FALLBACK_MODEL,
                    RecoveryAction.SWITCH_MODEL,
                    retryable=True,
                    message="switch fallback model",
                )
            return self._backoff(reason, state, max_retries, retry_after)

        if reason in {
            RecoveryReason.RATE_LIMIT_RETRY,
            RecoveryReason.TRANSIENT_RETRY,
            RecoveryReason.UNKNOWN_RETRY,
        }:
            return self._backoff(reason, state, max_retries, retry_after)

        return RecoveryDecision(reason, RecoveryAction.FAIL, message="unsupported")

    def decide_response(
        self,
        reason: RecoveryReason,
        state: RecoveryState,
    ) -> RecoveryDecision:
        if reason != RecoveryReason.MAX_OUTPUT_TOKENS_ESCALATE:
            return RecoveryDecision(reason, RecoveryAction.CONTINUE)

        if (
            not state.max_tokens_escalated
            and state.current_max_tokens < self.config.escalated_max_tokens
        ):
            return RecoveryDecision(
                RecoveryReason.MAX_OUTPUT_TOKENS_ESCALATE,
                RecoveryAction.ESCALATE_TOKENS,
                retryable=True,
                message="increase max_tokens",
            )
        if state.continuation_count < self.config.max_continuations:
            return RecoveryDecision(
                RecoveryReason.MAX_OUTPUT_TOKENS_RECOVERY,
                RecoveryAction.CONTINUATION,
                retryable=True,
                message="continue truncated output",
            )
        return RecoveryDecision(
            RecoveryReason.MAX_OUTPUT_TOKENS_RECOVERY,
            RecoveryAction.CONTINUE,
            message="continuation exhausted",
        )

    def _backoff(
        self,
        reason: RecoveryReason,
        state: RecoveryState,
        max_retries: int,
        retry_after: float | None,
    ) -> RecoveryDecision:
        if state.retry_count >= max_retries:
            return RecoveryDecision(reason, RecoveryAction.FAIL, message="retries exhausted")
        delay = retry_delay_seconds(state.retry_count + 1, self.config, retry_after=retry_after)
        return RecoveryDecision(
            reason,
            RecoveryAction.BACKOFF_RETRY,
            retryable=True,
            delay_seconds=delay,
            message="backoff retry",
        )

    def _should_switch_model(self, state: RecoveryState, *, side_query: bool) -> bool:
        return (
            not side_query
            and bool(self.config.fallback_model)
            and not state.fallback_used
            and state.consecutive_overloaded >= self.config.overload_fallback_after
        )
