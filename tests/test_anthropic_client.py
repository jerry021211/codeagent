from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import AnthropicModelClient, EnvironmentConfig


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


class FakeSdkClient:
    def __init__(self, final_message):
        self.messages = FakeMessages(final_message)


class AnthropicClientTests(unittest.TestCase):
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


class EnvironmentConfigTests(unittest.TestCase):
    def test_from_env_loads_dotenv_without_overriding_existing_values(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "MODEL_ID=from-dotenv",
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
