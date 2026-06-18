from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.messages import ToolUse
from codeagent.tools import TodoStore


class ContextManagerTests(unittest.TestCase):
    def test_compact_tool_output_truncates_single_large_result(self) -> None:
        manager = ContextManager(
            config=ContextConfig(single_tool_output_max_chars=10)
        )

        output = manager.compact_tool_output(
            ToolUse(id="toolu_1", name="bash", input={"command": "x"}),
            "abcdefghijklmnopqrstuvwxyz",
        )

        self.assertIn("[tool output truncated]", output)
        self.assertIn("original_chars: 26", output)
        self.assertIn("abcdefghij", output)

    def test_tool_result_budget_persists_large_last_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ContextManager(
                config=ContextConfig(
                    tool_result_budget_chars=20,
                    tool_output_dir=Path(temp_dir) / "outputs",
                )
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_big",
                            "content": "x" * 100,
                        }
                    ],
                }
            ]

            compacted = manager.tool_result_budget(messages)

            content = compacted[-1]["content"][0]["content"]
            self.assertIn("<persisted-output", content)
            self.assertTrue((Path(temp_dir) / "outputs" / "toolu_big.txt").exists())

    def test_snip_compact_keeps_tool_use_and_result_together(self) -> None:
        manager = ContextManager(
            config=ContextConfig(
                max_messages=5,
                keep_head_messages=1,
                keep_tail_messages=4,
            )
        )
        messages = [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1"}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}],
            },
            {"role": "assistant", "content": "middle"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "tail"},
            {"role": "user", "content": "done"},
        ]

        compacted = manager.snip_compact(messages)

        self.assertTrue(any("snipped" in str(message["content"]) for message in compacted))
        roles = [message["role"] for message in compacted]
        self.assertEqual(roles[-4:], ["assistant", "user", "assistant", "user"])

    def test_micro_compact_replaces_old_tool_results(self) -> None:
        manager = ContextManager(
            config=ContextConfig(keep_recent_tool_results=1)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "old",
                        "content": "old output " * 50,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "new",
                        "content": "new output " * 50,
                    }
                ],
            },
        ]

        compacted = manager.micro_compact(messages)

        self.assertIn("Earlier tool result compacted", compacted[0]["content"][0]["content"])
        self.assertIn("new output", compacted[1]["content"][0]["content"])

    def test_compact_history_uses_runtime_state_and_writes_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoStore(
                todos=[{"content": "Run tests", "status": "pending"}],
                revision=1,
            )
            state = RuntimeState(user_goal="Refactor hello.py")
            state.loaded_skills.append("python-refactor")
            manager = ContextManager(
                config=ContextConfig(
                    transcript_dir=Path(temp_dir) / "transcripts",
                    summary_max_chars=5000,
                ),
                state=state,
                todo_store=store,
            )

            compacted = manager.compact_history(
                [{"role": "user", "content": "hello"}],
                reason="test compact",
            )

            self.assertEqual(len(compacted), 1)
            summary = compacted[0]["content"]
            self.assertIn("Refactor hello.py", summary)
            self.assertIn("python-refactor", summary)
            self.assertIn("Run tests", summary)
            self.assertTrue(list((Path(temp_dir) / "transcripts").glob("*.jsonl")))


if __name__ == "__main__":
    unittest.main()
