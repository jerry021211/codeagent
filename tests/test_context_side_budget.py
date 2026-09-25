from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from codeagent import ModelResponse
from codeagent.context.budget import BoundModelClient, RequestBudgetError
from codeagent.memory import MemoryConfig, MemoryManager, MemoryStore
from codeagent.recovery import RecoveryConfig, RecoveryReason, RecoveryRuntime
from codeagent.runtime.cancellation import CancelledError


class RecordingClient:
    def __init__(self, *, calls=None, kind="main", response=None):
        self.calls = [] if calls is None else calls
        self.call_kind = kind
        self.response = response or ModelResponse("end_turn", [{"type": "text", "text": "[]"}])

    def fork(self, **kwargs):
        return RecordingClient(calls=self.calls, kind=kwargs.get("call_kind", self.call_kind), response=self.response)

    def create_message(self, **kwargs):
        self.calls.append((self.call_kind, deepcopy(kwargs)))
        return self.response


class ContextSideBudgetTests(unittest.TestCase):
    def setUp(self):
        self.request = dict(model="primary", system="system", messages=[{"role": "user", "content": "hello"}], tools=[], max_tokens=100)

    def test_side_client_passes_response_usage_and_original_request_unchanged(self):
        original = deepcopy(self.request)
        client = RecordingClient()
        bounded = BoundModelClient(client, max_request_chars=1000)
        response = bounded.create_message(**self.request)
        self.assertIs(response, client.response)
        self.assertEqual(client.calls[0][1], original)
        self.assertEqual(self.request, original)
        self.assertEqual(bounded.call_kind, "main")

    def test_side_client_rejects_complete_request_without_provider_call(self):
        client = RecordingClient()
        bounded = BoundModelClient(client, max_request_chars=500)
        with self.assertRaises(RequestBudgetError):
            bounded.create_message(**{**self.request, "system": "x" * 1000})
        self.assertEqual(client.calls, [])

    def test_nested_forks_keep_budget_and_actual_call_kind(self):
        client = RecordingClient()
        bounded = BoundModelClient(client, max_request_chars=500)
        child = bounded.fork(call_kind="memory_maintenance").fork(call_kind="memory_extract")
        self.assertEqual(child.call_kind, "memory_extract")
        child.create_message(**self.request)
        with self.assertRaises(RequestBudgetError):
            child.create_message(**{**self.request, "messages": [{"role": "user", "content": "x" * 1000}]})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "memory_extract")

    def test_client_without_fork_still_preserves_nested_budget(self):
        class ClientWithoutFork:
            def create_message(self, **kwargs):
                raise AssertionError("Oversized request must never arrive")
        child = BoundModelClient(ClientWithoutFork(), max_request_chars=50).fork(call_kind="memory_select")
        with self.assertRaises(RequestBudgetError):
            child.create_message(**self.request)

    def test_resolver_checks_actual_fallback_model_window(self):
        client = RecordingClient()
        resolved = []
        def window(model):
            resolved.append(model)
            return 1000 if model == "primary" else 100
        bounded = BoundModelClient(client, max_request_chars=1000, window_resolver=window)
        bounded.create_message(**self.request)
        with self.assertRaises(RequestBudgetError):
            bounded.create_message(**{**self.request, "model": "small-fallback"})
        self.assertEqual(resolved, ["primary", "small-fallback"])
        self.assertEqual(len(client.calls), 1)

    def test_memory_selection_recovery_treats_local_budget_as_nonretryable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp))
            store.remember(name="fact", description="d" * 10000, content="body")
            runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
            memory = MemoryManager(store, MemoryConfig(), recovery_runtime=runtime)
            client = RecordingClient()
            bounded = BoundModelClient(client, max_request_chars=5000)
            result = memory.select_context(self.request["messages"], client=bounded, model="primary", max_tokens=100)
            self.assertEqual(result, "")
            self.assertEqual(client.calls, [])

    def test_memory_extraction_fork_cannot_bypass_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = MemoryManager(MemoryStore(Path(temp)), MemoryConfig(auto_extract=True))
            client = RecordingClient()
            with self.assertRaises(RequestBudgetError):
                memory.after_turn(
                    [{"role": "user", "content": "x" * 10000}],
                    client=BoundModelClient(client, max_request_chars=1000), model="primary", max_tokens=100,
                )
            self.assertEqual(client.calls, [])
            self.assertEqual(memory.store.list_memories(), [])

    def test_memory_consolidation_fork_cannot_bypass_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp))
            store.remember(name="fact", description="known fact", content="x" * 4000)
            memory = MemoryManager(store, MemoryConfig(consolidate_threshold=1, consolidate_mode="model"))
            client = RecordingClient()
            with self.assertRaises(RequestBudgetError):
                memory.after_turn([], client=BoundModelClient(client, max_request_chars=1000), model="primary", max_tokens=100)
            self.assertEqual(client.calls, [])
            self.assertEqual(store.list_memories()[0].content, "x" * 4000)
            self.assertFalse((store.root / ".consolidate-lock").exists())

    def test_final_local_budget_failure_does_not_restart_compaction(self):
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        state = runtime.create_state(model="primary", max_tokens=100)
        compact_calls = []
        client = RecordingClient()
        bounded = BoundModelClient(client, max_request_chars=50)
        result = runtime.call_model(
            lambda model, max_tokens, messages: bounded.create_message(**{**self.request, "model": model, "max_tokens": max_tokens, "messages": messages}),
            state=state, messages=self.request["messages"], compact_fn=lambda messages: compact_calls.append(messages),
        )
        self.assertTrue(result.failed)
        self.assertEqual(result.reason, RecoveryReason.NON_RETRYABLE_ERROR)
        self.assertIn("serialized request characters", result.error)
        self.assertEqual(state.retry_count, 0)
        self.assertEqual(compact_calls, [])
        self.assertEqual(client.calls, [])

    def test_real_provider_overflow_still_compacts_once(self):
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        state = runtime.create_state(model="primary", max_tokens=100)
        calls = []
        compact_calls = []
        def call(model, max_tokens, messages):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                raise RuntimeError("provider prompt too long")
            return ModelResponse("end_turn", [{"type": "text", "text": "done"}])
        def compact(messages):
            compact_calls.append(messages)
            return [{"role": "user", "content": "smaller"}]
        result = runtime.call_model(call, state=state, messages=self.request["messages"], compact_fn=compact)
        self.assertTrue(result.ok)
        self.assertEqual(len(compact_calls), 1)
        self.assertEqual(calls[-1], [{"role": "user", "content": "smaller"}])

    def test_unavailable_or_failed_compaction_keeps_provider_failure_detail(self):
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        def overflow(*args):
            raise RuntimeError("provider maximum context: requested 90000, limit 32000")
        def failed_summary(messages):
            raise RuntimeError("summary service unavailable")
        for compact in (None, lambda messages: None, failed_summary):
            with self.subTest(compact=compact):
                result = runtime.call_model(overflow, state=runtime.create_state(model="primary", max_tokens=100), messages=[], compact_fn=compact)
                self.assertTrue(result.failed)
                self.assertIn("requested 90000, limit 32000", result.error)
                if compact is failed_summary:
                    self.assertIn("summary service unavailable", result.error)

    def test_compaction_cancellation_propagates(self):
        runtime = RecoveryRuntime(RecoveryConfig(sleep_enabled=False))
        def overflow(*args):
            raise RuntimeError("prompt too long")
        def compact(messages):
            raise CancelledError("cancelled during compaction")
        with self.assertRaises(CancelledError):
            runtime.call_model(overflow, state=runtime.create_state(model="primary", max_tokens=100), messages=[], compact_fn=compact)


if __name__ == "__main__":
    unittest.main()
