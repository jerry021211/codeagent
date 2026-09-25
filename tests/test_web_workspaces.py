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

    def test_fuzzy_search_ranks_names_and_filters_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("unrelated", "MyCodeAgent", "code", "codebase", "c_o_d_e"):
                (root / name).mkdir()
            (root / "unrelated" / ".git").mkdir()
            (root / "code-file.txt").write_text("x", encoding="utf-8")
            catalog = WorkspaceCatalog(root, max_entries=4)
            listing = catalog.list(query="CoDe")
            self.assertEqual(listing.current, str(root.resolve()))
            self.assertEqual(
                [entry.name for entry in listing.entries],
                ["code", "codebase", "MyCodeAgent", "c_o_d_e"],
            )

    def test_partial_absolute_path_returns_candidates_without_resolving_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ii-project-00").mkdir()
            default = root / "default"
            default.mkdir()
            catalog = WorkspaceCatalog(default)
            listing = catalog.list(query=str(root / "ii00"))
            self.assertEqual(listing.current, str(root.resolve()))
            self.assertEqual([entry.name for entry in listing.entries], ["ii-project-00"])
            self.assertEqual(catalog.list(query=str(root / "no-match")).entries, ())
            with self.assertRaises(ValueError):
                catalog.resolve(root / "ii00")

    def test_search_exact_name_is_selectable_and_separator_lists_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "项目目录"
            project.mkdir()
            (project / "child").mkdir()
            catalog = WorkspaceCatalog(root)
            for query in (str(project), "项目目录", "项录"):
                with self.subTest(query=query):
                    self.assertEqual([entry.path for entry in catalog.list(query=query).entries], [str(project.resolve())])
            listing = catalog.list(query=str(project) + "/")
            self.assertEqual(listing.current, str(project.resolve()))
            self.assertEqual([entry.name for entry in listing.entries], ["child"])
            with self.assertRaises(ValueError):
                catalog.list(query=str(root / "missing" / "child"))


if __name__ == "__main__":
    unittest.main()
