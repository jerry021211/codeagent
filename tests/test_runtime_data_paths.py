from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent.runtime import RuntimeDataPaths, default_runtime_data_dir


class RuntimeDataPathsTests(unittest.TestCase):
    def test_explicit_data_dir_does_not_require_a_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = Path(temp_dir) / "runtime"
            with patch.dict(
                "os.environ",
                {"CODEAGENT_DATA_DIR": str(configured)},
                clear=True,
            ), patch(
                "codeagent.runtime.data_paths.Path.home",
                side_effect=RuntimeError("home unavailable"),
            ):
                self.assertEqual(default_runtime_data_dir(), configured.resolve())

    def test_same_project_has_stable_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = RuntimeDataPaths(root / "runtime")
            project = root / "project"
            worktree = root / "worktree"
            project.mkdir()
            worktree.mkdir()

            workspace_id = data.workspace_id(project)

            self.assertEqual(workspace_id, data.workspace_id(project / "."))
            self.assertTrue(data.memory_dir(project).is_relative_to(data.root))
            self.assertFalse(data.memory_dir(project).is_relative_to(project))
            self.assertNotEqual(data.workspace_id(project), data.workspace_id(worktree))

    def test_contexts_are_isolated_by_conversation_agent_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = RuntimeDataPaths(root / "runtime")
            project = root / "project"
            project.mkdir()

            first = data.context_dir(
                project,
                conversation_id="conv/one",
                agent_id="agent:root",
            )
            second = data.context_dir(
                project,
                conversation_id="conv/one",
                agent_id="agent:teammate",
                generation=2,
            )

            self.assertNotEqual(first, second)
            self.assertTrue(first.is_relative_to(data.workspace_dir(project)))
            self.assertTrue(second.is_relative_to(data.workspace_dir(project)))

    def test_legacy_import_copies_once_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "project" / ".memory"
            destination = root / "runtime" / "memory"
            source.mkdir(parents=True)
            (source / "preference.md").write_text("legacy", encoding="utf-8")

            self.assertTrue(RuntimeDataPaths.import_legacy_directory(source, destination))
            self.assertEqual(
                (destination / "preference.md").read_text(encoding="utf-8"),
                "legacy",
            )
            self.assertTrue(source.exists())
            self.assertFalse(RuntimeDataPaths.import_legacy_directory(source, destination))

    def test_legacy_import_rejects_a_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "project" / ".memory"
            destination = root / "runtime" / "memory"
            source.mkdir(parents=True)

            with patch.object(Path, "is_symlink", return_value=True):
                copied = RuntimeDataPaths.import_legacy_directory(
                    source, destination
                )

            self.assertFalse(copied)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
