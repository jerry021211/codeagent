"""Pressure-driven cleanup, useful summaries and immutable incremental archives."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import codeagent.context.budget as budget_module
import codeagent.context.manager as manager_module
from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.context.budget import RequestBudgetError, inspect_request
from codeagent.context.projection import TOOL_VIEW_MARKER, WRITE_VIEW_MARKER, build_tool_projection
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.models import ModelResponse
from codeagent.tools import LoadContextHistoryTool
from tests import test_context_nonteam_regressions as runtime_helpers


class SummaryClient:
    def __init__(self, text="Verified earlier work; preserve the current task."):
        self.text = text
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse("end_turn", [{"type": "text", "text": self.text}])


def rounds(count=10, start=0, *, name="echo", size=1000, marker="evidence"):
    history = [{"role": "user", "content": "Keep the exact current user goal."}]
    for i in range(start, start + count):
        history.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"call_{i}", "name": name,
                                               "input": {"pattern": "lookup"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"call_{i}",
                                          "content": f"{marker}-{i}:" + "x" * size}]},
        ])
    return history


class ContextSimplificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manager(self, **options):
        manager = ContextManager(config=ContextConfig(**{
            "summarization_model": "summary", "transcript_dir": self.root / "archives",
            "tool_output_dir": self.root / "outputs", **options,
        }))
        manager.begin_turn(0)
        return manager

    def test_many_short_dialogue_messages_do_not_trigger_summary_or_archive(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
                   for i in range(100)]
        history.append({"role": "user", "content": "continue"})
        manager = self.manager(message_trigger_min_fold=1, round_trigger_min_fold=1)
        manager.begin_turn(100)
        client = SummaryClient()
        self.assertEqual(manager.prepare_before_model_call(history, client=client), history)
        self.assertEqual(client.calls, [])
        self.assertFalse(manager.config.transcript_dir.exists())

    def test_default_45_short_rounds_need_no_model_summaries(self):
        case = runtime_helpers.NonTeamContextRegressions()
        case.setUp()
        try:
            client = runtime_helpers.LongTaskClient(rounds=45)
            agent = case.agent(client, iterations=60)
            result = agent.run("Complete 45 short steps")
            self.assertEqual(result.final_text, "long task complete")
            self.assertFalse(any(request["model"] == "summary" for _, request in client.calls))
            self.assertFalse(agent.context.config.transcript_dir.exists())
            validate_tool_history(agent.messages)
        finally:
            case.doCleanups()

    def test_no_pressure_computes_complete_request_budget_once(self):
        manager = self.manager()
        measured = []
        def measure(**kwargs):
            measured.append(len(kwargs["messages"]))
            return inspect_request(**kwargs)
        with patch.object(manager_module, "inspect_request", side_effect=measure), patch.object(
            budget_module, "inspect_request", side_effect=measure,
        ):
            manager.prepare_before_model_call([{"role": "user", "content": "hello"}], model="main")
        self.assertEqual(measured, [1])

    def test_old_large_tools_are_untouched_until_pressure_and_can_avoid_summary(self):
        history = rounds(5, name="grep", size=10000)
        original = deepcopy(history)
        client = SummaryClient()
        manager = self.manager()
        self.assertEqual(manager.prepare_before_model_call(history, client=client), original)
        manager.config.compact_threshold_chars = 30000
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertIn(TOOL_VIEW_MARKER, str(projected))
        self.assertEqual(client.calls, [])
        self.assertEqual(history, original)
        self.assertEqual(projected[-4:], history[-4:])
        validate_tool_history(projected)

    def test_legacy_unarchived_evidence_and_attachments_survive_new_turn_even_under_pressure(self):
        history = rounds(3, name="bash", size=10000)
        history[2]["content"][0]["content"] = "HEAD" + "x" * 4000 + "UNIQUE_MIDDLE_EVIDENCE" + "y" * 4000 + "TAIL"
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "original-media"}}
        history[0]["content"] = [{"type": "text", "text": "old goal"}, image]
        old_count = len(history)
        history.append({"role": "user", "content": "next turn"})
        original = deepcopy(history)
        manager = self.manager(mode="off", compact_threshold_chars=1)
        manager.begin_turn(old_count)
        projected = manager.prepare_before_model_call(history)
        self.assertEqual(projected, original)
        self.assertIn("UNIQUE_MIDDLE_EVIDENCE", str(projected))
        validate_tool_history(projected)

    def test_short_bash_success_and_failure_do_not_create_archive_envelopes(self):
        manager = self.manager()
        tool = ToolUse("command", "bash", {"command": "example"})
        for text in ("x" * 2000, "Error: " + "x" * 10000):
            self.assertEqual(manager.finalize_tool_results([tool], [text]), [text])
        self.assertFalse(manager.config.tool_output_dir.exists())
        result = manager.finalize_tool_results([tool], ["x" * 100000])[0]
        self.assertIn("[tool output stored]", result)
        self.assertEqual(len(manager.state.tool_artifacts), 1)

    def test_small_edits_keep_arguments_and_large_write_notes_have_no_language_outline(self):
        def history(size):
            messages = rounds(1, name="write_file")
            messages[1]["content"][0]["input"] = {"file_path": "module.py", "content": "def old_function():\n" + "x" * size}
            messages[2]["content"][0]["content"] = "Wrote 2 lines to module.py"
            messages.extend([{"role": "assistant", "content": "check one"}, {"role": "assistant", "content": "check two"}])
            return messages
        small = history(500)
        self.assertEqual(build_tool_projection(small), small)
        large = history(10000)
        self.assertEqual(build_tool_projection(large[:-1]), large[:-1])
        projected = build_tool_projection(large)
        self.assertEqual(projected[1]["content"][0]["input"], {"file_path": "module.py"})
        self.assertIn(WRITE_VIEW_MARKER, str(projected))
        self.assertNotIn("old_function", str(projected))
        self.assertIn("old_function", str(large))

    def test_nonshrinking_summary_is_not_committed_or_archived_and_is_cooled_down(self):
        history = rounds(size=20)
        manager = self.manager(compact_threshold_chars=1, summary_max_chars=12000)
        original = deepcopy(history)
        client = SummaryClient("too verbose " * 800)
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertEqual(projected, original)
        self.assertEqual(manager.state.summary_revision, 0)
        self.assertEqual(manager.last_compaction["reason"], "insufficient_savings")
        self.assertGreater(manager.state.summary_retry_after_epoch, 0)
        self.assertFalse(manager.config.transcript_dir.exists())
        manager.prepare_before_model_call(history, client=client)
        restored = ContextManager(config=manager.config, state=RuntimeState(**asdict(manager.state)))
        restored.prepare_before_model_call(history, client=client)
        self.assertEqual(len(client.calls), 1)

    def test_nonshrinking_summary_cannot_bypass_the_main_hard_limit(self):
        history = rounds(size=20)
        manager = self.manager(max_request_chars=1500)
        with self.assertRaises(RequestBudgetError):
            manager.prepare_before_model_call(history, client=SummaryClient("verbose " * 1000))
        self.assertEqual(manager.state.summary_revision, 0)

    def test_model_window_pressure_triggers_summary_without_character_pressure(self):
        manager = self.manager(model_context_windows={"main": 8000})
        client = SummaryClient()
        projected = manager.prepare_before_model_call(rounds(), client=client, model="main", max_tokens=1000)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(manager.state.summary_revision, 1)
        self.assertIn("context_summary", str(projected))

    def test_incremental_archives_store_each_folded_message_once_and_page_across_segments(self):
        history, manager, client = rounds(), self.manager(), SummaryClient()
        manager.force_compact(history, client=client)
        first_path = Path(manager.state.summary_transcript)
        first_bytes = first_path.read_bytes()
        first_cursor = manager.state.compacted_message_count
        for start in (10, 18):
            history.extend(rounds(8, start=start)[1:])
            manager.force_compact(history, client=client)
        messages = []
        for path in manager.config.transcript_dir.glob("*.jsonl"):
            messages.extend(item for line in path.read_text(encoding="utf-8").splitlines()
                            if "_context_archive" not in (item := json.loads(line)))
        self.assertEqual(len(messages), manager.state.compacted_message_count)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        tool = LoadContextHistoryTool(manager.config.transcript_dir)
        page = tool.run(manager.state.summary_transcript, message_offset=first_cursor, message_limit=3, char_limit=4000)
        for index in range(first_cursor, first_cursor + 3):
            self.assertIn(json.dumps(history[index - 1], ensure_ascii=False), page)
        self.assertIn(f"next_message_offset={first_cursor + 3}", page)
        self.assertIn("no messages", tool.run(str(first_path), message_offset=first_cursor + 1))

    def test_resumed_branches_share_old_immutable_segment_without_leaking_new_evidence(self):
        history, manager = rounds(), self.manager()
        manager.force_compact(history, client=SummaryClient())
        state = asdict(manager.state)
        original = deepcopy(history)
        old = Path(manager.state.summary_transcript).read_bytes()
        history.extend(rounds(8, start=10, marker="LEFT_BRANCH")[1:])
        manager.force_compact(history, client=SummaryClient())
        other = ContextManager(config=manager.config, state=RuntimeState(**state))
        original.extend(rounds(8, start=10, marker="RIGHT_BRANCH")[1:])
        other.force_compact(original, client=SummaryClient())
        tool = LoadContextHistoryTool(manager.config.transcript_dir)
        left = tool.run(manager.state.summary_transcript, message_offset=23, message_limit=1)
        right = tool.run(other.state.summary_transcript, message_offset=23, message_limit=1)
        self.assertIn("LEFT_BRANCH", left)
        self.assertNotIn("RIGHT_BRANCH", left)
        self.assertIn("RIGHT_BRANCH", right)
        self.assertNotIn("LEFT_BRANCH", right)
        self.assertEqual(Path(state["summary_transcript"]).read_bytes(), old)

    def test_archive_links_reject_cycles_outside_paths_and_broken_ranges(self):
        root = self.root / "archives"
        root.mkdir()
        tool = LoadContextHistoryTool(root)
        base = root / "base.jsonl"
        base.write_text('{"role":"user","content":"base"}\n', encoding="utf-8")
        for previous, start, end in (("link.jsonl", 1, 2), ("../outside.jsonl", 1, 2),
                                     ("base.jsonl", 2, 3), ("base.jsonl", 1, 1)):
            with self.subTest(previous=previous, start=start, end=end):
                path = root / "link.jsonl"
                path.write_text(json.dumps({"_context_archive": 1, "previous": previous, "start": start, "end": end})
                                + '\n{"role":"user","content":"new"}\n', encoding="utf-8")
                self.assertTrue(tool.run(str(path)).startswith("Error:"))

    def test_same_clock_tick_never_overwrites_an_archive(self):
        manager = self.manager()
        with patch("codeagent.context.manager.datetime") as clock:
            clock.now.return_value.strftime.return_value = "same-clock-tick"
            first = manager.write_transcript([{"role": "user", "content": "first"}], reason="test")
            second = manager.write_transcript([{"role": "user", "content": "second"}], reason="test")
        self.assertNotEqual(first, second)
        self.assertIn("first", first.read_text(encoding="utf-8"))
        self.assertNotIn("second", first.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
