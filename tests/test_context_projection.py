from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from codeagent import create_default_registry
from codeagent.context.projection import (
    TOOL_VIEW_MARKER,
    WRITE_VIEW_MARKER,
    build_tool_projection as _build_tool_projection,
    is_projection_placeholder,
    truncate_head_tail,
)
from codeagent.messages import validate_tool_history
from codeagent.tools.read import ReadFileTool


# Exercise policy edges with explicit small test thresholds; production defaults
# and pressure gating are covered by test_context_simplification.
build_tool_projection = partial(_build_tool_projection, min_chars=2000, write_min_chars=500, write_keep=1)

def tool_round(call_id, name="grep", text=None, arguments=None, **result_fields):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": name,
             "input": arguments if arguments is not None else {"pattern": "needle"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id,
             "content": text if text is not None else "证据🧪" * 1000,
             **result_fields},
        ]},
    ]


def result_text(messages, index):
    return messages[index]["content"][0]["content"]


def archived_output(name="bash", path="C:/private/tool-results/command.txt", body=None):
    body = body if body is not None else "证据🧪" * 1000
    return (f"[tool output stored]\ntool: {name}\noriginal_chars: {len(body)}\npath: {path}\n"
            "完整结果已保存到指定路径。\n只有在当前预览缺少必要信息时，才按精确范围读取该文件。\n\n"
            "--- head preview ---\n" + body)


