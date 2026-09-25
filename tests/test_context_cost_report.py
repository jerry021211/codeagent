from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from evals.context_suite.cost_report import build_cost_report, cost_report_lines, load_pricing
from evals.evidence import write_json


def kind(input_tokens, output_tokens, cached=0, created=0, complete=True):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": created, "complete": complete}


def evidence():
    trials = [
        {"case_id": "S03", "variant": "A", "repeat": 1, "trial_directory": "a", "task_success": True,
         "by_kind": {"main": kind(42443, 622, 169472), "context_summary": kind(0, 0)}},
        {"case_id": "S03", "variant": "D", "repeat": 1, "trial_directory": "d", "task_success": True,
         "by_kind": {"main": kind(14188, 875, 58112), "context_summary": kind(32226, 614)}},
    ]
    return {"mode": "live", "trials": trials,
            "groups": [{"variant": "A", "totals": {"total_tokens": 212537}},
                       {"variant": "D", "totals": {"total_tokens": 106015}}],
            "paired_A_D": [{"case_id": "S03", "repeat": 1, "comparable": True}]}


class ContextCostReportTests(unittest.TestCase):
    def test_known_example_includes_summary_and_cache_and_cost_increases(self):
        report = build_cost_report(evidence())
        low = report["scenarios"]["offpeak"]
        high = report["scenarios"]["peak"]
        self.assertAlmostEqual(low["groups"][0]["estimated_cny"], 0.04832044, places=10)
        self.assertAlmostEqual(low["groups"][1]["estimated_cny"], 0.05353224, places=10)
        self.assertAlmostEqual(low["groups"][1]["by_kind"]["context_summary"], 0.034682, places=10)
        self.assertAlmostEqual(high["groups"][1]["estimated_cny"], 0.10706448, places=10)
        self.assertAlmostEqual(low["comparison"]["reduction_fraction"], -0.107859117176913, places=10)
        self.assertIsNone(report["actual_billed_cny"])
        text = "\n".join(cost_report_lines(report))
        for expected in ("212,537", "106,015", "增加 10.8%", "¥0.05353224", "不是实际扣费"):
            self.assertIn(expected, text)

    def test_failed_task_is_included_in_total_and_average(self):
        data = evidence()
        data["trials"][1]["task_success"] = False
        report = build_cost_report(data)["scenarios"]["offpeak"]
        self.assertEqual(report["groups"][1]["successes"], 0)
        self.assertAlmostEqual(report["groups"][1]["estimated_cny"], 0.05353224)
        self.assertEqual(report["groups"][1]["mean_cny_per_trial"], report["groups"][1]["estimated_cny"])

    def test_offline_and_missing_usage_never_become_zero_cost(self):
        for offline in (True, False):
            data = evidence()
            if offline:
                data["mode"] = "offline"
            else:
                data["trials"][1]["by_kind"]["context_summary"].update(input_tokens=None, complete=False)
            report = build_cost_report(data)
            for scenario in report["scenarios"].values():
                self.assertIsNone(scenario["groups"][1]["estimated_cny"])
                self.assertIsNone(scenario["comparison"]["reduction_fraction"])
            self.assertIn("未知", "\n".join(cost_report_lines(report)))

    def test_nonzero_unpriced_cache_creation_blocks_cost_but_zero_does_not(self):
        data = evidence()
        data["trials"][1]["by_kind"]["main"]["cache_creation_input_tokens"] = 10
        report = build_cost_report(data)["scenarios"]["offpeak"]
        self.assertIsNotNone(report["groups"][0]["estimated_cny"])
        self.assertIsNone(report["groups"][1]["estimated_cny"])
        self.assertIn("价格未提供", report["trials"][1]["by_kind"]["main"]["unavailable_reason"])

    def test_different_summary_prices_are_applied_separately(self):
        prices = deepcopy(load_pricing())
        prices["scenarios"]["offpeak"]["context_summary"]["output_tokens"] = "8"
        report = build_cost_report(evidence(), prices)
        self.assertAlmostEqual(report["scenarios"]["offpeak"]["groups"][1]["estimated_cny"], 0.05353224 + 614 * 4 / 1_000_000)
        self.assertEqual(report["pricing"]["scenarios"]["offpeak"]["context_summary"]["output_tokens"], "8")

    def test_zero_baseline_and_incomparable_pairs_do_not_produce_savings(self):
        data = evidence()
        data["paired_A_D"][0]["comparable"] = False
        self.assertIsNone(build_cost_report(data)["scenarios"]["offpeak"]["comparison"]["reduction_fraction"])
        data["paired_A_D"][0]["comparable"] = True
        data["trials"][0]["by_kind"]["main"] = kind(0, 0)
        comparison = build_cost_report(data)["scenarios"]["offpeak"]["comparison"]
        self.assertEqual(comparison["A_estimated_cny"], 0)
        self.assertIsNone(comparison["reduction_fraction"])

    def test_invalid_prices_fail_instead_of_creating_a_misleading_bill(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prices.json"
            for value in ("NaN", "Infinity", "-1", True, "invalid"):
                data = deepcopy(load_pricing())
                data["scenarios"]["offpeak"]["main"]["input_tokens"] = value
                write_json(path, data)
                with self.assertRaises(ValueError):
                    load_pricing(path)


if __name__ == "__main__":
    unittest.main()
