from __future__ import annotations

import unittest

from codeagent import Agent, AgentConfig, ModelResponse, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager, HistoryObserver
from codeagent.events import (
    CallbackEventSink,
    EventEmitter,
    RecordingEventSink,
    RunEvent,
)


class HistoryObserverTests(unittest.TestCase):
    def test_append_only_history_stays_in_same_generation(self) -> None:
        observer = HistoryObserver()

        first = observer.observe([{"role": "user", "content": "start"}])
        second = observer.observe(
            [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "continue"},
            ]
        )

        self.assertFalse(first.rewritten)
        self.assertFalse(second.rewritten)
        self.assertEqual(second.generation, 0)
        self.assertEqual(second.common_prefix_messages, 1)
        self.assertEqual(second.appended_messages, 1)

    def test_sent_prefix_change_starts_new_generation(self) -> None:
        observer = HistoryObserver()
        observer.observe(
            [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "old"},
            ]
        )

        rewritten = observer.observe(
            [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "compacted"},
                {"role": "user", "content": "next"},
            ]
        )
        appended = observer.observe(
            [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "compacted"},
                {"role": "user", "content": "next"},
                {"role": "assistant", "content": "continue"},
            ]
        )

        self.assertTrue(rewritten.rewritten)
        self.assertEqual(rewritten.generation, 1)
        self.assertEqual(rewritten.rewrite_reason, "sent_prefix_changed")
        self.assertEqual(rewritten.common_prefix_messages, 1)
        self.assertFalse(appended.rewritten)
        self.assertEqual(appended.generation, 1)

    def test_agent_emits_rewrite_observation_after_context_mutates_history(self) -> None:
        responses = [
            ModelResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "large_output",
                        "input": {},
                    }
                ],
            ),
            ModelResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "large_output",
                        "input": {},
                    }
                ],
            ),
            ModelResponse(
                stop_reason="end_turn",
                content=[{"type": "text", "text": "done"}],
            ),
        ]

        class SequenceClient:
            def create_message(self, **kwargs):
                return responses.pop(0)

        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition(
                name="large_output",
                description="Return a large result.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: "x" * 200,
        )
        events = []
        agent = Agent(
            client=SequenceClient(),
            tools=tools,
            config=AgentConfig(model="fake-model", system_prompt="test"),
            context=ContextManager(
                config=ContextConfig(
                    keep_recent_tool_results=1,
                    max_messages=100,
                    compact_threshold_chars=1_000_000,
                )
            ),
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )

        result = agent.run("inspect")

        self.assertEqual(result.final_text, "done")
        prompt_events = [event for event in events if event.type == "prompt.assembled"]
        rewrite_events = [event for event in events if event.type == "history.rewritten"]
        self.assertEqual(len(prompt_events), 3)
        self.assertEqual(
            [event.payload["history_generation"] for event in prompt_events],
            [0, 0, 1],
        )
        self.assertEqual(
            [event.payload["history_rewritten"] for event in prompt_events],
            [False, False, True],
        )
        self.assertEqual(len(rewrite_events), 1)
        self.assertEqual(
            rewrite_events[0].payload["rewrite_reason"],
            "sent_prefix_changed",
        )

    def test_recording_sink_attaches_observation_to_next_main_model_call(self) -> None:
        class Repository:
            def __init__(self) -> None:
                self.created = []

            def append_event(self, event):
                return event

            def create_model_call(self, run_id, **kwargs):
                self.created.append((run_id, kwargs))

        repository = Repository()
        sink = RecordingEventSink(repository)
        sink.emit(
            RunEvent(
                type="prompt.assembled",
                run_id="run_1",
                agent_id="agent_root",
                iteration=5,
                payload={
                    "prompt_hash": "prompt-hash",
                    "history_generation": 1,
                    "history_rewritten": True,
                    "rewrite_reason": "sent_prefix_changed",
                    "previous_message_count": 7,
                    "current_message_count": 9,
                    "common_prefix_messages": 2,
                    "discarded_prefix_messages": 5,
                    "appended_messages": 7,
                    "previous_history_hash": "previous-hash",
                    "current_history_hash": "current-hash",
                },
            )
        )
        sink.emit(
            RunEvent(
                type="model.started",
                run_id="run_1",
                agent_id="agent_root",
                payload={
                    "call_id": "call_1",
                    "model": "test-model",
                    "call_kind": "main",
                },
            )
        )

        metadata = repository.created[0][1]["metadata"]
        self.assertEqual(metadata["iteration"], 5)
        self.assertEqual(metadata["history_generation"], 1)
        self.assertTrue(metadata["history_rewritten"])
        self.assertEqual(metadata["rewrite_reason"], "sent_prefix_changed")


if __name__ == "__main__":
    unittest.main()
