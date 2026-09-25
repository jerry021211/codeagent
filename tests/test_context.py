from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.context import (
    ContextCompactionError,
    ContextConfig,
    ContextManager,
)
from codeagent.messages import ToolUse


class SummaryResponse:
    def __init__(self, text: str) -> None:
        self.content = [{"type": "text", "text": text}]


class SummaryClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return SummaryResponse(self.responses.pop(0))


class ContextManagerTests(unittest.TestCase):
    def test_tool_results_are_finalized_before_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "outputs"
            manager = ContextManager(
                config=ContextConfig(
                    summarization_model="summary-model",
                    single_tool_output_max_chars=500,
                    tool_result_budget_chars=700,
                    persisted_preview_chars=80,
                    tool_output_dir=output_dir,
                )
            )
            tool_uses = [
                ToolUse(id="toolu_1", name="read_file", input={"path": "a.py"}),
                ToolUse(id="toolu_2", name="read_file", input={"path": "b.py"}),
            ]

            finalized = manager.finalize_tool_results(
                tool_uses, ["a" * 900, "b" * 450]
            )

            self.assertLessEqual(sum(map(len, finalized)), 700)
            self.assertTrue((output_dir / "toolu_1.txt").exists())
            self.assertIn("只有在当前预览缺少必要信息时", finalized[0])
            self.assertNotIn("Re-run the tool", "\n".join(finalized))

    def test_history_is_unchanged_below_threshold(self) -> None:
        manager = ContextManager(
            config=ContextConfig(
                summarization_model="summary-model",
                compact_threshold_chars=10_000,
            )
        )
        messages = [{"role": "user", "content": "hello"}]

        prepared = manager.prepare_before_model_call(messages)

        self.assertIs(prepared, messages)
        self.assertEqual(manager.state.history_generation, 0)

    def test_first_and_update_summary_start_one_generation_each(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = SummaryClient(["## Goal\nfirst", "## Goal\nupdated"])
            manager = ContextManager(
                config=ContextConfig(
                    summarization_model="summary-model",
                    transcript_dir=Path(temp_dir) / "transcripts",
                ),
                task_state_provider=lambda: '[{"id":"1","status":"pending"}]',
            )
            manager.state.files_read["a.py|0|100"] = {
                "path": "a.py",
                "offset": 0,
                "limit": 100,
                "count": 3,
            }

            messages = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"inspect a.py {i}"}
                for i in range(28)
            ]
            first = manager.compact_history(
                messages,
                reason="auto_compact",
                client=client,
            )
            messages.extend([
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"continue {i}"}
                for i in range(16)
            ])
            second = manager.compact_history(
                messages,
                reason="manual_compact",
                client=client,
            )

            self.assertIn('revision="1"', first[0]["content"])
            self.assertIn('revision="2"', second[0]["content"])
            self.assertEqual(len(messages), 44)
            self.assertEqual(manager.state.compacted_message_count, 32)
            self.assertEqual(manager.state.history_generation, 2)
            self.assertIn("<previous-summary>", client.calls[1]["messages"][0]["content"])
            self.assertIn('"count": 3', client.calls[0]["messages"][0]["content"])
            self.assertIn('"status":"pending"', client.calls[0]["messages"][0]["content"])
            self.assertEqual(client.calls[0]["model"], "summary-model")
            self.assertEqual(client.calls[0]["tools"], [])
            self.assertNotIn("max_tokens", client.calls[0])
            self.assertEqual(len(list((Path(temp_dir) / "transcripts").glob("*.jsonl"))), 2)

    def test_summary_failure_keeps_history_and_generation(self) -> None:
        class FailingClient:
            def create_message(self, **kwargs):
                raise RuntimeError("summary unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ContextManager(
                config=ContextConfig(
                    summarization_model="summary-model",
                    transcript_dir=Path(temp_dir) / "transcripts",
                )
            )
            messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "important history"}
                        for i in range(28)]

            with self.assertRaises(ContextCompactionError):
                manager.compact_history(
                    messages,
                    reason="reactive_compact",
                    client=FailingClient(),
                )

            self.assertEqual(len(messages), 28)
            self.assertTrue(all(m["content"] == "important history" for m in messages))
            self.assertEqual(manager.state.history_generation, 0)
            self.assertFalse((Path(temp_dir) / "transcripts").exists())


if __name__ == "__main__":
    unittest.main()
