from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import (
    AnthropicModelClient,
    CallbackEventSink,
    EnvironmentConfig,
    EventEmitter,
    UsageTracker,
)
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import CancellationToken, ModelCallTimeout


class FakeMessages:
    def __init__(self, final_message):
        self.final_message = final_message
        self.last_create_params = None
        self.last_stream_params = None

    def create(self, **params):
        self.last_create_params = params
        return self.final_message

    def stream(self, **params):
        self.last_stream_params = params
        return FakeStream(self.final_message)


class FakeStream:
    text_stream = ["hello", " world"]

    def __init__(self, final_message):
        self.final_message = final_message

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get_final_message(self):
        return self.final_message

    def __iter__(self):
        for text in self.text_stream:
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text))

    def close(self):
        pass


class FakeSdkClient:
    def __init__(self, final_message):
        self.messages = FakeMessages(final_message)
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self


class AnthropicClientTests(unittest.TestCase):
    def test_team_thinking_and_tool_arguments_are_activity_without_text(self):
        now = [0.0]
        token = CancellationToken()
        activity = ExecutionActivity(token, response_timeout=5, model_timeout=30, clock=lambda: now[0])
        labels = []
        activity.on_activity = labels.append
        final = SimpleNamespace(stop_reason="tool_use", content=[])
        sdk = FakeSdkClient(final)

        class Stream(FakeStream):
            def __iter__(self):
                for kind, field in [("thinking_delta", "thinking"), ("input_json_delta", "partial_json")]:
                    now[0] += 4
                    yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=kind, **{field: "part"}))

        sdk.messages.stream = lambda **_: Stream(final)
        texts = []
        client = AnthropicModelClient(sdk_client=sdk, activity=activity, stream=True, on_text=texts.append)
        client.create_message(model="fake", system="", messages=[], tools=[], max_tokens=10)
        self.assertEqual(texts, [])
        self.assertIn("model_receiving", labels)
        self.assertEqual(sdk.options[0], {"max_retries": 0, "timeout": 5})
        self.assertIs(client.fork(stream=False).activity, activity)

    def test_keepalive_does_not_extend_content_idle_deadline(self):
        now = [0.0]
        activity = ExecutionActivity(CancellationToken(), response_timeout=5, clock=lambda: now[0])
        final = SimpleNamespace(stop_reason="end_turn", content=[])
        sdk = FakeSdkClient(final)

        class Stream(FakeStream):
            def __iter__(self):
                for i in range(1, 7):
                    now[0] = i
                    yield SimpleNamespace(type="ping")

        sdk.messages.stream = lambda **_: Stream(final)
        client = AnthropicModelClient(sdk_client=sdk, stream=True, activity=activity)
        with self.assertRaisesRegex(ModelCallTimeout, "model_response_timeout"):
            client.create_message(model="fake", system="", messages=[], tools=[], max_tokens=10)

    def test_interrupted_io_records_runtime_deadline_not_network_error(self):
        now = [0.0]
        activity = ExecutionActivity(CancellationToken(), response_timeout=5, clock=lambda: now[0])
        sdk = FakeSdkClient(None)

        def create(**_):
            now[0] = 6
            raise OSError("stream closed")

        sdk.messages.create = create
        events = []
        client = AnthropicModelClient(
            sdk_client=sdk, activity=activity,
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )
        with self.assertRaises(ModelCallTimeout):
            client.create_message(model="fake", system="", messages=[], tools=[], max_tokens=10)
        self.assertEqual([event.type for event in events], ["model.started", "model.failed"])
        self.assertEqual(events[-1].payload["error_type"], "ModelCallTimeout")
        self.assertEqual(events[-1].payload["error"], "model_response_timeout")

    def test_non_streaming_late_response_is_discarded(self):
        now = [0.0]
        activity = ExecutionActivity(CancellationToken(), response_timeout=5, clock=lambda: now[0])
        sdk = FakeSdkClient(SimpleNamespace(stop_reason="tool_use", content=[]))

        def create(**_):
            now[0] = 6
            return sdk.messages.final_message

        sdk.messages.create = create
        client = AnthropicModelClient(sdk_client=sdk, activity=activity)
        with self.assertRaises(ModelCallTimeout):
            client.create_message(model="fake", system="", messages=[], tools=[], max_tokens=10)
        self.assertEqual(sdk.options[0]["max_retries"], 0)

    def test_non_streaming_create_message_preserves_anthropic_content_blocks(self) -> None:
        final_message = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="read_file",
                    input={"file_path": "README.md"},
                )
            ],
        )
        sdk_client = FakeSdkClient(final_message)
        client = AnthropicModelClient(sdk_client=sdk_client)

        response = client.create_message(
            model="claude-test",
            system="system",
            messages=[{"role": "user", "content": "read"}],
            tools=[{"name": "read_file", "description": "read", "input_schema": {}}],
            max_tokens=100,
        )

        self.assertEqual(response.stop_reason, "tool_use")
        self.assertEqual(
            response.content,
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"file_path": "README.md"},
                }
            ],
        )
        self.assertEqual(
            sdk_client.messages.last_create_params["tools"][0]["input_schema"],
            {},
        )
        self.assertIsNotNone(response.usage)
        self.assertFalse(response.usage.available)

    def test_streaming_create_message_emits_text_and_returns_final_message(self) -> None:
        final_message = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="hello world")],
        )
        chunks: list[str] = []
        sdk_client = FakeSdkClient(final_message)
        client = AnthropicModelClient(
            sdk_client=sdk_client,
            stream=True,
            on_text=chunks.append,
        )

        response = client.create_message(
            model="claude-test",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=100,
        )

        self.assertEqual(chunks, ["hello", " world"])
        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(response.content, [{"type": "text", "text": "hello world"}])
        self.assertEqual(sdk_client.messages.last_stream_params["model"], "claude-test")

    def test_normalizes_usage_and_emits_model_events(self) -> None:
        final_message = SimpleNamespace(
            model="claude-test",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=7,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=5,
            ),
        )
        events = []
        tracker = UsageTracker()
        client = AnthropicModelClient(
            sdk_client=FakeSdkClient(final_message),
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
            usage_tracker=tracker,
            call_kind="memory_select",
        )

        response = client.create_message(
            model="claude-test",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=100,
        )

        self.assertIsNotNone(response.usage)
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 26)
        self.assertEqual(response.usage.prompt_input_tokens, 19)
        self.assertAlmostEqual(response.usage.cache_hit_ratio, 5 / 19)
        self.assertEqual(response.usage.call_kind, "memory_select")
        self.assertEqual(tracker.snapshot().total_tokens, 26)
        self.assertEqual(
            [event.type for event in events],
            ["model.started", "model.completed", "usage.updated"],
        )

    def test_fork_shares_usage_tracker_and_can_override_call_kind(self) -> None:
        final_message = SimpleNamespace(
            stop_reason="end_turn",
            content=[],
            usage={"input_tokens": 2, "output_tokens": 1},
        )
        tracker = UsageTracker()
        client = AnthropicModelClient(
            sdk_client=FakeSdkClient(final_message),
            usage_tracker=tracker,
        )

        forked = client.fork(stream=False, call_kind="subagent")
        response = forked.create_message(
            model="test", system="", messages=[], tools=[], max_tokens=10
        )

        assert response.usage is not None
        self.assertEqual(response.usage.call_kind, "subagent")
        self.assertEqual(tracker.snapshot().model_calls, 1)


class EnvironmentConfigTests(unittest.TestCase):
    def test_from_env_loads_dotenv_without_overriding_existing_values(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "MODEL_ID=from-dotenv",
                        "SUMMARIZATION_MODEL_ID=summary-from-dotenv",
                        "SUMMARIZATION_API_KEY=summary-key-from-dotenv",
                        "ANTHROPIC_API_KEY=dotenv-key",
                        "ANTHROPIC_BASE_URL=https://anthropic.test",
                        "STREAMING=true",
                    ]
                ),
                encoding="utf-8",
            )
            os.chdir(temp_dir)
            try:
                with patch.dict(
                    os.environ,
                    {"MODEL_ID": "from-process"},
                    clear=True,
                ):
                    config = EnvironmentConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.model_id, "from-process")
        self.assertEqual(config.api_key, "dotenv-key")
        self.assertEqual(config.base_url, "https://anthropic.test")
        self.assertTrue(config.stream)


if __name__ == "__main__":
    unittest.main()
