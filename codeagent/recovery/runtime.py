"""Runtime recovery engine for model calls."""

from __future__ import annotations

import time
import json
from collections import Counter
from copy import deepcopy
from contextlib import nullcontext

from codeagent.events import EventEmitter
from codeagent.messages import Message, _field
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
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import CancelledError

CONTINUATION_PROMPT = (
    "[运行时提醒：输出长度] 已达到本次输出上限。直接续写未完成内容，不重复已完成部分。"
    "必要时将剩余工作拆成较小部分；不要重新执行已产生副作用的操作。"
)

TRUNCATED_TOOL_RESULT = (
    "未执行：模型响应因 max_tokens 被截断，工具参数可能不完整。"
    "Runtime 未调用此工具，未产生本次调用的副作用；如仍需操作，请重新提交完整工具请求。"
)


class RecoveryRuntime:
    """Execute model calls with recovery decisions."""

    def __init__(
        self,
        config: RecoveryConfig | None = None,
        *,
        log: RecoveryLog | None = None,
        activity: ExecutionActivity | None = None,
    ) -> None:
        self.config = config or RecoveryConfig()
        self.policy = RecoveryPolicy(self.config)
        self.log = log
        self.activity = activity

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
        activity = self.activity
        if activity is not None and state.model_deadline is None:
            state.model_deadline = activity.clock() + activity.model_timeout
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                with (
                    activity.operation("model", state.model_deadline)
                    if activity is not None else nullcontext()
                ):
                    response = call(
                        state.current_model,
                        state.current_max_tokens,
                        current_messages,
                    )
                state.consecutive_overloaded = 0
                return RecoveryCallResult(response=response, messages=current_messages)
            except CancelledError:
                raise
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
                        return self._failed(reason, f"{state.last_error}\nNo compact function is available.")
                    try:
                        compacted = compact_fn(current_messages)
                    except CancelledError:
                        raise
                    except Exception as compact_error:
                        return self._failed(reason, (
                            f"{state.last_error}\nContext compaction failed: "
                            f"{type(compact_error).__name__}: {compact_error}"
                        ))
                    if compacted is None:
                        return self._failed(reason, f"{state.last_error}\nReactive compaction produced no smaller context.")
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
                    with (
                        activity.operation("retry", state.model_deadline)
                        if activity is not None else nullcontext()
                    ):
                        delay = decision.delay_seconds
                        if activity is not None:
                            delay = min(delay, max(0, state.model_deadline - activity.clock()))
                        self._sleep(delay, cancellation=cancellation)
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

        blocks = response.content if isinstance(response.content, list) else [response.content]
        if reason == RecoveryReason.MAX_OUTPUT_TOKENS_ESCALATE and any(
            _field(block, "type") == "tool_use" for block in blocks
        ):
            # Even syntactically valid earlier blocks belong to an unfinished
            # response. Never dispatch them, including after retry exhaustion.
            _record_unexecuted_truncated_tools(messages, blocks)
            if decision.action == RecoveryAction.ESCALATE_TOKENS:
                state.current_max_tokens = self.config.escalated_max_tokens
                state.max_tokens_escalated = True
            elif decision.action == RecoveryAction.CONTINUATION:
                state.continuation_count += 1
            else:
                return RecoveryResponseResult(
                    failed=True, messages=messages, reason=decision.reason,
                    error="模型输出持续达到长度上限，工具请求均未执行；输出升级和续写次数已用尽。",
                )
            messages.append({"role": "user", "content": CONTINUATION_PROMPT, "_context_source": "runtime"})
            return RecoveryResponseResult(retry=True, messages=messages, reason=decision.reason)

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
            messages.append({"role": "user", "content": CONTINUATION_PROMPT, "_context_source": "runtime"})
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
        state.model_deadline = None
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
            if self.activity is not None and self.activity.execution_budget is not None:
                seconds = min(seconds, self.activity.execution_budget.remaining_seconds())
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


def _record_unexecuted_truncated_tools(messages: list[Message], blocks: list) -> None:
    """Record received evidence with complete protocol pairs and no dispatch.

    Malformed IDs/arguments cannot be replayed as provider tool blocks. Keep
    those received values as explicitly inert data while preserving normal text.
    """
    ids = Counter(
        _field(block, "id") for block in blocks
        if _field(block, "type") == "tool_use" and isinstance(_field(block, "id"), str)
    )
    content = []
    results = []
    for block in blocks:
        if _field(block, "type") != "tool_use":
            content.append(deepcopy(block))
            continue
        identifier = _field(block, "id")
        name = _field(block, "name")
        arguments = _field(block, "input")
        if (isinstance(identifier, str) and identifier and ids[identifier] == 1
                and isinstance(name, str) and name and isinstance(arguments, dict)):
            content.append(deepcopy(block))
            results.append({
                "type": "tool_result", "tool_use_id": identifier,
                "is_error": True, "content": TRUNCATED_TOOL_RESULT,
            })
        else:
            dump = getattr(block, "model_dump", None)
            received = dump(mode="json") if callable(dump) else block
            content.append({"type": "text", "text": (
                "[截断响应中的无效工具请求，仅保存为历史数据；未执行，不是新的工具调用]\n"
                + json.dumps(received, ensure_ascii=False, default=str)
            )})
    additions = [{"role": "assistant", "content": content}]
    if results:
        additions.append({"role": "user", "content": results})
    messages.extend(additions)
