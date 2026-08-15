from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codeagent.events import (
    CallbackEventSink,
    EventEmitter,
    ExecutionContext,
    TokenUsage,
    UsageTracker,
    redact_payload,
)


class EventTests(unittest.TestCase):
    def test_emitter_orders_events_and_propagates_child_identity(self) -> None:
        events = []
        emitter = EventEmitter(
            CallbackEventSink(events.append),
            context=ExecutionContext(
                conversation_id="conv_1",
                run_id="run_1",
                turn_id="turn_1",
            ),
        )

        emitter.emit("run.started")
        emitter.child(agent_id="agent_child").emit("tool.started")

        self.assertEqual([event.seq for event in events], [1, 2])
        self.assertEqual(events[1].agent_id, "agent_child")
        self.assertEqual(events[1].parent_agent_id, "agent_root")

    def test_event_sink_failures_do_not_break_execution(self) -> None:
        emitter = EventEmitter(
            CallbackEventSink(lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
        )
        event = emitter.emit("run.started", {"ok": True})
        self.assertEqual(event.type, "run.started")

    def test_usage_tracker_is_additive(self) -> None:
        tracker = UsageTracker()
        tracker.record(TokenUsage(input_tokens=10, output_tokens=4))
        tracker.record(
            TokenUsage(
                input_tokens=5,
                output_tokens=2,
                cache_read_input_tokens=3,
            )
        )

        totals = tracker.snapshot()
        self.assertEqual(totals.input_tokens, 15)
        self.assertEqual(totals.output_tokens, 6)
        self.assertEqual(totals.cache_read_input_tokens, 3)
        self.assertEqual(totals.total_tokens, 24)
        self.assertEqual(totals.model_calls, 2)

    def test_usage_tracker_marks_missing_provider_usage(self) -> None:
        tracker = UsageTracker()
        tracker.record(TokenUsage(available=False, model="unknown"))

        totals = tracker.snapshot()
        self.assertEqual(totals.model_calls, 1)
        self.assertEqual(totals.unavailable_calls, 1)
        self.assertFalse(totals.to_dict()["available"])

    def test_payload_redacts_keys_environment_values_and_common_tokens(self) -> None:
        with patch.dict(os.environ, {"TEST_API_KEY": "very-secret-value"}):
            payload = redact_payload(
                {
                    "api_key": "plain",
                    "nested": {
                        "text": "Bearer abc.def.ghi very-secret-value sk-example123456789"
                    },
                }
            )

        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertNotIn("very-secret-value", payload["nested"]["text"])
        self.assertNotIn("abc.def.ghi", payload["nested"]["text"])
        self.assertNotIn("sk-example", payload["nested"]["text"])


if __name__ == "__main__":
    unittest.main()
