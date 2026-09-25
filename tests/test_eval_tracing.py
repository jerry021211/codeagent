from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evals.context_suite.runner import credentials
from evals.agent_adapter import EvidenceSDK, EvaluationCallLimit, EvaluationModelClient
from evals.evidence import read_json
from evals.tracing import configure_tracing, run_with_trace, tracing_settings


class EvaluationTracingTests(unittest.TestCase):
    def test_live_reads_dotenv_tracing_credentials_without_exposing_secrets_in_settings(self):
        values = {"MODEL_ID": "test-model", "API_KEY": "model-test-key", "LANGSMITH_TRACING": "true",
                  "SUMMARIZATION_API_KEY": "distinct-summary-key",
                  "LANGSMITH_API_KEY": "tracing-test-key", "LANGSMITH_PROJECT": "CodeAgent",
                  "LANGSMITH_ENDPOINT": "https://tracing.example", "LANGSMITH_WORKSPACE_ID": "workspace-test"}
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.dotenv_values", return_value=values):
            env = credentials("live")
        self.assertEqual(env["LANGSMITH_API_KEY"], "tracing-test-key")
        self.assertEqual(env["SUMMARIZATION_API_KEY"], "distinct-summary-key")
        self.assertEqual(env["LANGSMITH_WORKSPACE_ID"], "workspace-test")
        self.assertEqual(env["LANGSMITH_ENDPOINT"], "https://tracing.example")
        self.assertEqual(tracing_settings(env), {"enabled": True, "project": "CodeAgent"})

    def test_environment_overrides_dotenv_even_across_legacy_aliases(self):
        original = {"LANGCHAIN_TRACING_V2": "false", "LANGCHAIN_PROJECT": "process-project", "LANGCHAIN_API_KEY": "process-key"}
        env = configure_tracing(original, mode="live", dotenv={"LANGSMITH_TRACING": "true", "LANGSMITH_PROJECT": "file-project", "LANGSMITH_API_KEY": "file-key"})
        self.assertEqual(tracing_settings(env), {"enabled": False, "project": "process-project"})
        self.assertEqual(env["LANGSMITH_API_KEY"], "process-key")
        self.assertNotIn("LANGSMITH_TRACING", original)

    def test_legacy_dotenv_settings_are_supported(self):
        env = configure_tracing({}, mode="live", dotenv={"LANGCHAIN_TRACING_V2": "true", "LANGCHAIN_PROJECT": "legacy", "LANGCHAIN_API_KEY": "key"})
        self.assertEqual(tracing_settings(env), {"enabled": True, "project": "legacy"})
        self.assertEqual(env["LANGSMITH_API_KEY"], "key")

    def test_offline_switches_stay_disabled_despite_enabled_parent(self):
        env = configure_tracing({"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true"}, mode="offline")
        self.assertFalse(tracing_settings(env)["enabled"])
        self.assertEqual(env["LANGCHAIN_TRACING_V2"], "false")

    def test_live_without_tracing_configuration_does_not_opt_in(self):
        self.assertFalse(tracing_settings(configure_tracing({}, mode="live"))["enabled"])

    def test_separate_summary_client_shares_evidence_budget_and_is_closed(self):
        import json

        def sdk():
            value = Mock()
            value.with_options.return_value = value
            value.messages.create.return_value = SimpleNamespace(content=[{"type": "text", "text": "ok"}], usage=None, stop_reason="end_turn")
            return value

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}):
            main_sdk, summary_sdk = sdk(), sdk()
            recorder = EvidenceSDK(main_sdk, Path(temp), max_calls=2)
            summary_recorder = recorder.for_client(summary_sdk)
            client = EvaluationModelClient(sdk_client=recorder, summary_sdk_client=summary_recorder)
            summary = client.fork(call_kind="context_summary")
            summary.request_timeout = 45
            args = {"model": "same-model-name", "system": "test", "messages": [{"role": "user", "content": "test"}], "tools": [], "max_tokens": 32}
            client.create_message(**args)
            summary.create_message(**args)
            self.assertEqual(main_sdk.messages.create.call_count, 1)
            self.assertEqual(summary_sdk.messages.create.call_count, 1)
            summary_sdk.with_options.assert_called_once_with(max_retries=0, timeout=45)
            with self.assertRaises(EvaluationCallLimit):
                summary.create_message(**args)
            requests = [json.loads(line) for line in (Path(temp) / "model-requests.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["request_index"] for r in requests], [1, 2])
            recorder.close()
            recorder.close()
            main_sdk.close.assert_called_once()
            summary_sdk.close.assert_called_once()

    def exercise_trace(self, *, failure=False, offline=False, flush_failure=False):
        order = []
        handle = Mock()
        handle._run = SimpleNamespace(id="test-run-id")

        @contextmanager
        def fake_trace(name, **kwargs):
            self.assertEqual(name, "eval.context.S03.D.r1")
            self.assertEqual(kwargs["metadata"]["variant"], "D")
            self.assertEqual(kwargs["metadata"]["case_id"], "S03")
            self.assertEqual(kwargs["metadata"]["scale"], "stress")
            self.assertEqual(kwargs["inputs"], {"prompt": "question"})
            order.append("enter")
            try:
                yield handle
            finally:
                order.append("exit")

        def run(prompt):
            order.append("agent")
            if offline:
                self.assertEqual(os.environ["LANGSMITH_TRACING"], "false")
                self.assertEqual(os.environ["LANGCHAIN_TRACING_V2"], "false")
            if failure:
                raise ValueError("task failed")
            return SimpleNamespace(stop_reason="end_turn", iterations=2)

        def flush():
            order.append("flush")
            if flush_failure:
                raise OSError("private connection detail")

        agent = SimpleNamespace(run=run)
        spec = {"profile": {"mode": "offline" if offline else "live"}, "case_id": "S03", "variant": "D", "repeat": 1, "scale": "stress"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "LANGSMITH_PROJECT": "CodeAgent"}, clear=True):
            trial = Path(temp)
            with patch("evals.tracing.trace_run", fake_trace), patch("evals.tracing.flush_traces", side_effect=flush):
                if failure:
                    with self.assertRaisesRegex(ValueError, "task failed"):
                        run_with_trace(agent, "question", trial=trial, spec=spec)
                else:
                    result = run_with_trace(agent, "question", trial=trial, spec=spec)
                    self.assertEqual(result.iterations, 2)
            status = read_json(trial / "tracing.json")
        return order, status

    def test_trial_label_and_flush_after_trace_closes(self):
        order, status = self.exercise_trace()
        self.assertEqual(order, ["enter", "agent", "exit", "flush"])
        self.assertEqual(status["run_id"], "test-run-id")
        self.assertEqual(status["project"], "CodeAgent")
        self.assertEqual(status["flush_status"], "returned")
        self.assertFalse(status["delivery_confirmed"])

    def test_task_exception_is_preserved_and_traces_still_flush(self):
        order, status = self.exercise_trace(failure=True)
        self.assertEqual(order, ["enter", "agent", "exit", "flush"])
        self.assertEqual(status["flush_status"], "returned")

    def test_upload_failure_does_not_break_task_or_leak_error_details(self):
        order, status = self.exercise_trace(flush_failure=True)
        self.assertEqual(status["flush_error_type"], "OSError")
        self.assertNotIn("private connection detail", str(status))

    def test_direct_offline_worker_cannot_upload(self):
        order, status = self.exercise_trace(offline=True)
        self.assertEqual(order, ["agent"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["flush_status"], "disabled")


if __name__ == "__main__":
    unittest.main()
