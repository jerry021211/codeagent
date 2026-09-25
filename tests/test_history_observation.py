from __future__ import annotations

import unittest

from codeagent import Agent, AgentConfig, ModelResponse, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager, HistoryObserver
from codeagent.events import CallbackEventSink, EventEmitter, RecordingEventSink, RunEvent


class HistoryObserverTests(unittest.TestCase):
    def test_append_only_history_stays_in_same_generation(self) -> None:
        observer = HistoryObserver()
        observer.observe([{"role": "user", "content": "start"}])

        observation = observer.observe(
            [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "continue"},
            ],
            generation=0,
        )

        self.assertFalse(observation.rewritten)
        self.assertEqual(observation.generation, 0)
        self.assertEqual(observation.current_suffix_messages, 1)
        self.assertEqual(observation.message_count_delta, 1)

    def test_expected_and_unexpected_rewrites_get_clear_reasons(self) -> None:
        observer = HistoryObserver()
        observer.observe([{"role": "user", "content": "old"}], generation=0)

        expected = observer.observe(
            [{"role": "user", "content": "summary"}],
            generation=1,
            generation_reason="auto_compact",
        )
        unexpected = observer.observe(
            [{"role": "user", "content": "mutated"}], generation=1
        )

        self.assertEqual(expected.generation_reason, "auto_compact")
        self.assertEqual(expected.previous_suffix_messages, 1)
        self.assertEqual(unexpected.generation_reason, "unexpected_prefix_change")
        self.assertEqual(unexpected.generation, 2)

    def test_thirty_tool_calls_do_not_rewrite_history(self) -> None:
        responses = [
            ModelResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": f"toolu_{index}",
                        "name": "small_output",
                        "input": {},
                    }
                ],
            )
            for index in range(30)
        ]
        responses.append(
            ModelResponse(
                stop_reason="end_turn",
                content=[{"type": "text", "text": "done"}],
            )
        )

        class SequenceClient:
            def create_message(self, **kwargs):
                return responses.pop(0)

        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition(
                name="small_output",
                description="Return a short result.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: "short stable output",
        )
        events = []
        agent = Agent(
            client=SequenceClient(),
            tools=tools,
            config=AgentConfig(
                model="fake-model", max_iterations=31
            ),
            context=ContextManager(
                config=ContextConfig(
                    mode="off",
                    summarization_model="summary-model",
                    compact_threshold_chars=1_000_000,
                )
            ),
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )

        result = agent.run("inspect")

        prompt_events = [event for event in events if event.type == "prompt.assembled"]
        self.assertEqual(result.final_text, "done")
        self.assertEqual(len(prompt_events), 31)
        self.assertTrue(
            all(not event.payload["history_rewritten"] for event in prompt_events)
        )
        self.assertTrue(
            all(event.payload["history_generation"] == 0 for event in prompt_events)
        )
        self.assertNotIn("Earlier tool result compacted", str(agent.messages))
        self.assertNotIn("[snipped", str(agent.messages))

    def test_recording_sink_attaches_new_observation_fields(self) -> None:
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
                    "history_generation": 1,
                    "history_rewritten": True,
                    "rewrite_reason": "auto_compact",
                    "generation_reason": "auto_compact",
                    "previous_message_count": 7,
                    "current_message_count": 1,
                    "message_count_delta": -6,
                    "common_prefix_messages": 0,
                    "previous_suffix_messages": 7,
                    "current_suffix_messages": 1,
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
        self.assertEqual(metadata["generation_reason"], "auto_compact")
        self.assertEqual(metadata["message_count_delta"], -6)
        self.assertEqual(metadata["previous_suffix_messages"], 7)


if __name__ == "__main__":
    unittest.main()
