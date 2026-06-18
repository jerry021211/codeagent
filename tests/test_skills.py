from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.skills import SkillLoader
from codeagent.tools import LOAD_SKILL_TOOL_NAME, LoadSkillTool


class SkillLoaderTests(unittest.TestCase):
    def test_loader_scans_catalog_and_loads_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "python-refactor"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: python-refactor",
                        "description: Refactor Python safely.",
                        "when_to_use: Use for type hints and docstrings.",
                        "---",
                        "",
                        "# Python Refactor",
                        "",
                        "Follow the local style.",
                    ]
                ),
                encoding="utf-8",
            )

            loader = SkillLoader(roots=[root])

        skills = loader.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "python-refactor")
        self.assertIn("Refactor Python safely", loader.catalog_prompt())
        self.assertIn("Use for type hints", loader.catalog_prompt())
        loaded = loader.load("python-refactor")
        self.assertIn("# Python Refactor", loaded.content)

    def test_loader_rejects_unknown_skill_name(self) -> None:
        loader = SkillLoader(roots=[])

        with self.assertRaises(KeyError):
            loader.load("../SKILL.md")

    def test_load_skill_tool_returns_full_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "code-review"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: code-review\ndescription: Review code.\n---\n\n# Review\n",
                encoding="utf-8",
            )
            loader = SkillLoader(roots=[root])

            tool = LoadSkillTool(loader=loader)
            result = tool.run("code-review")

        self.assertEqual(tool.definition.name, LOAD_SKILL_TOOL_NAME)
        self.assertIn("[skill loaded] code-review", result)
        self.assertIn("# Review", result)


if __name__ == "__main__":
    unittest.main()
