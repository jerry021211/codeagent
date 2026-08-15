from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.tools import (
    BashTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WorkspaceGuard,
    WorkspaceViolationError,
    WriteFileTool,
    create_default_registry,
)


class WorkspaceGuardTests(unittest.TestCase):
    def test_resolves_workspace_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guard = WorkspaceGuard(root)

            self.assertEqual(guard.resolve("src/main.py"), root / "src" / "main.py")

    def test_allows_absolute_and_parent_paths_that_remain_inside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child = root / "child"
            child.mkdir()
            guard = WorkspaceGuard(root)

            self.assertEqual(guard.resolve(root / "inside.txt"), root / "inside.txt")
            self.assertEqual(
                guard.resolve("../inside.txt", base=child),
                root / "inside.txt",
            )

    def test_rejects_absolute_and_parent_paths_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guard = WorkspaceGuard(root)

            with self.assertRaises(WorkspaceViolationError):
                guard.resolve(root.parent / "outside.txt")
            with self.assertRaises(WorkspaceViolationError):
                guard.resolve("../outside.txt")

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(workspace_dir)
            outside = Path(outside_dir)
            outside_file = outside / "secret.txt"
            outside_file.write_text("secret", encoding="utf-8")
            link = root / "external"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            guard = WorkspaceGuard(root)
            with self.assertRaises(WorkspaceViolationError):
                guard.resolve("external/secret.txt")


class GuardedToolTests(unittest.TestCase):
    def test_file_tools_confine_access_and_share_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guard = WorkspaceGuard(root)
            changed_files: set[str] = set()
            writer = WriteFileTool(
                workspace_guard=guard,
                changed_files=changed_files,
            )
            reader = ReadFileTool(workspace_guard=guard)
            editor = EditFileTool(
                workspace_guard=guard,
                changed_files=changed_files,
            )

            self.assertIn("Wrote 1 lines", writer.run("sample.txt", "alpha"))
            self.assertEqual(reader.run("sample.txt"), "1\talpha")
            self.assertIn("Edited", editor.run("sample.txt", "alpha", "beta"))
            self.assertEqual(changed_files, {str(root / "sample.txt")})

            self.assertIn("Workspace access denied", reader.run("../outside.txt"))
            self.assertIn(
                "Workspace access denied",
                writer.run("../outside.txt", "blocked"),
            )
            self.assertFalse((root.parent / "outside.txt").exists())

    def test_search_tools_reject_outside_paths_and_filter_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(workspace_dir)
            outside = Path(outside_dir)
            (root / "inside.py").write_text("needle", encoding="utf-8")
            outside_file = outside / "outside.py"
            outside_file.write_text("needle", encoding="utf-8")
            guard = WorkspaceGuard(root)

            self.assertIn(
                str(root / "inside.py"),
                GlobTool(workspace_guard=guard).run("*.py"),
            )
            self.assertIn(
                f"{root / 'inside.py'}:1:",
                GrepTool(workspace_guard=guard).run("needle"),
            )
            self.assertIn(
                "Workspace access denied",
                GlobTool(workspace_guard=guard).run("*.py", "../"),
            )
            self.assertIn(
                "Workspace access denied",
                GrepTool(workspace_guard=guard).run("needle", "../"),
            )

            link = root / "outside.py"
            try:
                link.symlink_to(outside_file)
            except OSError:
                return
            self.assertNotIn(
                str(outside_file),
                GlobTool(workspace_guard=guard).run("*.py"),
            )
            self.assertNotIn(
                str(outside_file),
                GrepTool(workspace_guard=guard).run("needle"),
            )

    def test_bash_cwd_is_isolated_per_instance(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            (first / "child").mkdir()
            first_tool = BashTool(workspace_guard=WorkspaceGuard(first))
            second_tool = BashTool(workspace_guard=WorkspaceGuard(second))

            result = first_tool.run("cd child")

            self.assertNotIn("Error", result)
            self.assertEqual(first_tool.cwd, (first / "child").resolve())
            self.assertEqual(second_tool.cwd, second.resolve())
            self.assertNotIn("Blocked", first_tool.run("cd .."))
            self.assertEqual(first_tool.cwd, first.resolve())
            self.assertIn("Blocked", first_tool.run("cd .."))

    def test_default_registry_without_guard_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "absolute.txt"
            registry = create_default_registry()

            result = registry.execute(
                "write_file",
                {"file_path": str(target), "content": "allowed by CLI mode"},
            )

            self.assertIn("Wrote 1 lines", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "allowed by CLI mode")

    def test_default_registry_guard_confines_all_filesystem_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            changed_files: set[str] = set()
            registry = create_default_registry(
                workspace_guard=WorkspaceGuard(root),
                changed_files=changed_files,
            )

            self.assertIn(
                "Wrote 1 lines",
                registry.execute(
                    "write_file",
                    {"file_path": "inside.txt", "content": "inside"},
                ),
            )
            self.assertEqual(changed_files, {str(root / "inside.txt")})
            for tool, args in (
                ("read_file", {"file_path": "../outside.txt"}),
                ("write_file", {"file_path": "../outside.txt", "content": "x"}),
                (
                    "edit_file",
                    {"file_path": "../outside.txt", "old_string": "x", "new_string": "y"},
                ),
                ("glob", {"pattern": "*", "path": "../"}),
                ("grep", {"pattern": "x", "path": "../"}),
            ):
                self.assertIn("Workspace access denied", registry.execute(tool, args))


if __name__ == "__main__":
    unittest.main()
