"""Runtime recovery engine for model calls."""

from __future__ import annotations

import time

from codeagent.events import EventEmitter
from codeagent.messages import Message
from codeagent.models import ModelResponse
from codeagent.recovery.classifier import (
    classify_exception,
    classify_response,
    retry_after_seconds,
)
from codeagent.recovery.models import (
    CompactFn,
    ModelCall,
    RecoveryAction,
    RecoveryCallResult,
    RecoveryConfig,
    RecoveryLog,
    RecoveryReason,
    RecoveryResponseResult,
    RecoveryState,
)
from codeagent.recovery.policy import RecoveryPolicy
from codeagent.runtime import CancellationToken

CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly. No apology, no recap. "
    "Pick up mid-thought if needed. Break remaining work into smaller pieces."
)


class RecoveryRuntime:
    """Execute model calls with recovery decisions."""

    def __init__(
        self,
        config: RecoveryConfig | None = None,
        *,
        log: RecoveryLog | None = None,
    ) -> None:
        self.config = config or RecoveryConfig()
        self.policy = RecoveryPolicy(self.config)
        self.log = log

    def create_state(self, *, model: str, max_tokens: int) -> RecoveryState:
        return RecoveryState(current_model=model, current_max_tokens=max_tokens)

    def call_model(
        self,
        call: ModelCall,
        *,
        state: RecoveryState,
        messages: list[Message],
        compact_fn: CompactFn | None = None,
        side_query: bool = False,
        event_emitter: EventEmitter | None = None,
        cancellation: CancellationToken | None = None,
    ) -> RecoveryCallResult:
        current_messages = messages
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                response = call(
                    state.current_model,
                    state.current_max_tokens,
                    current_messages,
                )
                state.consecutive_overloaded = 0
                return RecoveryCallResult(response=response, messages=current_messages)
            except Exception as exc:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                reason = classify_exception(exc)
                state.last_reason = reason
                state.last_error = f"{type(exc).__name__}: {exc}"
                if reason == RecoveryReason.OVERLOADED_RETRY:
                    state.consecutive_overloaded += 1

                decision = self.policy.decide_exception(
                    reason,
                    state,
                    retry_after=retry_after_seconds(exc),
                    side_query=side_query,
                )
                self._log_decision(decision, state)
                self._emit_decision(event_emitter, decision, state, error=state.last_error)

                if decision.action == RecoveryAction.COMPACT_RETRY:
                    if compact_fn is None:
                        return self._failed(reason, "No compact function is available.")
                    compacted = compact_fn(current_messages)
                    if compacted is None:
                        return self._failed(reason, "Reactive compact failed.")
                    state.reactive_compact_attempted = True
                    current_messages = compacted
                    continue

                if decision.action == RecoveryAction.SWITCH_MODEL:
                    state.current_model = self.config.fallback_model
                    state.fallback_used = True
                    state.retry_count += 1
                    continue

                if decision.action == RecoveryAction.BACKOFF_RETRY:
                    state.retry_count += 1
                    self._sleep(decision.delay_seconds, cancellation=cancellation)
                    continue

                return self._failed(reason, state.last_error)

    def handle_response(
        self,
        response: ModelResponse,
        *,
        state: RecoveryState,
        messages: list[Message],
        event_emitter: EventEmitter | None = None,
        cancellation: CancellationToken | None = None,
    ) -> RecoveryResponseResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        reason = classify_response(response)
        decision = self.policy.decide_response(reason, state)
        state.last_reason = decision.reason
        self._log_decision(decision, state)
        if decision.action != RecoveryAction.CONTINUE:
            self._emit_decision(event_emitter, decision, state)

        if decision.action == RecoveryAction.ESCALATE_TOKENS:
            state.current_max_tokens = self.config.escalated_max_tokens
            state.max_tokens_escalated = True
            return RecoveryResponseResult(
                retry=True,
                messages=messages,
                reason=decision.reason,
            )

        if decision.action == RecoveryAction.CONTINUATION:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": CONTINUATION_PROMPT})
            state.continuation_count += 1
            return RecoveryResponseResult(
                retry=True,
                messages=messages,
                reason=decision.reason,
            )

        if decision.action == RecoveryAction.FAIL:
            return RecoveryResponseResult(
                failed=True,
                error=decision.message,
                reason=decision.reason,
            )
        return RecoveryResponseResult(response=response, messages=messages, reason=reason)

    def _failed(self, reason: RecoveryReason, error: str) -> RecoveryCallResult:
        return RecoveryCallResult(failed=True, error=error, reason=reason)

    def _sleep(
        self,
        seconds: float,
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if self.config.sleep_enabled and seconds > 0:
            if cancellation is not None:
                if cancellation.wait(seconds):
                    cancellation.raise_if_cancelled()
            else:
                time.sleep(seconds)

    def _log_decision(self, decision, state: RecoveryState) -> None:
        if not self.config.trace or self.log is None:
            return
        details = [
            f"reason={decision.reason.value}",
            f"action={decision.action.value}",
            f"retry={state.retry_count}",
            f"model={state.current_model}",
            f"max_tokens={state.current_max_tokens}",
        ]
        if decision.delay_seconds:
            details.append(f"delay={decision.delay_seconds:.2f}s")
        if state.fallback_used:
            details.append("fallback=true")
        self.log("[recovery] " + " ".join(details))

    @staticmethod
    def _emit_decision(
        event_emitter: EventEmitter | None,
        decision,
        state: RecoveryState,
        *,
        error: str = "",
    ) -> None:
        if event_emitter is None:
            return
        event_emitter.emit(
            "recovery.scheduled",
            {
                "reason": decision.reason.value,
                "action": decision.action.value,
                "retryable": decision.retryable,
                "retry_count": state.retry_count,
                "delay_seconds": decision.delay_seconds,
                "model": state.current_model,
                "max_tokens": state.current_max_tokens,
                "fallback_used": state.fallback_used,
                "error": error,
            },
        )
