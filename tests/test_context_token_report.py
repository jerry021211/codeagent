from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals.context_suite.runner import write_report
from evals.context_suite.token_report import build_token_report, token_report_lines, trial_tokens
from evals.evidence import read_json, write_json


SUMMARY_SYSTEM = "你是编程助手的上下文摘要器，只生成供后续继续工作的结构化 Markdown 摘要。"


def usage(input_tokens=100, output_tokens=10, cache_create=0, cache_read=40):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_create, "cache_read_input_tokens": cache_read}


class ContextTokenReportTests(unittest.TestCase):
    def test_different_token_budgets_are_not_comparable(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage()])
        for trial, limit in ((a, 300000), (d, 0)):
            path = self.root / trial["trial_directory"] / "manifest.json"
            manifest = read_json(path)
            manifest["profile"]["max_total_tokens"] = limit
            write_json(path, manifest)
        report = build_token_report(self.root, [a, d], "live")
        self.assertIsNone(report["comparison"]["reduction_fraction"])
        self.assertIn("max_total_tokens", str(report["paired_A_D"]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def trial(self, variant, usages, *, summary_indices=(), success=True, repeat=1):
        name = f"trial-S03-{variant}-r{repeat}"
        trial = self.root / name
        trial.mkdir()
        write_json(trial / "manifest.json", {
            "seed_history_sha256": "same-fixed-history", "engine_sha256": "same-engine", "scale": "stress",
            "profile": {"model": "model", "summary_model": "summary-model", "provider_host": "example.invalid", "max_iterations": 12,
                        "context": {"mode": "off" if variant == "A" else "model", "tool_projection_enabled": variant == "D",
                                    "compact_threshold_chars": 40000, "context_window_tokens": 0, "summary_context_window_tokens": 0},
                        "max_tokens": 2048, "max_api_calls": 16},
        })
        requests = [{"request_index": index, "system": SUMMARY_SYSTEM if index in summary_indices else "main"}
                    for index in range(1, len(usages) + 1)]
        responses = [{"request_index": index, "usage": value, "stop_reason": "end_turn"}
                     for index, value in enumerate(usages, 1)]
        self.records(trial / "model-requests.jsonl", requests)
        self.records(trial / "model-responses.jsonl", responses)
        return {"case_id": "S03", "variant": variant, "repeat": repeat, "task_success": success,
                "trial_directory": name, "failure_reasons": [] if success else ["答案错误"],
                "metrics": {"api_requests": len(usages), "summary_api_requests": len(summary_indices),
                            "first_main_request_chars": 100, "usage_complete": True,
                            "usage_by_kind": {"main": {"input_tokens": 999999}}, "duration_ms": 100,
                            "summary_successes": len(summary_indices), "locally_blocked_calls": 0, "recall_calls": 0}}

    @staticmethod
    def records(path, values):
        path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")

    def test_summary_and_cache_are_included_once_from_raw_sdk_evidence(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage(15, 5, 3, 2), usage(30, 4, 1, 20)], summary_indices=(1,))
        report = build_token_report(self.root, [a, d], "live")
        trial = report["trials"][1]
        self.assertEqual(trial["by_kind"]["context_summary"]["total_tokens"], 25)
        self.assertEqual(trial["by_kind"]["main"]["total_tokens"], 55)
        self.assertEqual(trial["totals"]["total_tokens"], 80)
        self.assertEqual(report["groups"][0]["totals"]["total_tokens"], 150)
        self.assertEqual(report["paired_A_D"][0]["D_minus_A_total_tokens"], -70)
        self.assertAlmostEqual(report["comparison"]["reduction_fraction"], 70 / 150)
        self.assertEqual(report["trials"][0]["by_kind"]["context_summary"]["total_tokens"], 0)

    def test_failed_task_and_truncated_summary_still_count_all_reported_usage(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage(50, 100), usage()], summary_indices=(1,), success=False)
        path = self.root / d["trial_directory"] / "model-responses.jsonl"
        responses = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        responses[0]["stop_reason"] = "max_tokens"
        self.records(path, responses)
        report = build_token_report(self.root, [a, d], "live")
        self.assertTrue(report["trials"][1]["complete"])
        self.assertEqual(report["groups"][1]["totals"]["total_tokens"], 340)
        self.assertEqual(report["groups"][1]["successes"], 0)
        self.assertLess(report["comparison"]["reduction_fraction"], 0)
        self.assertIn("含任务失败，减少不代表同等效果", "\n".join(token_report_lines(report)))

    def test_missing_cache_field_does_not_become_zero_or_saving(self):
        a = self.trial("A", [usage()])
        missing = usage(20, 4)
        del missing["cache_read_input_tokens"]
        d = self.trial("D", [missing])
        report = build_token_report(self.root, [a, d], "live")
        result = report["trials"][1]
        self.assertFalse(result["complete"])
        self.assertEqual(result["totals"]["input_tokens"], 20)
        self.assertIsNone(result["totals"]["cache_read_input_tokens"])
        self.assertIsNone(result["totals"]["total_tokens"])
        self.assertIsNone(report["comparison"]["reduction_fraction"])
        self.assertIn("未知", "\n".join(token_report_lines(report)))

    def test_missing_failed_request_response_keeps_total_unknown(self):
        d = self.trial("D", [usage(), None], summary_indices=(2,), success=False)
        path = self.root / d["trial_directory"] / "model-responses.jsonl"
        self.records(path, [{"request_index": 1, "usage": usage()},
                            {"request_index": 2, "error_type": "TimeoutError"}])
        result = trial_tokens(self.root, d, "live")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["totals"]["total_tokens"])
        self.assertEqual(result["by_kind"]["context_summary"]["requests"], 1)
        self.assertEqual(result["by_kind"]["main"]["total_tokens"], 150)

    def test_offline_does_not_present_synthetic_usage_as_real(self):
        results = [self.trial("A", [usage()]), self.trial("D", [usage(1, 1, 0, 0)])]
        report = build_token_report(self.root, results, "offline")
        for group in report["groups"]:
            self.assertFalse(group["complete"])
            self.assertTrue(all(value is None for value in group["totals"].values()))
        self.assertIsNone(report["comparison"]["reduction_fraction"])
        self.assertIn("离线模拟没有真实 token 用量", report["paired_A_D"][0]["reason"])

    def test_zero_baseline_has_no_percentage(self):
        a = self.trial("A", [usage(0, 0, 0, 0)])
        d = self.trial("D", [usage(0, 0, 0, 0)])
        report = build_token_report(self.root, [a, d], "live")
        self.assertTrue(report["paired_A_D"][0]["comparable"])
        self.assertEqual(report["comparison"]["A_total_tokens"], 0)
        self.assertIsNone(report["comparison"]["reduction_fraction"])
        self.assertIn("A 用量为 0", "\n".join(token_report_lines(report)))

    def test_pair_requires_matching_history_and_execution_conditions(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage(1, 1, 0, 0)])
        path = self.root / d["trial_directory"] / "manifest.json"
        manifest = read_json(path)
        manifest["seed_history_sha256"] = "different-history"
        write_json(path, manifest)
        report = build_token_report(self.root, [a, d], "live")
        self.assertFalse(report["paired_A_D"][0]["comparable"])
        self.assertIsNone(report["comparison"]["reduction_fraction"])
        self.assertIn("不一致", report["paired_A_D"][0]["reason"])

    def test_group_average_includes_failed_trials_without_filtering(self):
        results = [self.trial("A", [usage()], repeat=1), self.trial("D", [usage(30, 10, 0, 10)], repeat=1),
                   self.trial("A", [usage()], repeat=2), self.trial("D", [usage(30, 10, 0, 10)], repeat=2, success=False)]
        report = build_token_report(self.root, results, "live")
        d = report["groups"][1]
        self.assertEqual(d["trials"], 2)
        self.assertEqual(d["successes"], 1)
        self.assertEqual(d["totals"]["total_tokens"], 100)
        self.assertEqual(d["mean_per_trial"]["total_tokens"], 50)
        self.assertEqual(report["comparison"]["pairs"], 2)
        self.assertEqual(report["comparison"]["both_success_pairs"], 1)

    def test_common_context_and_summary_model_must_match_but_strategy_switches_may_differ(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage()])
        results = [a, d]
        self.assertTrue(build_token_report(self.root, results, "live")["paired_A_D"][0]["comparable"])
        path = self.root / d["trial_directory"] / "manifest.json"
        original = read_json(path)
        mutations = [("summary_model", "different-summary-model"), ("common_context.summary_context_window_tokens", 64000),
                     ("common_context.compact_threshold_chars", 80000), ("common_context.context_window_tokens", 128000)]
        for field, value in mutations:
            with self.subTest(field=field):
                manifest = json.loads(json.dumps(original))
                target = manifest["profile"]["context"] if field.startswith("common_context.") else manifest["profile"]
                target[field.split(".")[-1]] = value
                write_json(path, manifest)
                pair = build_token_report(self.root, results, "live")["paired_A_D"][0]
                self.assertFalse(pair["comparable"])
                self.assertIn(field, pair["reason"])

    def test_old_optional_fields_may_be_absent_but_recorded_differences_are_rejected(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage()])
        results = [a, d]
        old_pair = build_token_report(self.root, results, "live")["paired_A_D"][0]
        self.assertTrue(old_pair["comparable"])
        self.assertIn("sdk_retries", old_pair["unrecorded_optional_fields"])
        path = self.root / d["trial_directory"] / "manifest.json"
        original = read_json(path)
        for field in ("wall_timeout_seconds", "sdk_retries", "recovery_retries", "summary_max_tokens"):
            with self.subTest(field=field):
                manifest = json.loads(json.dumps(original))
                manifest["profile"][field] = 9
                write_json(path, manifest)
                pair = build_token_report(self.root, results, "live")["paired_A_D"][0]
                self.assertFalse(pair["comparable"])
                self.assertIn("runtime_options." + field, pair["reason"])

    def test_missing_critical_identity_field_names_are_reported(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage()])
        path = self.root / d["trial_directory"] / "manifest.json"
        manifest = read_json(path)
        del manifest["profile"]["summary_model"]
        manifest["profile"]["context"]["context_window_tokens"] = None
        write_json(path, manifest)
        pair = build_token_report(self.root, [a, d], "live")["paired_A_D"][0]
        self.assertFalse(pair["comparable"])
        self.assertIn("D.summary_model", pair["reason"])
        self.assertIn("D.common_context.context_window_tokens", pair["reason"])

    def test_unpaired_or_duplicate_trials_disable_overall_reduction(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage()])
        for results in ([a], [a, a, d]):
            with self.subTest(results=len(results)):
                report = build_token_report(self.root, results, "live")
                self.assertIsNone(report["comparison"]["reduction_fraction"])
                self.assertIn("不成对", report["paired_A_D"][0]["reason"])

    def test_duplicate_response_ids_and_bad_values_are_not_totals(self):
        result = self.trial("A", [usage()])
        path = self.root / result["trial_directory"] / "model-responses.jsonl"
        for values in ([{"request_index": 1, "usage": usage()}] * 2,
                       [{"request_index": 1, "usage": usage(cache_read=-1)}],
                       [{"request_index": 1, "usage": usage(cache_read=True)}],
                       [{"request_index": 1, "usage": usage(cache_read=None)}]):
            self.records(path, values)
            self.assertIsNone(trial_tokens(self.root, result, "live")["totals"]["total_tokens"])

    def test_regrade_reads_original_evidence_and_report_serializes_accounting(self):
        a = self.trial("A", [usage()])
        d = self.trial("D", [usage(20, 5, 0, 5)])
        for result in (a, d):
            result["evidence_directory"] = str(self.root / result["trial_directory"])
        output = self.root / "regraded"
        output.mkdir()
        write_report(output, [a, d], "live", "stress")
        report = read_json(output / "result.json")
        self.assertEqual(report["token_accounting"]["comparison"]["D_total_tokens"], 30)
        markdown = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("80.0%", markdown)
        self.assertIn("平均每次试验 token", markdown)
        self.assertIn("缓存创建", markdown)
        self.assertIn("Token 与费用总览", markdown)
        self.assertIn("空闲估算总费用", markdown)
        self.assertIn("cost_estimates", report)
        self.assertIsNone(report["cost_estimates"]["actual_billed_cny"])


if __name__ == "__main__":
    unittest.main()
