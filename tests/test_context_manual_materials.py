from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from evals.context_suite.manual_materials import prepare_manual


class ManualContextMaterialsTests(unittest.TestCase):
    def test_pack_is_inert_separates_gold_and_has_concrete_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            manifest = prepare_manual(root)
            self.assertEqual(manifest["model_calls"], 0)
            self.assertEqual(manifest["status"], "materials_only_not_run")
            self.assertEqual([case["prompt_count"] for case in manifest["cases"]], [13, 9, 2])
            for case in manifest["cases"]:
                folder = root / case["case_id"]
                self.assertTrue((folder / "操作.md").is_file())
                self.assertFalse((folder / "runtime-data").exists())
                self.assertFalse(list((folder / "workspace").rglob("*expected*")))
                template = json.loads((folder / "evaluator-only" / "result-template.json").read_text(encoding="utf-8"))
                self.assertEqual(template["status"], "not_run")
                self.assertIsNone(template["answer_passed"])
            # Final questions contain field names, never the gold values.
            final = (root / "M01/prompts/13-prompt.txt").read_text(encoding="utf-8")
            for answer in ("LHX-8241", "exports/final-v3.json", "Asia/Shanghai", "UTF-8"):
                self.assertNotIn(answer, final)
            self.assertGreater(len((root / "M01/prompts/02-prompt.txt").read_text(encoding="utf-8")), 6000)
            initial = (root / "M01/prompts/01-prompt.txt").read_text(encoding="utf-8")
            update = (root / "M01/prompts/05-prompt.txt").read_text(encoding="utf-8")
            self.assertIn("LHX-8241", initial)
            self.assertNotIn("LHX-8241", update)
            with self.assertRaises(FileExistsError):
                prepare_manual(root)

    def test_restart_and_launch_keep_runtime_outside_materials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            prepare_manual(root)
            script = (root / "M02/start.ps1").read_text(encoding="utf-8")
            self.assertIn("[switch]$Resume", script)
            self.assertIn("CODEAGENT_DATA_DIR", script)
            self.assertIn("sealed materials package", script)
            self.assertIn("Join-Path $PSScriptRoot 'workspace'", script)
            self.assertNotIn("evaluator-only", script)
            self.assertIn("$manualOriginalEnvironment", script)
            self.assertIn("SetEnvironmentVariable($manualName, $manualOriginalEnvironment[$manualName]", script)
            self.assertTrue((root / "README.md").is_file())
            guide = (root / "M02/操作.md").read_text(encoding="utf-8")
            self.assertIn("after-restart-before-message", guide)
            self.assertIn("compacted_prefix_hash", guide)
            self.assertIn("Ctrl+C", guide)
            compile((root / "capture-state.py").read_text(encoding="utf-8"), "capture-state.py", "exec")

    def test_each_manual_compaction_has_complete_history_to_fold(self):
        from codeagent.context import ContextConfig, ContextManager

        class FakeSummaryClient:
            def __init__(self):
                self.calls = 0

            def create_message(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(content=[{"type": "text", "text": f"Offline summary for boundary validation {self.calls}"}], stop_reason="end_turn")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            prepare_manual(root)
            for case_id, expected_count in (("M01", 3), ("M02", 2)):
                manager = ContextManager(config=ContextConfig(
                    summarization_model="offline-fixture", recency_messages=2, recency_rounds=2,
                    min_fold_messages=4, transcript_dir=Path(temporary) / case_id / "archives",
                ))
                client = FakeSummaryClient()
                messages = []
                cursors = []
                for path in sorted((root / case_id / "prompts").glob("*.txt")):
                    prompt = path.read_text(encoding="utf-8")
                    manager.begin_turn(len(messages))
                    messages.append({"role": "user", "content": prompt})
                    if int(path.name[:2]) in (4, 8, 12):
                        tool_id = "compact-" + path.name[:2]
                        messages.extend([
                            {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "compact", "input": {}}]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "requested"}]},
                        ])
                        manager.force_compact(messages, client=client)
                        self.assertEqual(manager.last_compaction["status"], "written")
                        cursors.append(manager.state.compacted_message_count)
                    messages.append({"role": "assistant", "content": "已收到。"})
                self.assertEqual(client.calls, expected_count)
                self.assertEqual(manager.state.summary_revision, expected_count)
                self.assertEqual(cursors, sorted(set(cursors)))
                self.assertGreater(cursors[0], 0)

    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"), "PowerShell is not installed")
    def test_generated_powershell_parses_without_executing_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            prepare_manual(root)
            executable = shutil.which("powershell") or shutil.which("pwsh")
            for path in root.glob("*/start.ps1"):
                literal = str(path).replace("'", "''")
                command = (
                    "$tokens = $null; $errors = $null; "
                    f"[System.Management.Automation.Language.Parser]::ParseFile('{literal}', [ref]$tokens, [ref]$errors) | Out-Null; "
                    "if ($errors.Count -gt 0) { $errors | Out-String | Write-Output; exit 1 }"
                )
                subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True, text=True, check=True)

    def test_large_output_target_is_outside_preview_and_retrievable(self):
        from codeagent.context import ContextConfig, ContextManager
        from codeagent.messages import ToolUse
        from codeagent.tools.read import ReadFileTool
        from codeagent.tools.runtime_data import LoadToolOutputTool

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            prepare_manual(root)
            source = root / "M03/workspace/synthetic-log.txt"
            output = ReadFileTool().run(str(source), offset=1, limit=400)
            self.assertGreater(len(output), 4000)
            archive = Path(temporary) / "outputs"
            context = ContextManager(config=ContextConfig(mode="off", single_tool_output_max_chars=4000, tool_output_dir=archive))
            preview = context._finalize_single_tool_result(ToolUse(id="manual-read", name="read_file", input={"file_path": str(source)}), output)
            self.assertIn("[tool output stored]", preview)
            self.assertNotIn("INC-R7-4829", preview)
            recovered = LoadToolOutputTool(archive).run(str(archive / "manual-read.txt"), offset=271, limit=1)
            self.assertIn("INC-R7-4829", recovered)
            self.assertIn("retry_allowed=false", recovered)

    def test_capture_script_reads_real_repository_and_refuses_overwrite(self):
        from codeagent.web.storage import SQLiteRepository

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manual"
            prepare_manual(root)
            run = Path(temporary) / "run"
            repository = SQLiteRepository.for_workspace(run / "workspace", data_dir=run / "runtime-data")
            conversation = repository.create_conversation(title="manual fixture", workspace=str(run / "workspace"))
            repository.close()
            command = [sys.executable, str(root / "capture-state.py"), "--run-dir", str(run), "--label", "first"]
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn("first.json", completed.stdout)
            result = json.loads((run / "evidence/first.json").read_text(encoding="utf-8"))
            self.assertEqual(result["conversations"][0]["id"], conversation.id)
            self.assertEqual(result["status"], "evidence_only_not_a_pass")
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)


if __name__ == "__main__":
    unittest.main()
