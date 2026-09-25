from __future__ import annotations

import unittest

from codeagent import ModelResponse
from codeagent.recovery import (
    RecoveryConfig,
    RecoveryReason,
    RecoveryRuntime,
    classify_exception,
)


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RecoveryClassifierTests(unittest.TestCase):
    def test_classifier_covers_common_provider_errors(self) -> None:
        self.assertEqual(
            classify_exception(ApiError("rate limit", 429)),
            RecoveryReason.RATE_LIMIT_RETRY,
        )
        self.assertEqual(
            classify_exception(ApiError("overloaded", 529)),
            RecoveryReason.OVERLOADED_RETRY,
        )
        self.assertEqual(
            classify_exception(ApiError("prompt too long", 413)),
            RecoveryReason.REACTIVE_COMPACT_RETRY,
        )
        self.assertEqual(
            classify_exception(ApiError("invalid api key", 401)),
            RecoveryReason.NON_RETRYABLE_ERROR,
        )
        self.assertEqual(
            classify_exception(TimeoutError("timed out")),
            RecoveryReason.TRANSIENT_RETRY,
        )
        self.assertEqual(
            classify_exception(ValueError("Streaming is required for long requests")),
            RecoveryReason.NON_RETRYABLE_ERROR,
        )


class RecoveryRuntimeTests(unittest.TestCase):
    def test_rate_limit_retries_and_returns_response(self) -> None:
        runtime = RecoveryRuntime(
            RecoveryConfig(max_retries=2, sleep_enabled=False),
        )
        state = runtime.create_state(model="fake-model", max_tokens=8000)
        calls = []

        def call(model, max_tokens, messages):
            calls.append(model)
            if len(calls) == 1:
                raise ApiError("rate limit", 429)
            return ModelResponse("end_turn", [{"type": "text", "text": "done"}])

        result = runtime.call_model(call, state=state, messages=[])

        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(state.retry_count, 1)

    def test_overloaded_switches_to_fallback_model(self) -> None:
        runtime = RecoveryRuntime(
            RecoveryConfig(
                max_retries=5,
                overload_fallback_after=2,
                fallback_model="fallback-model",
                sleep_enabled=False,
            )
        )
        state = runtime.create_state(model="primary-model", max_tokens=8000)
        models = []

        def call(model, max_tokens, messages):
            models.append(model)
            if len(models) <= 2:
                raise ApiError("overloaded", 529)
            return ModelResponse("end_turn", [{"type": "text", "text": "done"}])

        result = runtime.call_model(call, state=state, messages=[])

        self.assertTrue(result.ok)
        self.assertIn("fallback-model", models)
        self.assertTrue(state.fallback_used)

    def test_prompt_too_long_uses_compact_once(self) -> None:
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        state = runtime.create_state(model="fake-model", max_tokens=8000)
        calls = []

        def call(model, max_tokens, messages):
            calls.append(messages)
            if len(calls) == 1:
                raise ApiError("prompt too long", 413)
            return ModelResponse("end_turn", [{"type": "text", "text": "done"}])

        result = runtime.call_model(
            call,
            state=state,
            messages=[{"role": "user", "content": "huge"}],
            compact_fn=lambda messages: [{"role": "user", "content": "compact"}],
        )

        self.assertTrue(result.ok)
        self.assertTrue(state.reactive_compact_attempted)
        self.assertEqual(calls[1], [{"role": "user", "content": "compact"}])

    def test_non_retryable_error_fails_without_retry(self) -> None:
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        state = runtime.create_state(model="fake-model", max_tokens=8000)
        calls = 0

        def call(model, max_tokens, messages):
            nonlocal calls
            calls += 1
            raise ApiError("invalid api key", 401)

        result = runtime.call_model(call, state=state, messages=[])

        self.assertTrue(result.failed)
        self.assertEqual(calls, 1)
        self.assertEqual(result.reason, RecoveryReason.NON_RETRYABLE_ERROR)

    def test_max_tokens_escalates_then_continues(self) -> None:
        runtime = RecoveryRuntime(
            RecoveryConfig(
                escalated_max_tokens=64_000,
                max_continuations=1,
                sleep_enabled=False,
            )
        )
        state = runtime.create_state(model="fake-model", max_tokens=8000)
        messages = [{"role": "user", "content": "write a lot"}]
        first = ModelResponse("max_tokens", [{"type": "text", "text": "partial"}])

        result = runtime.handle_response(first, state=state, messages=messages)

        self.assertTrue(result.retry)
        self.assertEqual(state.current_max_tokens, 64_000)
        self.assertEqual(messages, [{"role": "user", "content": "write a lot"}])

        second = runtime.handle_response(first, state=state, messages=messages)

        self.assertTrue(second.retry)
        self.assertEqual(state.continuation_count, 1)
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertIn("已达到本次输出上限", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
