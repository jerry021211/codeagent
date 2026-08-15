from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.web.workspaces import WorkspaceCatalog


class WorkspaceCatalogTests(unittest.TestCase):
    def test_lists_directories_and_marks_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
            (root / "ordinary").mkdir()
            (root / "file.txt").write_text("hidden", encoding="utf-8")
            listing = WorkspaceCatalog(root).list()
            self.assertEqual(listing.current, str(root.resolve()))
            self.assertEqual([entry.name for entry in listing.entries], ["project", "ordinary"])
            self.assertTrue(listing.entries[0].is_project)
            self.assertFalse(listing.entries[1].is_project)

    def test_rejects_relative_missing_and_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = WorkspaceCatalog(root)
            file_path = root / "file.txt"
            file_path.write_text("x", encoding="utf-8")
            for invalid in ("relative", root / "missing", file_path):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    catalog.resolve(invalid)


if __name__ == "__main__":
    unittest.main()
