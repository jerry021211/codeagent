from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals.evidence import read_json, seal, verify_seal
from evals.metrics import summarize
from evals.runner import run
from evals.task import TARGET, grade, prepare, validate


class EvaluationTests(unittest.TestCase):
    def test_seed_fails_gold_passes_and_regressions_survive(self):
        with tempfile.TemporaryDirectory() as temp:
            result = validate(Path(temp) / "validation")
            self.assertTrue(result["valid"])
            self.assertFalse(result["seed_passed"])
            self.assertFalse(result["empty_patch_passed"])
            self.assertTrue(result["gold_passed"])

    def test_syntax_error_is_candidate_failure_not_invalid_trial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare(root / "workspace")
            (root / "workspace" / TARGET).write_text("not valid python !!!", encoding="utf-8")
            result = grade(root / "workspace", root / "grader.json")
            self.assertFalse(result["passed"])
            self.assertIn("candidate_error", result)
            self.assertNotIn("invalid_reason", result)

    def test_usage_dedupes_call_and_ignores_completed_copy(self):
        payload = {"call_id": "one", "call_kind": "main", "available": True, "input_tokens": 10, "output_tokens": 2}
        events = [{"type": "model.started", "payload": payload}, {"type": "model.completed", "payload": {"call_id": "one", "usage": payload}}, {"type": "usage.updated", "payload": payload}, {"type": "usage.updated", "payload": payload}]
        result = summarize(events)
        self.assertEqual(result["logical_model_calls"], 1)
        self.assertEqual(result["usage_by_kind"]["main"]["input_tokens"], 10)
        self.assertTrue(result["usage_complete"])
        events.append({"type": "model.failed", "payload": {"call_id": "failed"}})
        self.assertFalse(summarize(events)["usage_complete"])

    def test_started_without_response_is_incomplete_usage(self):
        result = summarize([{"type": "model.started", "payload": {"call_id": "interrupted"}}])
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["cost"])

    def test_checksums_detect_mutation_and_added_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "evidence.txt").write_text("original")
            seal(root)
            self.assertTrue(verify_seal(root)["valid"])
            (root / "evidence.txt").write_text("changed")
            (root / "added.txt").write_text("new")
            result = verify_seal(root)
            self.assertFalse(result["valid"])
            self.assertEqual(result["changed"], ["evidence.txt"])
            self.assertEqual(result["added"], ["added.txt"])

    def test_offline_end_to_end_keeps_engine_separate_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            experiment = run(mode="offline", output=Path(temp))
            result = read_json(experiment / "result.json")
            self.assertTrue(result["task_success"], result)
            self.assertFalse(result["quality_measurement"])
            self.assertFalse(result["metrics"]["usage_complete"])
            trial = experiment / "trial-001"
            runtime = read_json(trial / "worker-runtime.json")
            self.assertIn("engine-snapshot", runtime["engine_package"])
            grader = read_json(trial / "grader.json")
            self.assertIn("verification-workspace", grader["imported_module"])
            self.assertEqual(len(grader["checks"]), 11)
            self.assertEqual(result["metrics"]["logical_model_calls"], 3)
            schemas = read_json(trial / "tool-schemas.json")
            self.assertFalse({"bash", "subagent", "ask_user"} & {s["name"] for s in schemas})
            for filename in ("events.jsonl", "model-requests.jsonl", "model-responses.jsonl", "patch.diff", "runtime/state.db"):
                self.assertTrue((trial / filename).exists(), filename)
            self.assertTrue(verify_seal(experiment)["valid"])

    def test_timeout_keeps_failure_result_and_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            experiment = run(mode="offline", output=Path(temp), timeout=0.001)
            result = read_json(experiment / "result.json")
            self.assertEqual(result["execution_status"], "timeout")
            self.assertFalse(result["task_success"])
            self.assertTrue(verify_seal(experiment)["valid"])


if __name__ == "__main__":
    unittest.main()
