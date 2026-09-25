from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.base import ToolDefinition, ToolInputState, ToolOutput, normalize_tool_output
from codeagent.tools.edit import EditFileTool
from codeagent.tools.registry import ToolRegistry
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.tools.write import WriteFileTool


class ToolEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.edit = EditFileTool(workspace_guard=WorkspaceGuard(self.root))
        self.write = WriteFileTool(workspace_guard=WorkspaceGuard(self.root))
        self.registry = ToolRegistry()
        self.registry.register(self.edit)
        self.registry.register(self.write)

    def test_output_remains_a_string_and_preserves_execution_evidence(self):
        facts = dict(
            status="error", exit_code=1, outcome="diagnostic", deterministic=True,
            input_state="hash", state_known=True, result_signature="failure",
            changed_files=("a.py",), process_id=42, process_running=True,
            duration_seconds=1.5, retryable=True, retry_safe=True,
        )
        output = ToolOutput("diagnosis", **facts)
        self.assertIsInstance(output, str)
        self.assertEqual(output, "diagnosis")
        self.assertIs(normalize_tool_output(output), output)
        output.guard_feedback = "try another diagnosis"
        self.assertEqual(normalize_tool_output(output).guard_feedback, "try another diagnosis")
        for key, value in facts.items():
            self.assertEqual(getattr(output, key), value)
        self.assertFalse(ToolOutput("ok").deterministic)
        self.assertFalse(ToolOutput("ok").state_known)
        self.assertEqual(normalize_tool_output("Error: failed").status, "error")
        with self.assertRaises(ValueError):
            ToolOutput("bad", status="invalid")

    def test_input_state_is_frozen_and_defaults_to_unknown(self):
        state = ToolInputState()
        self.assertEqual((state.fingerprint, state.known, state.cwd), ("", False, ""))
        with self.assertRaises(FrozenInstanceError):
            state.known = True

    def test_schema_errors_prevent_handler_and_wrapper_execution(self):
        handler = Mock(return_value="ok")
        wrapper = Mock(return_value="wrapped")
        registry = ToolRegistry(wrapper)
        registry.register_handler(ToolDefinition("probe", "", {
            "type": "object", "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        }), handler)
        for args in ({}, {"count": True}, {"count": "1"}, []):
            with self.subTest(args=args):
                result = registry.execute("probe", args)
                self.assertEqual(result.outcome, "parameter_error")
                self.assertTrue(result.deterministic)
                self.assertIn("Error:", result)
        handler.assert_not_called()
        wrapper.assert_not_called()
        self.assertIsNone(registry.parameter_error("probe", {"count": 1}))
        self.assertEqual(registry.execute("probe", {"count": 1}), "wrapped")
        wrapper.assert_called_once()

    def test_basic_property_types(self):
        cases = [
            ("string", "text", 1), ("integer", 2, True), ("number", 1.5, False),
            ("boolean", True, 1), ("array", [], {}), ("object", {}, []),
            ("null", None, "null"), (["string", "null"], None, 2),
        ]
        for kind, valid, invalid in cases:
            with self.subTest(kind=kind):
                registry = ToolRegistry()
                registry.register_handler(ToolDefinition("probe", "", {
                    "properties": {"value": {"type": kind}},
                }), lambda **kwargs: "ok")
                self.assertIsNone(registry.parameter_error("probe", {"value": valid}))
                self.assertIsNotNone(registry.parameter_error("probe", {"value": invalid}))
                self.assertIsNone(registry.parameter_error("probe", {}))

    def test_unknown_tool_is_actionable_and_deterministic(self):
        result = self.registry.execute("missing", {})
        self.assertEqual(result.outcome, "parameter_error")
        self.assertEqual(result.result_signature, "unknown_tool")
        self.assertTrue(result.deterministic)
        self.assertIn("available tool schemas", result)

    def test_builtin_edit_validation_signatures_ignore_unrelated_fields(self):
        invalid = [
            {"file_path": "a", "new_string": "secret"},
            {"file_path": "b", "old_string": "", "new_string": "other", "extra": 1},
        ]
        results = [self.registry.parameter_error("edit_file", args) for args in invalid]
        self.assertEqual(results[0].result_signature, results[1].result_signature)
        same = [self.registry.parameter_error("edit_file", {
            "file_path": name, "old_string": secret, "new_string": secret, "extra": index,
        }) for index, (name, secret) in enumerate((("a", "secret"), ("b", "other")))]
        self.assertEqual(same[0].result_signature, same[1].result_signature)
        self.assertNotIn("secret", str(results) + str(same))
        self.assertFalse((self.root / "a").exists())

    def test_custom_handler_named_edit_file_uses_only_its_schema(self):
        registry = ToolRegistry()
        registry.register_handler(ToolDefinition("edit_file", "", {"type": "object"}), lambda: "custom")
        self.assertEqual(registry.execute("edit_file", {}), "custom")

    def test_corrected_parameters_can_execute(self):
        (self.root / "a").write_text("before", encoding="utf-8")
        args = {"file_path": "a", "old_string": "before", "new_string": "before"}
        self.assertEqual(self.registry.execute("edit_file", args).outcome, "parameter_error")
        args["new_string"] = "after"
        self.assertEqual(self.registry.execute("edit_file", args).status, "success")
        self.assertEqual((self.root / "a").read_text(encoding="utf-8"), "after")

    def test_direct_edit_and_write_validate_without_registry(self):
        for result in (
            self.edit.run(), self.edit.run("a", "", "new"), self.edit.run("a", "same", "same"),
            self.edit.run("a", 1, "new"), self.write.run(), self.write.run("a", 1),
        ):
            self.assertEqual(result.outcome, "parameter_error")
            self.assertTrue(result.deterministic)
            self.assertEqual(result.changed_files, ())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_fingerprint_tracks_uncommitted_content_and_new_files(self):
        args = {"file_path": "a"}
        missing = self.registry.input_state("edit_file", args)
        path = self.root / "a"
        path.write_bytes(b"first")
        first = self.registry.input_state("edit_file", args)
        path.write_bytes(b"other")  # Same size, no Git metadata needed.
        second = self.registry.input_state("edit_file", args)
        (self.root / "unrelated").write_bytes(b"change")
        self.assertEqual(second, self.registry.input_state("edit_file", args))
        path.write_bytes(b"first")
        self.assertEqual(first, self.registry.input_state("edit_file", args))
        self.assertEqual(len({missing.fingerprint, first.fingerprint, second.fingerprint}), 3)
        self.assertTrue(all(state.known for state in (missing, first, second)))
        self.assertEqual(first.cwd, str(self.root))
        self.assertNotIn("first", first.fingerprint)

    def test_directory_missing_and_empty_file_have_distinct_states(self):
        path = self.root / "target"
        missing = self.edit.input_state({"file_path": "target"})
        path.mkdir()
        directory = self.edit.input_state({"file_path": "target"})
        path.rmdir()
        path.write_bytes(b"")
        empty = self.edit.input_state({"file_path": "target"})
        self.assertTrue(all(item.known for item in (missing, directory, empty)))
        self.assertEqual(len({item.fingerprint for item in (missing, directory, empty)}), 3)

    def test_outside_workspace_is_unknown_and_never_read(self):
        args = {"file_path": str(self.root.parent / "outside.txt")}
        with patch.object(Path, "read_bytes", side_effect=AssertionError("outside read")):
            self.assertFalse(self.edit.input_state(args).known)
            result = self.edit.run(args["file_path"], "old", "new")
            write_result = self.write.run(args["file_path"], "new")
        for output in (result, write_result):
            self.assertEqual(output.status, "error")
            self.assertFalse(output.deterministic)
            self.assertEqual(output.changed_files, ())

    def test_permission_and_read_errors_do_not_claim_known_state(self):
        path = self.root / "a"
        path.write_text("old", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertFalse(self.edit.input_state({"file_path": "a"}).known)
            output = self.edit.run("a", "old", "new")
        self.assertFalse(output.deterministic)
        self.assertFalse(output.state_known)
        self.assertEqual(output.changed_files, ())
        path.write_bytes(b"\xff")
        self.assertFalse(self.edit.run("a", "old", "new").deterministic)

    def test_edit_diagnostics_carry_actual_input_fingerprint(self):
        path = self.root / "a"
        path.write_text("repeat repeat", encoding="utf-8")
        state = self.edit.input_state({"file_path": "a"})
        for original, category in (("missing", "old_string_not_found"), ("repeat", "ambiguous_old_string")):
            with self.subTest(original=original):
                result = self.registry.execute("edit_file", {
                    "file_path": "a", "old_string": original, "new_string": "new",
                })
                self.assertEqual(result.status, "error")
                self.assertEqual(result.outcome, "diagnostic")
                self.assertTrue(result.deterministic)
                self.assertTrue(result.state_known)
                self.assertEqual(result.input_state, state.fingerprint)
                self.assertEqual(result.result_signature, f"edit:{category}")
                self.assertEqual(result.changed_files, ())
        self.assertEqual(self.edit.changed_files, set())

    def test_actual_changes_are_reported_and_noop_write_does_not_touch_file(self):
        path = self.root / "a"
        created = self.write.run("a", "before\n")
        self.assertEqual(created.changed_files, (str(path),))
        self.write.changed_files.clear()
        with patch.object(Path, "write_text", side_effect=AssertionError("no-op write")):
            repeated = self.write.run("a", "before\n")
        self.assertEqual(repeated.status, "success")
        self.assertEqual(repeated.changed_files, ())
        self.assertEqual(self.write.changed_files, set())
        before = self.edit.input_state({"file_path": "a"})
        edited = self.edit.run("a", "before", "after")
        self.assertEqual(edited.changed_files, (str(path),))
        self.assertEqual(edited.input_state, before.fingerprint)
        self.assertEqual(self.edit.changed_files, {str(path)})
        self.assertNotEqual(before, self.edit.input_state({"file_path": "a"}))
        self.assertEqual(self.write.run("a", "replacement").changed_files, (str(path),))

    def test_creating_empty_file_is_a_change_and_binary_file_can_be_overwritten(self):
        path = self.root / "a"
        self.assertEqual(self.write.run("a", "").changed_files, (str(path),))
        self.assertEqual(self.write.run("a", "").changed_files, ())
        path.write_bytes(b"\xff")
        self.assertEqual(self.write.run("a", "text").changed_files, (str(path),))

    def test_edit_with_equivalent_on_disk_newlines_is_not_a_change(self):
        path = self.root / "a"
        path.write_bytes(b"a\rb")
        with patch.object(Path, "write_text", side_effect=AssertionError("no-op edit")):
            output = self.edit.run("a", "\n", "\r")
        self.assertEqual(output.outcome, "parameter_error")
        self.assertEqual(output.changed_files, ())
        self.assertEqual(self.edit.changed_files, set())
        self.assertEqual(path.read_bytes(), b"a\rb")

    def test_providers_survive_registry_copy_and_wrapper(self):
        state = ToolInputState("provided", True, str(self.root))
        self.registry.register_handler(
            ToolDefinition("probe", "", {}), lambda: "ok", input_state=lambda args: state,
        )
        for registry in (
            self.registry.copy_without({"write_file"}),
            self.registry.with_execution_wrapper(lambda name, args, handler: handler(**args)),
        ):
            self.assertEqual(registry.input_state("probe", {}), state)
            self.assertEqual(registry.execute("probe"), "ok")
            self.assertEqual(registry.input_state("edit_file", {"file_path": "a"}),
                             self.edit.input_state({"file_path": "a"}))

    def test_generic_shell_state_is_unknown_with_bound_cwd(self):
        class Shell:
            cwd = self.root

            def run(self, command):
                return "ok"

        self.registry.register_handler(ToolDefinition("bash", "", {}), Shell().run)
        state = self.registry.input_state("bash", {"command": "pytest"})
        self.assertFalse(state.known)
        self.assertEqual(state.fingerprint, "")
        self.assertEqual(state.cwd, str(self.root))
        self.assertEqual(self.registry.input_state("missing", {}).cwd, str(Path.cwd()))

    def test_registry_never_retries_or_swallows_execution_stop(self):
        handler = Mock(side_effect=ConnectionError("temporary"))
        self.registry.register_handler(ToolDefinition("probe", "", {}), handler)
        self.assertEqual(self.registry.execute("probe").status, "error")
        handler.assert_called_once()

        stopped = Mock(side_effect=ExecutionStopped("budget"))
        self.registry.register_handler(ToolDefinition("stop", "", {}), stopped)
        with self.assertRaises(ExecutionStopped):
            self.registry.execute("stop")
        stopped.assert_called_once()

    def test_bind_runtime_forwards_callbacks_to_bound_tools_only(self):
        calls = []

        class RuntimeTool:
            def bind_runtime(self, cancellation_check, remaining_seconds):
                calls.append((cancellation_check, remaining_seconds))

            def run(self):
                return "ok"

        self.registry.register_handler(ToolDefinition("runtime", "", {}), RuntimeTool().run)
        self.registry.register_handler(ToolDefinition("plain", "", {}), lambda: "ok")
        cancellation_check = Mock()
        remaining_seconds = Mock(return_value=10.0)
        wrapped = self.registry.with_execution_wrapper(lambda name, args, handler: handler(**args))
        wrapped.bind_runtime(cancellation_check, remaining_seconds)
        self.assertEqual(calls, [(cancellation_check, remaining_seconds)])
        cancellation_check.assert_not_called()
        remaining_seconds.assert_not_called()


if __name__ == "__main__":
    unittest.main()
