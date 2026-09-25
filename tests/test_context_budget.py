from __future__ import annotations

import json
import unittest
from copy import deepcopy

from codeagent.context.budget import RequestBudgetError, enforce_request, inspect_request


class ContextBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = {
            "model": "example-model",
            "system": "System instructions.",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
            "max_tokens": 100,
        }

    def test_complete_request_and_unicode_character_count(self) -> None:
        self.request["messages"][0]["content"] = "中文🙂"
        expected = json.dumps(self.request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result = inspect_request(**self.request)
        self.assertEqual(result.request_chars, len(expected))
        self.assertGreater(len(expected.encode("utf-8")), result.request_chars)
        self.assertEqual(result.estimated_total_tokens, result.estimated_prompt_tokens + 100)
        self.assertTrue(result.to_dict()["token_count_is_estimate"])

    def test_system_tools_and_new_user_material_each_consume_budget(self) -> None:
        baseline = inspect_request(**self.request)
        for field in ("system", "tools", "messages"):
            changed = deepcopy(self.request)
            if field == "system":
                changed[field] += "x" * 1000
            elif field == "tools":
                changed[field][0]["description"] = "x" * 1000
            else:
                changed[field].append({"role": "user", "content": "x" * 1000})
            with self.subTest(field=field), self.assertRaises(RequestBudgetError):
                enforce_request(**changed, max_request_chars=baseline.request_chars + 100)

    def test_limits_accept_boundary_and_reject_one_over(self) -> None:
        budget = inspect_request(**self.request)
        self.assertEqual(enforce_request(**self.request, max_request_chars=budget.request_chars), budget)
        with self.assertRaises(RequestBudgetError) as raised:
            enforce_request(**self.request, max_request_chars=budget.request_chars - 1)
        self.assertEqual(raised.exception.reason, "max_request_chars")
        self.assertNotIn("System instructions", str(raised.exception))

    def test_output_and_safety_margin_reserved_from_model_window(self) -> None:
        budget = inspect_request(**self.request)
        self.assertEqual(enforce_request(**self.request, context_window_tokens=budget.estimated_total_tokens), budget)
        with self.assertRaises(RequestBudgetError) as raised:
            enforce_request(**self.request, context_window_tokens=budget.estimated_total_tokens, safety_margin_tokens=1)
        self.assertEqual(raised.exception.reason, "context_window_tokens")
        changed = {**self.request, "max_tokens": 10000}
        with self.assertRaises(RequestBudgetError):
            enforce_request(**changed, context_window_tokens=1000)

    def test_disabled_unknown_model_limits_do_not_guess_a_window(self) -> None:
        changed = {**self.request, "model": "unknown-model", "max_tokens": 1000000}
        for disabled in (None, 0):
            result = enforce_request(**changed, max_request_chars=disabled, context_window_tokens=disabled)
            self.assertEqual(result.output_reserve_tokens, 1000000)

    def test_media_inside_tool_result_preserved_and_reserves_tokens(self) -> None:
        self.request["messages"] = [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tool_1", "content": [
                {"type": "text", "text": "image evidence"},
                {"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}},
            ],
        }]}]
        original = deepcopy(self.request)
        budget = inspect_request(**self.request)
        self.assertEqual(budget.multimodal_blocks, 1)
        self.assertGreaterEqual(budget.estimated_prompt_tokens, 8192)
        with self.assertRaises(RequestBudgetError):
            enforce_request(**self.request, context_window_tokens=1000)
        self.assertEqual(self.request, original)

    def test_request_serialization_is_stable_across_mapping_order(self) -> None:
        other = {key: value for key, value in reversed(list(self.request.items()))}
        other["messages"] = [{"content": "hello", "role": "user"}]
        self.assertEqual(inspect_request(**self.request), inspect_request(**other))

    def test_configuration_rejects_negative_fractional_and_boolean_values(self) -> None:
        for name in ("max_request_chars", "context_window_tokens", "safety_margin_tokens", "max_tokens"):
            for value in (-1, 0.5, True, "100"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    enforce_request(**{**self.request, name: value})

    def test_unserializable_or_nonfinite_content_is_not_silently_stringified(self) -> None:
        for value in (object(), float("nan"), float("inf")):
            changed = {**self.request, "messages": [{"role": "user", "content": value}]}
            with self.subTest(value=type(value)), self.assertRaises((ValueError, TypeError)):
                inspect_request(**changed)

    def test_sdk_content_blocks_use_their_json_shape(self) -> None:
        class SDKBlock:
            def model_dump(self, *, mode):
                self.asserted_mode = mode
                return {"type": "text", "text": "中文"}
        block = SDKBlock()
        result = inspect_request(**{**self.request, "messages": [{"role": "user", "content": [block]}]})
        expected = inspect_request(**{**self.request, "messages": [{"role": "user", "content": [{"type": "text", "text": "中文"}]}]})
        self.assertEqual(result, expected)
        self.assertEqual(block.asserted_mode, "json")


if __name__ == "__main__":
    unittest.main()
