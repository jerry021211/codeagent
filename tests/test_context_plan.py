from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.context_suite.plan import STEPS, describe_steps, prepare_kit, run_step, step_definitions
from evals.evidence import read_json, verify_seal


class ContextPlanTests(unittest.TestCase):
    def test_each_suite_budget_covers_exact_matrix_and_paid_flags_match_mode(self):
        for step in step_definitions():
            if step["kind"] != "suite":
                self.assertFalse(step["paid"])
                continue
            options = step["options"]
            count = len(options["cases"]) * len(options["variants"]) * options["repeats"]
            self.assertEqual(options["max_trials"], count)
            self.assertEqual(options["max_total_api_calls"], count * options["max_api_calls"])
            self.assertEqual(options["max_total_tokens"], 0)
            self.assertEqual(step["paid"], options["mode"] == "live")
        copy = step_definitions()
        copy[2]["options"]["cases"].clear()
        self.assertEqual(len(STEPS["03-rehearsal"]["options"]["cases"]), 6)

    def test_one_step_dispatches_once_and_listing_does_not_call_a_model(self):
        with patch("evals.context_suite.plan.run_suite", return_value=Path("result")) as run:
            self.assertIn("真实请求上限", describe_steps())
            run.assert_not_called()
            self.assertEqual(run_step("06-live-repeat"), Path("result"))
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["repeats"], 5)
            self.assertEqual(run.call_args.kwargs["max_trials"], 10)
            with self.assertRaises(ValueError):
                run_step("run-all-without-checking")

    def test_cli_live_preview_does_not_dispatch(self):
        from evals.context_suite.__main__ import main
        with patch("sys.argv", ["context", "step", "06-live-repeat", "--preview"]), \
                patch("evals.context_suite.__main__.run_step") as run:
            self.assertEqual(main(), 0)
            run.assert_not_called()

    def test_complete_kit_has_all_materials_and_seal_and_never_runs_a_suite(self):
        with tempfile.TemporaryDirectory() as temp, patch("evals.context_suite.plan.run_suite") as run:
            root = prepare_kit(Path(temp) / "kit")
            run.assert_not_called()
            self.assertTrue(verify_seal(root)["valid"])
            self.assertEqual(read_json(root / "kit-manifest.json")["model_calls"], 0)
            for scale in ("stress", "production"):
                self.assertEqual(len(read_json(root / f"materials-{scale}/index.json")["cases"]), 6)
                self.assertTrue((root / f"materials-{scale}/S03/gold.json").exists())
                self.assertFalse((root / f"materials-{scale}/S03/workspace/gold.json").exists())
            for path in ("lifecycle", "manual"):
                self.assertTrue((root / path).is_dir())
            self.assertTrue((root / "一步一步操作.md").is_file())
            with self.assertRaises(FileExistsError):
                prepare_kit(root)


if __name__ == "__main__":
    unittest.main()
