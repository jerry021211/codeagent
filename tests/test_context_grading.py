from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from evals.agent_adapter import append_record
from evals.context_suite.grading import grade_answer
from evals.context_suite.materials import definitions
from evals.context_suite.regrade import regrade
from evals.context_suite.runner import run_suite
from evals.evidence import hashes, read_json, seal, verify_seal, write_json
from evals.metrics import request_usage


class ContextGradingTests(unittest.TestCase):
    def test_format_case_equivalence_does_not_relax_other_constraints(self):
        case = next(c for c in definitions() if c["id"] == "S03")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "answer.json"
            for format_value in ("json", "Json", "JSON"):
                answer = {**case["expected"], "format": format_value}
                write_json(path, answer)
                result = grade_answer(path, case)
                self.assertTrue(result["passed"])
                self.assertEqual(result["answer"]["format"], format_value)
            wrong_answers = [
                {**case["expected"], "format": "CSV"},
                {**case["expected"], "format": "JSON "},
                {**case["expected"], "fields": ["金额", "姓名"]},
                {**case["expected"], "overwrite": 0},
                {**case["expected"], "overwrite": "false"},
                {**case["expected"], "extra": "unused"},
            ]
            wrong_key = deepcopy(case["expected"])
            wrong_key["FORMAT"] = wrong_key.pop("format")
            for value in [*wrong_answers, wrong_key]:
                write_json(path, value)
                self.assertFalse(grade_answer(path, case)["passed"], value)

    def test_paths_symbols_and_other_case_ids_still_require_exact_values(self):
        case = next(c for c in definitions() if c["id"] == "S02")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "answer.json"
            for field in ("entry_file", "entry_symbol"):
                write_json(path, {**case["expected"], field: case["expected"][field].upper()})
                self.assertFalse(grade_answer(path, case)["passed"])
            gold = {"id": "unrelated", "expected": {"format": "JSON"}}
            write_json(path, {"format": "json"})
            self.assertFalse(grade_answer(path, gold)["passed"])

    def test_forwarded_usage_is_complete_even_when_response_hits_output_limit(self):
        requests = [{"request_index": 1}, {"request_index": 2}]
        responses = [{"request_index": i, "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 32}} for i in (1, 2)]
        self.assertTrue(request_usage(requests, responses)["usage_complete"])
        self.assertFalse(request_usage(requests, responses, offline=True)["usage_complete"])
        self.assertFalse(request_usage([], [])["usage_complete"])

    def test_real_missing_error_and_duplicate_usage_remain_incomplete(self):
        request = [{"request_index": 1}]
        valid = {"request_index": 1, "usage": {"input_tokens": 10, "output_tokens": 2}}
        for responses in ([], [valid, valid], [{"request_index": 1, "error_type": "ConnectionError"}],
                          [{"request_index": 1, "usage": {"output_tokens": 2}}],
                          [{"request_index": 1, "usage": {"input_tokens": -1, "output_tokens": 2}}],
                          [{**valid, "request_index": 2}]):
            self.assertFalse(request_usage(request, responses)["usage_complete"], responses)

    def test_exhausted_sdk_cap_stops_without_network_style_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = run_suite(output=Path(temp), cases=["S03"], variants=["D"], scale="stress", max_api_calls=1)
            result = read_json(root / "result.json")["results"][0]
            self.assertEqual(result["failure_codes"][0], "api_call_limit")
            self.assertEqual(result["execution"]["stop_reason"], "budget_exceeded:evaluation_api_calls")
            self.assertEqual(result["metrics"]["api_requests"], 1)
            self.assertEqual(result["metrics"]["locally_blocked_calls"], 1)
            self.assertIn("没有生成 answer.json", (root / "report.md").read_text(encoding="utf-8"))

    def test_regrade_corrects_old_scores_without_mutating_sealed_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            trial = source / "trial-001-S03-A-r1"
            workspace = trial / "workspace"
            workspace.mkdir(parents=True)
            case = next(c for c in definitions() if c["id"] == "S03")
            write_json(source / "suite.json", {"mode": "live", "scale": "stress"})
            write_json(trial / "manifest.json", {"case_id": "S03", "variant": "A", "repeat": 1, "scale": "stress", "initial_files": {}, "profile": {"mode": "live"}})
            write_json(trial / "gold.json", {**case, "requires_archive_evidence": False})
            write_json(workspace / "answer.json", {**case["expected"], "format": "json"})
            write_json(trial / "execution.json", {"execution_status": "completed", "canonical_prefix_unchanged": True, "worker_wall_ms": 100})
            write_json(trial / "result.json", {"task_success": False, "metrics": {"usage_complete": False}})
            append_record(trial / "events.jsonl", {"type": "model.failed", "payload": {"call_id": "blocked", "error_type": "EvaluationCallLimit"}})
            append_record(trial / "model-requests.jsonl", {"request_index": 1, "system": "main", "messages": []})
            append_record(trial / "model-responses.jsonl", {"request_index": 1, "usage": {"input_tokens": 10, "output_tokens": 2}})
            seal(source)
            before = hashes(source)
            out = regrade(source, output=root / "regraded")
            result = read_json(out / "result.json")
            self.assertTrue(result["all_passed"])
            self.assertEqual(result["new_model_calls"], 0)
            self.assertTrue(result["results"][0]["metrics"]["usage_complete"])
            self.assertEqual(result["results"][0]["metrics"]["locally_blocked_calls"], 1)
            self.assertEqual(hashes(source), before)
            self.assertTrue(verify_seal(source)["valid"])
            self.assertTrue(verify_seal(out)["valid"])
            with self.assertRaises(ValueError):
                regrade(source, output=source / "nested")
            write_json(workspace / "answer.json", {})
            with self.assertRaises(ValueError):
                regrade(source, output=root / "regraded")


if __name__ == "__main__":
    unittest.main()
