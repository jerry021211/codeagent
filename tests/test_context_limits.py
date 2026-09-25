"""Production context boundaries with oversized evidence and restored state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, EnvironmentConfig, ModelResponse, RecoveryConfig, RecoveryRuntime, ToolRegistry
from codeagent.context import ContextCompactionError, ContextConfig, ContextManager, RuntimeState
from codeagent.context.budget import inspect_request
from codeagent.messages import validate_tool_history


def _response(text="done"):
    return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": text}])


def _rounds(count=10, *, kind="probe", start=0):
    messages = [{"role": "user", "content": "Keep current API and continue the verified task."}]
    for index in range(start, start + count):
        calls, receipts = [], []
        for suffix in ("a", "b") if kind == "read_file" else ("a",):
            call_id = f"round-{index}-{suffix}"
            path = f"D:/project/模块-{index}-{suffix}.py"
            arguments = {"file_path": path}
            if kind == "write_file":
                arguments["content"] = "CODE START\n" + "x" * 150_000 + "\nCODE END"
                content = ("ERROR START\n" + "failure-log" * 1500 + "\nFAILED public_api; ERROR E129; exit=2"
                           if index == 0 else f"Wrote 3 lines to {path}")
            elif kind == "read_file":
                # Actual built-in full-file shape: one numbered line without a
                # partial-read footer. Both 80k results are exempt from clearing.
                content = "1\t" + "x" * 79_998
            else:
                content = f"completed step {index}"
            calls.append({"type": "tool_use", "id": call_id, "name": kind, "input": arguments})
            receipt = {"type": "tool_result", "tool_use_id": call_id, "content": content}
            if kind == "write_file" and index == 0:
                receipt.update(is_error=True, status="error", exit_code=2)
            receipts.append(receipt)
        messages.extend([{"role": "assistant", "content": calls}, {"role": "user", "content": receipts}])
    return messages


class CapturingClient:
    def __init__(self, summaries=("Validated summary; keep current task.",)):
        self.summaries = list(summaries)
        self.calls = []

    def fork(self, **_kwargs):
        return self

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if kwargs["model"].startswith("summary"):
            value = self.summaries.pop(0)
            if isinstance(value, Exception):
                raise value
            return _response(value)
        return _response()

    @property
    def summary_calls(self):
        return [item for item in self.calls if item["model"].startswith("summary")]

    @property
    def main_calls(self):
        return [item for item in self.calls if not item["model"].startswith("summary")]


def _new_summary_messages(call):
    text = call["messages"][0]["content"]
    return json.loads(text.split("<new-messages>\n", 1)[1].split("\n</new-messages>", 1)[0])


class ContextLimitsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def manager(self, **options):
        manager = ContextManager(config=ContextConfig(**{
            "compact_threshold_chars": 1000, "summarization_model": "summary", "transcript_dir": self.root / "transcripts",
            "tool_output_dir": self.root / "outputs", **options,
        }))
        manager.begin_turn(0)
        return manager

    def agent(self, manager, client, messages):
        return Agent(
            client=client, config=AgentConfig(model="main", max_tokens=1000),
            tools=ToolRegistry(), context=manager, messages=messages,
            allow_subagents=False,
            recovery_runtime=RecoveryRuntime(RecoveryConfig(sleep_enabled=False, max_retries=0)),
        )

    def test_default_summary_budget_recovers_huge_write_rounds_with_failure_evidence(self):
        history = _rounds(kind="write_file")
        original = deepcopy(history)
        manager, client = self.manager(), CapturingClient()
        agent = self.agent(manager, client, history)

        result = agent.run()

        self.assertEqual(result.final_text, "done")
        self.assertEqual(len(client.summary_calls), 1)
        self.assertTrue(manager.last_compaction["source_previews_used"])
        self.assertGreater(manager.state.compacted_message_count, 0)
        self.assertLessEqual(inspect_request(**client.summary_calls[0]).request_chars, 120_000)
        source = _new_summary_messages(client.summary_calls[0])
        call, receipt = source[1]["content"][0], source[2]["content"][0]
        self.assertEqual(call["input"]["file_path"], "D:/project/模块-0-a.py")
        self.assertTrue(call["input"]["content"]["truncated"])
        self.assertEqual((receipt["is_error"], receipt["status"], receipt["exit_code"]), (True, "error", 2))
        self.assertIn("FAILED public_api; ERROR E129; exit=2", receipt["content"]["preview"])
        self.assertEqual(agent.messages[:len(original)], original)
        validate_tool_history(agent.messages)
        validate_tool_history(client.main_calls[0]["messages"])
        self.assertTrue(Path(manager.state.summary_transcript).is_file())

    def test_parallel_complete_80k_reads_no_longer_block_every_summary_batch(self):
        history = _rounds(kind="read_file")
        original = deepcopy(history)
        manager, client = self.manager(), CapturingClient()
        result = self.agent(manager, client, history).run()
        self.assertEqual(result.final_text, "done")
        self.assertTrue(manager.last_compaction["source_previews_used"])
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(manager.state.compacted_message_count, 17)
        self.assertLessEqual(inspect_request(**client.summary_calls[0]).request_chars, 120_000)
        self.assertLessEqual(inspect_request(**client.main_calls[0]).request_chars, 600_000)
        source = _new_summary_messages(client.summary_calls[0])
        self.assertEqual([block["id"] for block in source[1]["content"]], ["round-0-a", "round-0-b"])
        self.assertEqual([block["tool_use_id"] for block in source[2]["content"]], ["round-0-a", "round-0-b"])
        self.assertTrue(all(block["content"]["truncated"] for block in source[2]["content"]))
        self.assertEqual(history[:len(original)], original)

    def test_full_source_mode_removes_reasoning_and_media_payload_before_summary(self):
        history = _rounds()
        history[1]["content"].insert(0, {"type": "thinking", "thinking": "DO-NOT-SUMMARIZE-THOUGHT", "signature": "PRIVATE-SIGNATURE"})
        history[2]["content"][0]["content"] = [
            {"type": "text", "text": "visible exact tool evidence"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "DO-NOT-INLINE-PIXELS" * 20}},
        ]
        original = deepcopy(history)
        manager, client = self.manager(), CapturingClient()
        self.agent(manager, client, history).run()
        self.assertFalse(manager.last_compaction["source_previews_used"])
        payload = client.summary_calls[0]["messages"][0]["content"]
        for private in ("DO-NOT-SUMMARIZE-THOUGHT", "PRIVATE-SIGNATURE", "DO-NOT-INLINE-PIXELS"):
            self.assertNotIn(private, payload)
        self.assertIn("visible exact tool evidence", payload)
        self.assertIn("provider_reasoning_not_summarized", payload)
        self.assertIn("media_payload_not_in_summary", payload)
        self.assertEqual(history[:len(original)], original)

    def test_failure_cooldown_restores_from_checkpoint_and_uses_monotonic_boundary(self):
        clock = {"mono": 10.0, "wall": 1000.0}
        history, manager = _rounds(), self.manager()
        client = CapturingClient((RuntimeError("temporary unavailable"), "recovered"))
        with patch("codeagent.context.manager.time.monotonic", side_effect=lambda: clock["mono"]), patch("codeagent.context.manager.time.time", side_effect=lambda: clock["wall"]):
            with self.assertRaises(ContextCompactionError):
                manager.force_compact(history, client=client)
            state = asdict(manager.state)
            self.assertEqual(state["summary_retry_after_epoch"], 1090.0)
            clock.update(mono=500.0, wall=1030.0)
            restored = ContextManager(config=manager.config, state=RuntimeState(**state))
            restored.force_compact(history, client=client)
            self.assertEqual(len(client.summary_calls), 1)
            self.assertEqual(restored.last_compaction["reason"], "failure_cooldown")
            # A wall-clock jump after restoration must not end active cooldown.
            clock.update(mono=559.999, wall=999_999.0)
            restored.force_compact(history, client=client)
            self.assertEqual(len(client.summary_calls), 1)
            clock["mono"] = 560.0
            restored.force_compact(history, client=client)
            self.assertEqual(len(client.summary_calls), 2)
            self.assertEqual(restored.state.summary_text, "recovered")
            self.assertEqual(restored.state.summary_retry_after_epoch, 0.0)

    def test_model_explicit_key_and_owner_scope_changes_release_old_cooldown(self):
        for changed in ("model", "key", "scope"):
            with self.subTest(changed=changed):
                history, manager = _rounds(), self.manager()
                manager.summary_credentials_scope = "owner-old"
                client = CapturingClient((RuntimeError("old allowance refusal"), "new config succeeds"))
                with patch("codeagent.context.manager.time.monotonic", return_value=10.0), patch("codeagent.context.manager.time.time", return_value=1000.0):
                    with self.assertRaises(ContextCompactionError):
                        manager.force_compact(history, client=client)
                    if changed == "model":
                        manager.config.summarization_model = "summary-next"
                    elif changed == "key":
                        manager.config.summarization_api_key = "different-authorized-key"
                    else:
                        manager.summary_credentials_scope = "owner-next"
                    manager.force_compact(history, client=client)
                self.assertEqual(len(client.summary_calls), 2)
                self.assertEqual(manager.state.summary_text, "new config succeeds")

    def test_failed_archive_write_keeps_previous_summary_cursor_and_transcripts(self):
        history, manager = _rounds(), self.manager()
        client = CapturingClient(("first stable summary", "must not commit"))
        manager.force_compact(history, client=client)
        history.extend(_rounds(8, start=10)[1:])
        before, original = asdict(manager.state), deepcopy(history)
        with patch.object(manager, "write_transcript", side_effect=OSError("disk full")):
            with self.assertRaises(ContextCompactionError):
                manager.force_compact(history, client=client)
        for key, value in before.items():
            if key not in {"summary_retry_after_epoch", "summary_failure_scope"}:
                self.assertEqual(getattr(manager.state, key), value, key)
        self.assertEqual(history, original)
        self.assertEqual(manager.last_compaction["status"], "failed")
        self.assertGreater(manager.state.summary_retry_after_epoch, 0)

    def test_repeated_prepare_with_no_new_fold_does_not_open_summary_client(self):
        history, manager, client = _rounds(), self.manager(), CapturingClient()
        first = manager.prepare_before_model_call(history, client=client)
        state = asdict(manager.state)
        factory_calls = []

        def must_stay_lazy():
            factory_calls.append(True)
            raise AssertionError("no new source should not create a summary client")

        for _ in range(5):
            self.assertEqual(manager.prepare_before_model_call(history, client=must_stay_lazy), first)
        self.assertEqual(manager.force_compact(history, client=must_stay_lazy), first)
        self.assertEqual(factory_calls, [])
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(asdict(manager.state), state)

    def test_folded_runtime_note_is_not_re_pinned_but_real_user_steering_is(self):
        manager, client = self.manager(), CapturingClient(("first", "second"))
        agent = self.agent(manager, client, _rounds(4))
        runtime_text = "INTERNAL REMINDER: preserve already recorded state"
        steering = "User correction: preserve literal api_v2 and do not remove tests."
        agent.add_user_message(runtime_text, source="runtime")
        agent.add_user_message(steering)
        agent.messages.extend(_rounds(6, start=4)[1:])
        agent.run()
        agent.messages.extend(_rounds(8, start=10)[1:])
        agent.run()
        self.assertEqual(len(client.summary_calls), 2)
        self.assertTrue(any(item.get("_context_source") == "runtime" for item in agent.messages))
        for call in client.main_calls:
            sent = call["messages"]
            self.assertNotIn(runtime_text, str(sent))
            self.assertEqual(sum(message.get("content") == steering for message in sent), 1)
            self.assertTrue(all("_context_source" not in message for message in sent))
            validate_tool_history(sent)

    def test_deployment_environment_loads_new_context_settings(self):
        environment = {
            "MODEL_ID": "main", "SUMMARIZATION_MODEL_ID": "summary",
            "CONTEXT_RECENCY_MESSAGES": "14", "CONTEXT_RECENCY_ROUNDS": "3",
            "CONTEXT_MIN_FOLD_MESSAGES": "6", "CONTEXT_MESSAGE_TRIGGER_MIN_FOLD": "18",
            "CONTEXT_ROUND_TRIGGER_MIN_FOLD": "9", "CONTEXT_MAX_FOLD_MESSAGES": "160",
            "CONTEXT_MAX_FOLD_ROUNDS": "11", "CONTEXT_MAX_REQUEST_CHARS": "550000",
            "CONTEXT_SUMMARY_INPUT_MAX_CHARS": "110000", "CONTEXT_WINDOW_TOKENS": "99000",
            "CONTEXT_SUMMARY_WINDOW_TOKENS": "55000", "CONTEXT_FAILURE_COOLDOWN_SECONDS": "25.5",
            "CONTEXT_SUMMARY_TIMEOUT_SECONDS": "30.5", "CONTEXT_TOOL_PROJECTION_ENABLED": "false",
            "CONTEXT_INVESTIGATION_KEEP_ROUNDS": "0", "CONTEXT_COMMAND_KEEP_ROUNDS": "2",
            "CONTEXT_WRITE_KEEP_ROUNDS": "2", "CONTEXT_TOOL_CLEAR_MIN_CHARS": "1500",
            "CONTEXT_WRITE_CLEAR_MIN_CHARS": "450", "CONTEXT_SUMMARY_TEXT_PREVIEW_CHARS": "3000",
            "CONTEXT_SUMMARY_ARGUMENT_PREVIEW_CHARS": "1500", "CONTEXT_NEAR_CONTEXT_RATIO": "0.75",
            "CONTEXT_MODEL_WINDOWS_JSON": '{"main": 100000, "fallback": 24000}',
        }
        with patch.dict(os.environ, environment, clear=True), patch("codeagent.config._load_dotenv"):
            config = EnvironmentConfig.from_env().context_config
        expected = {
            "recency_messages": 14, "recency_rounds": 3, "min_fold_messages": 6,
            "message_trigger_min_fold": 18, "round_trigger_min_fold": 9, "max_fold_messages": 160,
            "max_fold_rounds": 11, "max_request_chars": 550000, "summary_input_max_chars": 110000,
            "context_window_tokens": 99000, "summary_context_window_tokens": 55000,
            "failure_cooldown_seconds": 25.5, "summary_timeout_seconds": 30.5,
            "tool_projection_enabled": False, "investigation_keep_rounds": 0,
            "command_keep_rounds": 2, "write_keep_rounds": 2, "tool_clear_min_chars": 1500,
            "write_clear_min_chars": 450, "summary_text_preview_chars": 3000,
            "summary_argument_preview_chars": 1500, "near_context_ratio": 0.75,
        }
        for key, value in expected.items():
            self.assertEqual(getattr(config, key), value, key)
        self.assertEqual(config.window_for_model("main"), 100000)
        self.assertEqual(config.window_for_model("fallback"), 24000)
        self.assertEqual(config.window_for_model("unmapped"), 99000)

    def test_invalid_deployment_context_values_fail_before_execution(self):
        cases = {
            "CONTEXT_RECENCY_MESSAGES": "-1", "CONTEXT_RECENCY_ROUNDS": "0",
            "CONTEXT_MAX_FOLD_ROUNDS": "-1", "CONTEXT_SUMMARY_INPUT_MAX_CHARS": "-1",
            "CONTEXT_MAX_REQUEST_CHARS": "0", "CONTEXT_WINDOW_TOKENS": "-2",
            "CONTEXT_SUMMARY_WINDOW_TOKENS": "-1", "CONTEXT_FAILURE_COOLDOWN_SECONDS": "-0.5",
            "CONTEXT_SUMMARY_TIMEOUT_SECONDS": "0", "CONTEXT_SUMMARY_TEXT_PREVIEW_CHARS": "-1",
            "CONTEXT_SUMMARY_ARGUMENT_PREVIEW_CHARS": "0", "CONTEXT_NEAR_CONTEXT_RATIO": "1.5",
            "CONTEXT_MODEL_WINDOWS_JSON": '{"main": -1}',
        }
        for key, value in cases.items():
            with self.subTest(key=key), patch.dict(os.environ, {"MODEL_ID": "main", "SUMMARIZATION_MODEL_ID": "summary", key: value}, clear=True), patch("codeagent.config._load_dotenv"):
                with self.assertRaises(ValueError):
                    EnvironmentConfig.from_env()
        for invalid in ("not-json", "[]", '{"main":true}'):
            with self.subTest(model_windows=invalid), patch.dict(os.environ, {"MODEL_ID": "main", "SUMMARIZATION_MODEL_ID": "summary", "CONTEXT_MODEL_WINDOWS_JSON": invalid}, clear=True), patch("codeagent.config._load_dotenv"):
                with self.assertRaises(ValueError):
                    EnvironmentConfig.from_env()


if __name__ == "__main__":
    unittest.main()
