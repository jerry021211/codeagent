from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.config import EnvironmentConfig
from codeagent.cli import create_skill_loader
from codeagent.skills import SkillLoader
from codeagent.tools import LOAD_SKILL_TOOL_NAME, LoadSkillTool
from codeagent.web.factory import WebAgentFactory


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


class SharedSkillLibraryTests(unittest.TestCase):
    @staticmethod
    def write_skill(root: Path, content: str) -> None:
        directory = root / "code-review"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: code-review\ndescription: Review code.\n---\n\n" + content,
            encoding="utf-8",
        )

    def test_cli_and_web_share_skills_across_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            for configured in (None, Path("custom-skills"), root / "external-skills", Path(".skills")):
                with self.subTest(configured=configured):
                    options = {} if configured is None else {"skill_roots": (configured,)}
                    env = EnvironmentConfig(model_id="test", data_dir=data, **options)
                    expected = configured or Path("skills")
                    expected = expected if expected.is_absolute() else data / expected
                    self.write_skill(expected, "Shared instructions")
                    for name in ("project-a", "project-b", "team-worktree"):
                        workspace = root / str(expected.name) / name
                        workspace.mkdir(parents=True)
                        self.write_skill(workspace / ".skills", "Project instructions")
                        if expected.name != ".skills":
                            self.write_skill(workspace / expected.name, "Project instructions")
                        factory = WebAgentFactory(env, workspace, task_service=None)
                        for loader in (create_skill_loader(env, workspace), factory._skill_loader()):
                            self.assertIsNotNone(loader)
                            self.assertEqual(loader.roots, [expected.resolve()])
                            self.assertEqual([skill.name for skill in loader.list_skills()], ["code-review"])
                            self.assertIn("Shared instructions", loader.load("code-review").content)
                            self.assertNotIn("Project instructions", loader.load("code-review").content)
                        self.assertFalse(factory.workspace_guard.allows(expected))

    def test_missing_shared_library_does_not_fall_back_to_project_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            self.write_skill(workspace / ".skills", "Project instructions")
            env = EnvironmentConfig(model_id="test", data_dir=root / "data")
            factory = WebAgentFactory(env, workspace, task_service=None)
            for loader in (create_skill_loader(env, workspace), factory._skill_loader()):
                self.assertEqual(loader.list_skills(), [])
            self.assertFalse(env.data_dir.exists())

    def test_disabled_skills_are_not_loaded_by_cli_or_web(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            env = EnvironmentConfig(model_id="test", enable_skills=False)
            self.assertIsNone(create_skill_loader(env, workspace))
            self.assertIsNone(WebAgentFactory(env, workspace, task_service=None)._skill_loader())


if __name__ == "__main__":
    unittest.main()
