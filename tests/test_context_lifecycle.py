"""Offline lifecycle evidence includes actual cross-process SQLite recovery."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from evals.context_suite.lifecycle import CASES, prepare_lifecycle, run_lifecycle
from evals.evidence import hashes, read_json, verify_seal, write_json


class ContextLifecycleTests(unittest.TestCase):
    def test_existing_materials_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = prepare_lifecycle(Path(directory) / "materials")
            before = hashes(root)
            with self.assertRaises(FileExistsError):
                prepare_lifecycle(root)
            self.assertEqual(hashes(root), before)
            self.assertTrue(verify_seal(root)["valid"])

    def test_nonzero_process_cannot_pass_via_worker_result(self):
        def failed_process(command, **kwargs):
            case = Path(command[command.index("--case") + 1])
            phase = command[command.index("--phase") + 1]
            write_json(case / f"{phase}-result.json", {"passed": True, "returncode": 0, "phase": "forged"})
            return subprocess.CompletedProcess(command, 1, "", "injected failed exit")

        with tempfile.TemporaryDirectory() as directory, patch(
            "evals.context_suite.lifecycle.subprocess.run", side_effect=failed_process
        ) as process:
            run = run_lifecycle(Path(directory))
            result = read_json(run / "result.json")
            for flag in ("passed", "all_passed", "valid", "quality_measurement"):
                self.assertFalse(result[flag])
            self.assertEqual(process.call_count, len(CASES))
            for case in result["cases"]:
                self.assertFalse(case["passed"])
                self.assertEqual(len(case["phases"]), 1)
                self.assertEqual(case["phases"][0]["returncode"], 1)
                self.assertEqual(case["phases"][0]["phase"], "prepare")
            self.assertTrue(verify_seal(run)["valid"])

    def test_worker_error_keeps_partial_state_and_failed_result(self):
        from evals.context_suite.lifecycle_worker import ScriptedClient, execute

        original = ScriptedClient.create_message

        def fail_second_summary(client, **kwargs):
            if client.stage == 2:
                raise RuntimeError("injected local summary failure")
            return original(client, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "R01"
            case.mkdir()
            with patch.object(ScriptedClient, "create_message", fail_second_summary):
                result = execute(case, "prepare")
            self.assertFalse(result["passed"])
            self.assertEqual(result["error_type"], "ContextCompactionError")
            self.assertEqual(read_json(case / "prepare-result.json"), result)
            self.assertEqual(read_json(case / "prepare-state.json")["summary_revision"], 1)
            self.assertTrue((case / "prepare-messages.json").exists())
            self.assertTrue((case / "prepare-scripted-calls.jsonl").exists())

    def test_materials_have_all_stages_and_separate_reviewer_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = prepare_lifecycle(Path(directory) / "materials")
            self.assertTrue(verify_seal(root)["valid"])
            for case_id in CASES:
                self.assertEqual(len(read_json(root / case_id / "stages.json")["stages"]), 4)
                self.assertFalse(read_json(root / case_id / "case.json")["quality_claim_allowed"])
            manual = root / "manual-model-quality"
            self.assertTrue((manual / "stage-04.txt").exists())
            self.assertFalse(read_json(manual / "expected-for-reviewer-only.json")["published"])

    def test_three_lifecycle_cases_use_new_processes_and_leave_verified_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            run = run_lifecycle(Path(directory))
            result = read_json(run / "result.json")
            self.assertTrue(result["passed"], result)
            self.assertTrue(result["valid"])
            self.assertTrue(result["all_passed"])
            self.assertFalse(result["quality_measurement"])
            self.assertEqual(result["paid_model_calls"], 0)
            self.assertFalse(result["model_quality_measured"])
            self.assertTrue(verify_seal(run)["valid"])
            self.assertEqual([item["id"] for item in result["cases"]], list(CASES))
            for case in result["cases"]:
                before, after = case["phases"]
                self.assertNotEqual(before["pid"], after["pid"])
                self.assertTrue(after["checks"]["new_python_process"])
                self.assertTrue((run / case["id"] / "runtime/state.db").exists())
            r01 = result["cases"][0]["phases"][1]["checks"]
            self.assertTrue(r01["fourth_summary_committed"])
            self.assertTrue(r01["earliest_linked_archive_readable"])
            self.assertTrue(result["cases"][1]["phases"][1]["checks"]["stale_cursor_rejected"])
            self.assertTrue(result["cases"][2]["phases"][1]["checks"]["unchanged_compacted_message_count"])


if __name__ == "__main__":
    unittest.main()
