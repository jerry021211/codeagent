from __future__ import annotations

import json
import unittest
from copy import deepcopy

from codeagent.context.summary_source import bounded_summary_data, bounded_summary_messages, summary_source_messages


class SummarySourceTests(unittest.TestCase):
    def test_large_tool_round_keeps_identity_status_and_failure_tail(self):
        path = "D:/项目/失败修复.py"
        failure = "TRACE START\n" + "x" * 150_000 + "\nFAILED test_public_api; exit=2; ERROR E1234"
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "write-7", "name": "write_file", "input": {"file_path": path, "content": "HEAD\n" + "body" * 80_000 + "\nTAIL"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write-7", "is_error": True, "status": "error", "exit_code": 2, "content": failure}]},
        ]
        before = deepcopy(messages)
        result = bounded_summary_messages(messages, text_limit=300, argument_limit=200)
        call, receipt = result[0]["content"][0], result[1]["content"][0]
        self.assertEqual((call["id"], call["name"], call["input"]["file_path"]), ("write-7", "write_file", path))
        self.assertEqual((receipt["tool_use_id"], receipt["is_error"], receipt["status"], receipt["exit_code"]), ("write-7", True, "error", 2))
        self.assertIn("FAILED test_public_api", receipt["content"]["preview"])
        self.assertIn("ERROR E1234", receipt["content"]["preview"])
        self.assertEqual(receipt["content"]["original_chars"], len(failure))
        self.assertTrue(receipt["content"]["truncated"])
        self.assertLessEqual(len(receipt["content"]["preview"]), 300)
        self.assertLessEqual(len(call["input"]["content"]["preview"]), 200)
        self.assertLess(len(json.dumps(result, ensure_ascii=False, allow_nan=False)), 2000)
        self.assertEqual(messages, before)

    def test_parallel_tool_group_is_not_reduced_to_generic_list_preview(self):
        calls = [{"type": "tool_use", "id": f"call-{i}", "name": "grep", "input": {"pattern": "long" * 1000}} for i in range(65)]
        results = [{"type": "tool_result", "tool_use_id": f"call-{i}", "is_error": i == 32, "content": "matched" * 1000} for i in range(65)]
        result = bounded_summary_messages([{"role": "assistant", "content": calls}, {"role": "user", "content": results}], text_limit=60, argument_limit=40)
        self.assertEqual([block["id"] for block in result[0]["content"]], [block["id"] for block in calls])
        self.assertEqual([block["tool_use_id"] for block in result[1]["content"]], [block["tool_use_id"] for block in results])
        self.assertTrue(result[1]["content"][32]["is_error"])

    def test_media_and_hidden_reasoning_are_not_summarized_as_visible_evidence(self):
        messages = [{"role": "assistant", "reasoning_content": "private-chain", "content": [
            {"type": "thinking", "thinking": "private-chain", "signature": "private-signature"},
            {"type": "redacted_thinking", "data": "private-encrypted"},
            {"type": "text", "text": "visible answer"},
        ]}, {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "private-pixels" * 10_000}},
            {"type": "document", "title": "report.pdf", "source": {"type": "url", "url": "https://example.test/report.pdf"}},
            {"type": "tool_result", "tool_use_id": "media-result", "content": [{"type": "image", "url": "data:image/png;base64,private-pixels"}, {"type": "text", "text": "image returned"}]},
        ]}]
        original = deepcopy(messages)
        result = bounded_summary_messages(messages)
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        for secret in ("private-chain", "private-signature", "private-encrypted", "private-pixels"):
            self.assertNotIn(secret, payload)
        for preserved in ("visible answer", "image/png", "report.pdf", "https://example.test/report.pdf", "media-result", "image returned"):
            self.assertIn(preserved, payload)
        self.assertIn("provider_reasoning_not_summarized", payload)
        self.assertIn("media_payload_not_in_summary", payload)
        self.assertEqual(messages, original)

    def test_small_data_preserves_values_and_long_lists_explain_middle_omission(self):
        data = {"goal": "keep", "items": [{"id": f"task-{i}", "description": str(i) * 100, "status": "failed" if i == 99 else "done"} for i in range(100)]}
        original = deepcopy(data)
        result = bounded_summary_data(data, text_limit=30)
        self.assertEqual(result["goal"], "keep")
        preview = result["items"]
        self.assertEqual((preview["original_items"], preview["omitted_items"]), (100, 68))
        self.assertEqual(preview["head"][0]["id"], "task-0")
        self.assertEqual(preview["tail"][-1]["id"], "task-99")
        self.assertEqual(preview["tail"][-1]["status"], "failed")
        self.assertEqual(data, original)
        json.dumps(result, allow_nan=False)

    def test_full_visible_source_still_omits_reasoning_and_binary_without_clipping_text(self):
        body = "visible-long-text" * 10_000
        arguments = {"file_path": "code.py", "content": body, "items": list(range(100))}
        messages = [{"role": "assistant", "reasoning_content": "private-thought", "content": [
            {"type": "thinking", "thinking": "private-thought", "signature": "private-signature"},
            {"type": "text", "text": body},
            {"type": "tool_use", "id": "tool-1", "name": "write_file", "input": arguments},
        ]}, {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "private-pixels"}},
        ]}]
        before = deepcopy(messages)
        result = summary_source_messages(messages)
        self.assertEqual(result[0]["content"][1]["text"], body)
        self.assertEqual(result[0]["content"][2]["input"], arguments)
        payload = json.dumps(result, allow_nan=False)
        for secret in ("private-thought", "private-signature", "private-pixels"):
            self.assertNotIn(secret, payload)
        self.assertEqual(messages, before)

    def test_identity_strings_remain_exact_even_over_field_limit(self):
        identity = "D:/" + "very-long-component/" * 1000 + "code.py"
        result = bounded_summary_data({"file_path": identity, "tool_use_id": "id" * 1000, "description": "description" * 1000}, text_limit=40)
        self.assertEqual(result["file_path"], identity)
        self.assertEqual(result["tool_use_id"], "id" * 1000)
        self.assertLessEqual(len(result["description"]["preview"]), 40)

    def test_unicode_and_tiny_budgets_never_break_preview_limits(self):
        for limit in (0, 1, 3, 10, 100):
            with self.subTest(limit=limit):
                result = bounded_summary_data("甲😀乙" * 1000, text_limit=limit)
                self.assertLessEqual(len(result["preview"]), limit)
                self.assertEqual(result["original_chars"], 3000)
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def test_sdk_blocks_are_read_without_mutation(self):
        class Block:
            def __init__(self):
                self.text = "SDK text " * 2000

            def model_dump(self, *, mode):
                return {"type": "text", "text": self.text}

        block = Block()
        before = block.text
        result = bounded_summary_messages([{"role": "assistant", "content": [block]}], text_limit=90)
        self.assertEqual(block.text, before)
        self.assertTrue(result[0]["content"][0]["text"]["truncated"])
        json.dumps(result, allow_nan=False)

    def test_tool_argument_fields_stay_valid_json_and_separate_from_body_budget(self):
        message = {"role": "assistant", "content": [{"type": "tool_use", "id": "edit", "name": "edit_file", "input": {"file_path": "code.py", "old_string": "old" * 2000, "new_string": "new" * 2000, "nested": {"values": list(range(100))}}}]}
        result = bounded_summary_messages([message], text_limit=800, argument_limit=50)
        arguments = result[0]["content"][0]["input"]
        self.assertEqual(arguments["file_path"], "code.py")
        for key in ("old_string", "new_string"):
            self.assertLessEqual(len(arguments[key]["preview"]), 50)
        self.assertEqual(arguments["nested"]["values"]["omitted_items"], 68)
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)

    def test_unknown_binary_nonfinite_and_deep_data_have_explicit_notes(self):
        data = {"raw": b"private", "number": float("inf"), "other": object()}
        result = bounded_summary_data(data)
        self.assertEqual(result["raw"]["original_bytes"], 7)
        json.dumps(result, allow_nan=False)
        deeply_nested = []
        current = deeply_nested
        for _ in range(100):
            nested = []
            current.append(nested)
            current = nested
        self.assertIn("summary_source_depth_limit", json.dumps(bounded_summary_data(deeply_nested)))

    def test_invalid_limits_are_rejected(self):
        for limit in (-1, True, 0.5, "4"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    bounded_summary_data({}, text_limit=limit)
                with self.assertRaises(ValueError):
                    bounded_summary_messages([], argument_limit=limit)


if __name__ == "__main__":
    unittest.main()