class ToolProjectionTests(unittest.TestCase):
    def test_input_unchanged_and_projection_idempotent(self):
        original = [{"role": "user", "content": "current goal"}]
        for index in range(5):
            original += tool_round(str(index))
        snapshot = deepcopy(original)
        projected = build_tool_projection(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(projected, build_tool_projection(projected))
        self.assertIs(projected[-1], original[-1])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(original)))
        validate_tool_history(projected)

    def test_no_candidates_reuses_original_list(self):
        original = tool_round("short", text="small")
        self.assertIs(build_tool_projection(original), original)

    def test_investigation_and_command_round_windows_are_independent(self):
        messages = []
        for index, name in enumerate(["grep", "bash", "glob", "bash", "grep"]):
            messages += tool_round(str(index), name=name, text=archived_output() if name == "bash" else None)
        projected = build_tool_projection(messages)
        self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 1))
        self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 3))
        for index in (5, 7, 9):
            self.assertEqual(projected[index], messages[index])

    def test_parallel_results_leave_window_as_one_round(self):
        messages = tool_round("a")
        parallel = tool_round("b")
        messages[0]["content"] += parallel[0]["content"]
        messages[1]["content"] += parallel[1]["content"]
        messages += tool_round("c") + tool_round("d")
        projected = build_tool_projection(messages)
        for block in projected[1]["content"]:
            self.assertIn(TOOL_VIEW_MARKER, block["content"])
        self.assertEqual(projected[3:], messages[3:])
        validate_tool_history(projected)

    def test_zero_retention_projects_all_large_results_and_preserves_small(self):
        messages = tool_round("a") + tool_round("b", text="tiny")
        projected = build_tool_projection(messages, investigation_keep=0)
        self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 1))
        self.assertEqual(projected[3], messages[3])

    def test_complete_read_exemption_matches_real_tool_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long.py"
            path.write_text("first = '" + "x" * 2100 + "'\nlast = 2\n", encoding="utf-8")
            reader = ReadFileTool()
            complete = tool_round("full", "read_file", reader.run(str(path)), {"file_path": str(path)})
            self.assertIs(build_tool_projection(complete, investigation_keep=0), complete)
            partial = tool_round("part", "read_file", reader.run(str(path), limit=1), {"file_path": str(path), "limit": 1})
            projected = build_tool_projection(partial, investigation_keep=0)
            self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 1))
            self.assertIn("分段读取", result_text(projected, 1))

    def test_nonzero_offset_is_partial_even_without_truncation_footer(self):
        messages = tool_round("tail", "read_file", "2\t" + "x" * 3000,
                              {"file_path": "a.py", "offset": 2})
        projected = build_tool_projection(messages, investigation_keep=0)
        self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 1))

    def test_complete_reads_do_not_consume_candidate_retention(self):
        messages = tool_round("old") + tool_round("keep")
        messages += tool_round("full", "read_file", "1\t" + "x" * 3000, {"file_path": "a.py"})
        projected = build_tool_projection(messages, investigation_keep=1)
        self.assertIn(TOOL_VIEW_MARKER, result_text(projected, 1))
        self.assertEqual(projected[3:], messages[3:])

    def test_failed_blocked_and_verification_results_remain_complete(self):
        for name, text, fields in [
            ("bash", "failure evidence" * 250 + "\n[exit code: 1]", {}),
            ("bash", "FAILED test_critical\n" + "trace" * 500, {}),
            ("bash", "Blocked: details\n" + "trace" * 500, {}),
            ("bash", "trace" * 500, {"exit_code": 2}),
            ("bash", "trace" * 500, {"status": "blocked"}),
            ("grep", "trace" * 500, {"is_error": True}),
            ("test_run", "trace" * 500, {}),
            ("read_then_delete", "trace" * 500, {}),
        ]:
            with self.subTest(name=name, fields=fields, text=text[:40]):
                messages = tool_round("a", name, text, **fields)
                self.assertIs(build_tool_projection(messages, investigation_keep=0, command_keep=0), messages)

    def test_bash_note_retains_the_received_archive_and_forbids_replay(self):
        path = "C:/private/tool-results/command.txt"
        messages = tool_round("a", "bash", text=archived_output(path=path), arguments={"command": "command"})
        projected = build_tool_projection(messages, command_keep=0)
        text = result_text(projected, 1)
        self.assertIn("不得仅为查看输出而重跑", text)
        self.assertIn("load_tool_output", text)
        self.assertIn(path, text)
        self.assertIn("权限校验", text)

    def test_unarchived_legacy_commands_keep_their_only_available_evidence(self):
        messages = tool_round("a", "bash", arguments={"command": "command"})
        self.assertIs(build_tool_projection(messages, command_keep=0), messages)

    def test_malformed_or_oversized_archive_references_are_never_removed(self):
        for text in (
            "[tool output stored]\npath: invented\n" + "x" * 3000,
            archived_output(name="grep"),
            archived_output(path="C:/" + "x" * 700),
            archived_output().replace("original_chars: 3000", "original_chars: 1"),
        ):
            with self.subTest(text=text[:100]):
                messages = tool_round("a", "bash", text=text)
                self.assertIs(build_tool_projection(messages, command_keep=0), messages)

    def test_unarchived_command_does_not_consume_archive_retention_window(self):
        messages = tool_round("archived", "bash", text=archived_output()) + tool_round("legacy", "bash")
        self.assertIs(build_tool_projection(messages, command_keep=1), messages)

    def test_read_only_archive_keeps_its_historical_reference(self):
        path = "C:/private/tool-results/read.txt"
        messages = tool_round("read", "read_file", text=archived_output("read_file", path),
                              arguments={"file_path": "source.py", "offset": 20})
        projected = build_tool_projection(messages, investigation_keep=0)
        text = result_text(projected, 1)
        self.assertIn("load_tool_output", text)
        self.assertIn(path, text)
        self.assertNotIn("按原参数重新读取", text)

    def test_result_notice_and_long_identity_are_bounded_and_stable(self):
        messages = tool_round("a", arguments={"pattern": "😀" * 5000})
        projected = build_tool_projection(messages, investigation_keep=0)
        text = result_text(projected, 1)
        self.assertLess(len(text), 2000)
        self.assertTrue(is_projection_placeholder(text))
        later = build_tool_projection(messages + tool_round("later"), investigation_keep=0)
        self.assertEqual(text, result_text(later, 1))

    def test_sdk_blocks_and_multimodal_content_survive_without_mutation(self):
        reasoning = SimpleNamespace(type="thinking", thinking="private reasoning", signature="signed")
        image = {"type": "image", "source": {"type": "base64", "data": "image-data", "media_type": "image/png"}}
        text = SimpleNamespace(type="text", text="evidence" * 500)
        call = SimpleNamespace(type="tool_use", id="sdk", name="grep", input={"pattern": "needle"})
        result = SimpleNamespace(type="tool_result", tool_use_id="sdk", content=[text, image])
        messages = [{"role": "assistant", "content": [reasoning, call]},
                    {"role": "user", "content": [result, {"type": "text", "text": "user supplement"}]}]
        projected = build_tool_projection(messages, investigation_keep=0)
        self.assertEqual(text.text, "evidence" * 500)
        self.assertIs(projected[0]["content"][0], reasoning)
        self.assertIs(projected[1]["content"][0].content[1], image)
        self.assertEqual(projected[1]["content"][1], messages[1]["content"][1])
        self.assertIn(TOOL_VIEW_MARKER, projected[1]["content"][0].content[0].text)
        validate_tool_history(projected)

    def test_missing_ambiguous_and_cross_round_results_are_preserved(self):
        missing = tool_round("a")[:1]
        duplicated = tool_round("a") + tool_round("a")
        delayed = tool_round("a")[:1] + [{"role": "assistant", "content": "other"}] + tool_round("a")[1:]
        duplicate_results = tool_round("a")
        duplicate_results[1]["content"] *= 2
        malformed = tool_round("a")
        malformed[0]["content"][0]["input"] = "{invalid-json"
        for messages in (missing, duplicated, delayed, duplicate_results, malformed):
            self.assertIs(build_tool_projection(messages, investigation_keep=0), messages)

    def test_write_success_clears_only_body_after_any_new_assistant(self):
        messages = tool_round("write", "write_file", "Wrote 1 lines to app.py",
                              {"file_path": "app.py", "content": "x" * 3000})
        self.assertIs(build_tool_projection(messages), messages)
        messages += [{"role": "assistant", "content": "继续验证"}]
        original = deepcopy(messages)
        projected = build_tool_projection(messages)
        self.assertEqual(projected[0]["content"][0]["input"], {"file_path": "app.py"})
        self.assertIn(WRITE_VIEW_MARKER, result_text(projected, 1))
        self.assertTrue(result_text(projected, 1).startswith("Wrote 1 lines to app.py"))
        self.assertEqual(messages, original)
        self.assertEqual(projected, build_tool_projection(projected))
        validate_tool_history(projected)

    def test_old_edit_body_counts_even_when_new_body_is_tiny(self):
        messages = tool_round("edit", "edit_file", "Edited app.py\n--- a/app.py\n+++ b/app.py",
                              {"file_path": "app.py", "old_string": "x" * 3000, "new_string": "x"})
        projected = build_tool_projection(messages, write_keep=0)
        self.assertEqual(projected[0]["content"][0]["input"], {"file_path": "app.py"})
        self.assertIn(WRITE_VIEW_MARKER, result_text(projected, 1))

    def test_failed_unknown_or_mismatched_write_keeps_repair_parameters(self):
        for text, fields in [
            ("Error: permission denied", {}),
            ("Wrote 1 lines to app.py", {"is_error": True}),
            ("Wrote 1 lines to app.py", {"status": "blocked"}),
            ("Wrote 1 lines to other.py", {}),
            ("probably succeeded", {}),
            ("[tool output stored]\nuncertain preview", {}),
        ]:
            with self.subTest(text=text, fields=fields):
                messages = tool_round("write", "write_file", text,
                                      {"file_path": "app.py", "content": "x" * 3000}, **fields)
                self.assertIs(build_tool_projection(messages, write_keep=0), messages)

    def test_write_note_preserves_multimodal_blocks(self):
        messages = tool_round("write", "write_file", "Wrote 1 lines to app.py",
                              {"file_path": "app.py", "content": "x" * 3000})
        content = [{"type": "text", "text": "Wrote 1 lines to app.py"}, {"type": "image", "source": {"data": "kept"}}]
        messages[1]["content"][0]["content"] = content
        projected = build_tool_projection(messages, write_keep=0)
        self.assertEqual(result_text(projected, 1)[:2], content)
        self.assertIn(WRITE_VIEW_MARKER, result_text(projected, 1)[2]["text"])

    def test_invalid_configuration_fails_explicitly_and_tiny_threshold_is_safe(self):
        for key in ("investigation_keep", "command_keep", "min_chars", "write_keep", "write_min_chars"):
            for value in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    build_tool_projection([], **{key: value})
        messages = tool_round("a")
        self.assertIs(build_tool_projection(messages, investigation_keep=0, min_chars=1), messages)

    def test_unicode_truncation_never_exceeds_budget(self):
        text = "😀中文🔬" * 100
        for limit in range(100):
            truncated = truncate_head_tail(text, limit)
            self.assertLessEqual(len(truncated), limit)
            truncated.encode("utf-8", errors="strict")
        self.assertEqual(truncate_head_tail("abc", 3), "abc")
        self.assertEqual(truncate_head_tail(text, -1), "")
        self.assertEqual(truncate_head_tail("0123456789", 6, ".."), "012..9")

    def test_projection_stubs_and_legacy_shapes_cannot_write_to_disk(self):
        registry = create_default_registry()
        note = result_text(build_tool_projection(tool_round("a"), investigation_keep=0), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protected.txt"
            path.write_text("original", encoding="utf-8")
            for placeholder in ("[已清理]", "[已清理·须重填]", note):
                for tool, arguments in [
                    ("write_file", {"content": placeholder}),
                    ("edit_file", {"old_string": "original", "new_string": placeholder}),
                    ("edit_file", {"old_string": placeholder, "new_string": "x"}),
                ]:
                    output = registry.execute(tool, {"file_path": str(path), **arguments})
                    self.assertEqual(output.status, "error")
                    self.assertEqual(path.read_text(encoding="utf-8"), "original")
            for metadata in ({"_cleared": True}, {"_landed_summary": "outline"}, {"status": "landed"}, {}):
                output = registry.execute("write_file", {"file_path": str(path), **metadata})
                self.assertEqual(output.status, "error")
                self.assertEqual(path.read_text(encoding="utf-8"), "original")
            prose = "正常文档说明：已清理；例子 [已清理] 仍可作为正文。"
            output = registry.execute("write_file", {"file_path": str(path), "content": prose})
            self.assertEqual(output.status, "success")
            self.assertEqual(path.read_text(encoding="utf-8"), prose)


if __name__ == "__main__":
    unittest.main()
