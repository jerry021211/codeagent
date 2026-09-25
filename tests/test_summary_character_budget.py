"""Rolling summaries have a character budget, independent of output tokens."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import anthropic
import httpx

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextCompactionError, ContextConfig, ContextManager
from codeagent.context.budget import BoundModelClient, RequestBudgetError, inspect_request
from codeagent.context.summary_source import summary_file_ledger
from codeagent.models import ModelResponse


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, ModelResponse) else ModelResponse("end_turn", value)


class SummaryCharacterBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = ContextManager(config=ContextConfig(
            summarization_model="summary", transcript_dir=Path(temporary.name) / "history",
        ))

    def test_exact_character_boundary_and_whitespace(self):
        # 4000 Unicode code points are well over 4000 UTF-8 bytes.
        text = "中文😀\n" * 1000
        # Use a non-whitespace final character so strip does not change the size.
        expected = text[:-1] + "末"
        client = ScriptedClient(" \n" + expected + "\n ")
        self.assertEqual(self.manager._model_summary([], client=client), expected)
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("max_tokens", client.calls[0])
        self.assertEqual(client.calls[0]["tools"], [])
        self.assertIn("最多 4000 字符", client.calls[0]["system"])
        self.assertIn("不是 token 数", client.calls[0]["system"])

    def test_overlong_output_is_recompressed_once_with_original_evidence(self):
        history = [{"role": "user", "content": "保留目标和原始证据"}]
        client = ScriptedClient("长" * 4001, "  ## 未决问题 / 待办\n继续验证  ")
        self.assertEqual(self.manager._model_summary(history, client=client), "## 未决问题 / 待办\n继续验证")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["messages"][0], client.calls[0]["messages"][0])
        self.assertIn("原始证据", client.calls[1]["messages"][0]["content"])
        self.assertEqual(client.calls[1]["messages"][1], {"role": "assistant", "content": "长" * 4001})
        self.assertIn("4001 字符", client.calls[1]["messages"][-1]["content"])
        self.assertTrue(all("max_tokens" not in call for call in client.calls))

    def test_custom_character_budget_drives_prompt_and_both_validations(self):
        self.manager.config.summary_max_chars = 30
        client = ScriptedClient("字" * 31, "字" * 30)
        self.assertEqual(len(self.manager._model_summary([], client=client)), 30)
        self.assertIn("最多 30 字符", client.calls[0]["system"])
        self.assertIn("最多 30 字符", client.calls[1]["messages"][-1]["content"])

    def test_repair_failure_preserves_existing_summary_watermark_and_archives(self):
        failures = ["长" * 4001, " \n ", TimeoutError("timeout"), RuntimeError("quota"),
                    ModelResponse("max_tokens", "未完整结束")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "原始证据" * 200}
                           for i in range(28)]
                # Independent owner and directory for each subtest.
                with tempfile.TemporaryDirectory() as directory:
                    manager = ContextManager(config=ContextConfig(
                        summarization_model="summary", transcript_dir=Path(directory) / "history",
                    ))
                    manager.force_compact(history, client=ScriptedClient("旧摘要"))
                    history.extend(deepcopy(history[:16]))
                    before, original = asdict(manager.state), deepcopy(history)
                    archives = {p: p.read_bytes() for p in manager.config.transcript_dir.glob("*")}
                    client = ScriptedClient("长" * 4001, failure)
                    with self.assertRaises(ContextCompactionError):
                        manager.force_compact(history, client=client)
                    self.assertEqual(len(client.calls), 2)
                    for key, value in before.items():
                        if key not in {"summary_retry_after_epoch", "summary_failure_scope"}:
                            self.assertEqual(getattr(manager.state, key), value, key)
                    self.assertEqual(history, original)
                    self.assertEqual({p: p.read_bytes() for p in manager.config.transcript_dir.glob("*")}, archives)

    def test_valid_repair_commits_only_once_and_is_reused(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "历史" * 300}
                   for i in range(28)]
        original = deepcopy(history)
        client = ScriptedClient("长" * 4001, "合格摘要")
        self.manager.force_compact(history, client=client)
        self.assertEqual(self.manager.state.summary_text, "合格摘要")
        self.assertEqual(self.manager.state.summary_revision, 1)
        self.assertEqual(self.manager.state.compacted_message_count, 16)
        self.assertEqual(len(list(self.manager.config.transcript_dir.glob("*"))), 1)
        self.manager.force_compact(history, client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(history, original)

    def test_empty_exception_or_incomplete_first_response_is_not_retried(self):
        for response in ("\n  ", TimeoutError("timeout"), ModelResponse("max_tokens", "截断")):
            client = ScriptedClient(response)
            with self.assertRaises((RuntimeError, TimeoutError)):
                self.manager._model_summary([], client=client)
            self.assertEqual(len(client.calls), 1)

    def test_repair_checks_complete_input_budget_before_second_call(self):
        client = ScriptedClient("x" * 8000)
        self.manager.config.summary_input_max_chars = 6000
        with self.assertRaises(RequestBudgetError):
            self.manager._model_summary([], client=client)
        self.assertEqual(len(client.calls), 1)

    def test_paths_are_exact_deduplicated_batch_records_not_guessed_from_text(self):
        path = "D:/项目/修复 文件.py"
        messages = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "write_file", "input": {"file_path": path, "content": "fake.py"}},
            {"type": "tool_use", "id": "b", "name": "read_file", "input": {"path": path}},
            {"type": "text", "text": "计划编辑 guessed.py"},
        ]}]
        self.assertEqual(summary_file_ledger(messages), [path])
        request = self.manager._summary_params(messages)
        self.assertIn("# 本批涉及的文件", request["messages"][0]["content"])
        self.assertIn(path, request["messages"][0]["content"])
        self.assertIn("首次压缩", self.manager._summary_params([])["messages"][0]["content"])
        self.assertNotIn("# 本批涉及的文件", self.manager._summary_params([])["messages"][0]["content"])

    def test_budget_without_token_cap_does_not_reserve_characters_as_tokens(self):
        request = self.manager._summary_params([])
        budget = inspect_request(**request)
        self.assertEqual(budget.output_reserve_tokens, 0)
        self.assertEqual(budget.estimated_total_tokens, budget.estimated_prompt_tokens)
        client = ScriptedClient("ok")
        BoundModelClient(client, max_request_chars=10000).create_message(**request)
        self.assertNotIn("max_tokens", client.calls[0])

    def test_actual_sdk_transport_omits_summary_cap_and_preserves_main_cap(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "msg_test", "type": "message", "role": "assistant", "model": "summary",
                "content": [{"type": "text", "text": "摘要"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 2},
            })

        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            sdk = anthropic.Anthropic(api_key="test", base_url="https://example.test/anthropic", http_client=http)
            client = AnthropicModelClient(sdk_client=sdk, request_timeout=45)
            self.assertEqual(self.manager._model_summary([], client=client), "摘要")
            client.create_message(model="main", system="", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=8000)
        self.assertNotIn("max_tokens", requests[0])
        self.assertEqual(requests[1]["max_tokens"], 8000)


if __name__ == "__main__":
    unittest.main()
