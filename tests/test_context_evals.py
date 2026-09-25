from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evals.agent_adapter import EvidenceSDK, EvaluationCallLimit, append_record
from evals.context_suite.grading import grade_answer
from evals.context_suite.materials import definitions, material, prepare
from evals.context_suite.runner import run_suite, validate_materials
from evals.evidence import read_json, verify_seal


class ContextEvaluationTests(unittest.TestCase):
    def test_long_evidence_preserves_structure_and_redacts_credentials_and_reasoning(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "requests.jsonl"
            values = [{"role": "user", "content": f"message-{i}"} for i in range(205)]
            values[-1]["content"] = [{"type": "text", "text": "super-private-value"}, {"type": "thinking", "thinking": "hidden"}]
            deep = {"value": "last"}
            for _ in range(12):
                deep = {"nested": deep}
            with patch.dict("os.environ", {"API_KEY": "super-private-value"}):
                append_record(path, {"messages": values, "nested": deep, "api_key": "another-private-value"})
            text = path.read_text(encoding="utf-8")
            result = json.loads(text)
            self.assertEqual(len(result["messages"]), 205)
            self.assertIn("last", text)
            self.assertNotIn("truncated", text)
            self.assertNotIn("super-private-value", text)
            self.assertNotIn("another-private-value", text)
            self.assertNotIn("hidden", text)
            self.assertEqual(len(result["messages"][-1]["content"]), 1)

    def test_sdk_option_clones_share_request_ids_and_hard_call_limit(self):
        class SDK:
            def __init__(self):
                self.messages = self
                self.options = []
                self.calls = 0

            def with_options(self, **kwargs):
                self.options.append(kwargs)
                return self

            def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(content=[], usage=None, stop_reason="end_turn")

            def post(self, path, *, body, cast_to):
                return self.create(**body)

        with tempfile.TemporaryDirectory() as temp:
            sdk = SDK()
            wrapper = EvidenceSDK(sdk, Path(temp), max_calls=2)
            fork = wrapper.with_options(timeout=45, max_retries=0)
            fork.post("/v1/messages", body={"model": "summary"}, cast_to=object)
            wrapper.messages.create(model="main")
            with self.assertRaises(EvaluationCallLimit):
                fork.post("/v1/messages", body={"model": "retry"}, cast_to=object)
            self.assertEqual(sdk.calls, 2)
            self.assertEqual(wrapper.count, fork.count)
            rows = [json.loads(line) for line in (Path(temp) / "model-requests.jsonl").read_text().splitlines()]
            self.assertEqual([r["request_index"] for r in rows], [1, 2])
            self.assertEqual(sdk.options, [{"timeout": 45, "max_retries": 0}])

    def test_material_grader_positive_and_negative_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = validate_materials(Path(temp))
            result = read_json(root / "result.json")
            self.assertTrue(result["valid"])
            self.assertEqual(len(result["checks"]), 12)
            self.assertTrue(verify_seal(root)["valid"])

    def test_bool_zero_duplicate_keys_and_extra_fields_do_not_pass(self):
        gold = {"expected": {"overwrite": False}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "answer.json"
            for text in ('{"overwrite": 0}', '{"overwrite": true, "overwrite": false}', '{"overwrite": false, "extra": 1}'):
                path.write_text(text)
                self.assertFalse(grade_answer(path, gold)["passed"], text)
            path.write_text('{"overwrite": false}')
            self.assertTrue(grade_answer(path, gold)["passed"])

    def test_materials_are_deterministic_have_no_initial_summary_and_keep_gold_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "materials"
            self.assertEqual(len(prepare(root)), 6)
            with self.assertRaises(FileExistsError):
                prepare(root)
            for case in definitions():
                seed, gold = material(case, "production")
                self.assertEqual(seed, material(case, "production")[0])
                self.assertEqual(seed["initial_context_state"], {})
                self.assertNotIn("answer.json", seed["workspace_files"])
                self.assertFalse((root / case["id"] / "workspace/gold.json").exists())
                if case["recipe"] in {"revision", "status", "handoff"}:
                    tail = str(seed["messages"][-12:]) + seed["prompt"]
                    self.assertFalse(any(m in tail for m in gold["fact_markers"]))

    def test_planned_trial_and_global_call_caps_reject_before_creating_experiment(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                run_suite(output=Path(temp), max_trials=1)
            with self.assertRaises(ValueError):
                run_suite(output=Path(temp), max_total_api_calls=1)
            with self.assertRaises(ValueError):
                run_suite(output=Path(temp), context_window_tokens=-1)
            for invalid in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    run_suite(output=Path(temp), max_total_tokens=invalid)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_explicit_token_budget_reaches_agent_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_suite(output=Path(temp), cases=["S01"], variants=["A"], max_total_tokens=300000)
            result = read_json(root / "result.json")
            self.assertTrue(result["all_passed"])
            trial = root / result["results"][0]["trial_directory"]
            self.assertEqual(read_json(trial / "effective-loop-guard.json")["max_total_tokens"], 300000)
            self.assertIn("每场累计 token 预算：300,000 token", (root / "report.md").read_text(encoding="utf-8"))

    def test_offline_ablation_really_triggers_projection_summaries_and_recall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_suite(output=Path(temp), cases=["S02", "S03", "S04"], variants=list("ABCD"), scale="stress")
            result = read_json(root / "result.json")
            self.assertTrue(result["all_passed"], result)
            self.assertFalse(result["quality_measurement"])
            self.assertTrue(verify_seal(root)["valid"])
            rows = {(r["case_id"], r["variant"]): r for r in result["results"]}
            report = (root / "report.md").read_text(encoding="utf-8")
            self.assertIn("模拟调用总数", report)
            self.assertIn("每场累计 token 预算：不设上限", report)
            self.assertEqual(read_json(root / "suite.json")["max_total_tokens_per_trial"], 0)
            self.assertNotIn("API 请求总数", report)
            for key, row in rows.items():
                m = row["metrics"]
                if key[1] in "AB":
                    self.assertEqual(m["summary_api_requests"], 0)
                if key[0] == "S03" and key[1] in "CD":
                    self.assertGreater(m["summary_api_requests"], 0)
                if key[0] == "S02" and key[1] in "BD":
                    self.assertGreater(m["first_projection_placeholders"], 0)
                if key[0] == "S04":
                    self.assertTrue(m["archive_evidence_observed"])
                self.assertFalse(m["usage_complete"])
                self.assertIsNone(m["cost"])
                path = root / row["trial_directory"]
                self.assertEqual(read_json(path / "manifest.json")["profile"]["max_total_tokens"], 0)
                guard = read_json(path / "effective-loop-guard.json")
                self.assertEqual(guard["max_total_tokens"], 0)
                self.assertGreater(guard["max_model_calls"], 0)
                self.assertIn("engine-snapshot", read_json(path / "worker-runtime.json")["engine_package"])
                actual = read_json(path / "effective-context.json")
                trace = read_json(path / "tracing.json")
                self.assertFalse(trace["enabled"])
                self.assertEqual(trace["flush_status"], "disabled")
                self.assertEqual(actual["tool_projection_enabled"], key[1] in "BD")
                self.assertEqual(actual["mode"], "off" if key[1] in "AB" else "model")
                self.assertTrue(row["execution"]["canonical_prefix_unchanged"])
                if key[0] == "S03" and key[1] in "CD":
                    self.assertTrue(all(not marker["in_first_retained_view"] for marker in row["fact_exposure_audit"]))

    def test_timeout_is_failed_and_evidence_is_still_sealed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_suite(output=Path(temp), cases=["S01"], variants=["A"], timeout=0.001)
            result = read_json(root / "result.json")
            self.assertFalse(result["all_passed"])
            self.assertEqual(result["results"][0]["execution"]["execution_status"], "timeout")
            self.assertTrue(verify_seal(root)["valid"])


if __name__ == "__main__":
    unittest.main()
