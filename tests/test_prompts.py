from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.prompts import PromptConfig, PromptMode, PromptRuntime


class PromptRuntimeTests(unittest.TestCase):
    def test_runtime_includes_tool_conditioned_fragments(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())

        result = runtime.assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[
                {"name": "todo_write"},
                {"name": "subagent"},
                {"name": "load_skill"},
            ],
            skill_catalog="Available skills:\n- python-refactor: Refactor Python.",
        )

        self.assertIn("interactive coding agent", result.system_prompt)
        self.assertIn("call todo_write before", result.system_prompt)
        self.assertIn("Use the subagent tool", result.system_prompt)
        self.assertIn("focused investigation", result.system_prompt)
        self.assertIn("final integration", result.system_prompt)
        self.assertIn("do not submit the same failed assignment", result.system_prompt)
        self.assertIn("Available skills:", result.system_prompt)
        self.assertIn("SYSTEM_PROMPT_DYNAMIC_BOUNDARY", result.system_prompt)
        self.assertIn("Current workspace:", result.system_prompt)
        self.assertEqual(
            [item.id for item in result.trace],
            [
                "base.identity",
                "base.execution",
                "tools.available",
                "tools.todo",
                "tools.subagent",
                "skills.catalog",
                "runtime.reminder",
            ],
        )
        self.assertEqual(result.trace[0].source, "templates/identity.md")

        repeated = runtime.assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[
                {"name": "todo_write"},
                {"name": "subagent"},
                {"name": "load_skill"},
            ],
            skill_catalog="Available skills:\n- python-refactor: Refactor Python.",
        )
        self.assertEqual(repeated.system_prompt, result.system_prompt)
        self.assertEqual(repeated.prompt_hash, result.prompt_hash)

    def test_subagent_mode_does_not_include_subagent_guidance(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())

        result = runtime.assemble(
            mode=PromptMode.SUBAGENT,
            tool_schemas=[{"name": "subagent"}, {"name": "read_file"}],
        )

        self.assertIn("focused coding subagent", result.system_prompt)
        self.assertIn("Edit files only when", result.system_prompt)
        self.assertIn("## Outcome", result.system_prompt)
        self.assertNotIn("Use the subagent tool", result.system_prompt)

    def test_task_guidance_replaces_todo_guidance(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())

        result = runtime.assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[
                {"name": "TaskCreate"},
                {"name": "TaskGet"},
                {"name": "TaskList"},
                {"name": "TaskUpdate"},
            ],
        )

        self.assertIn("Perform the work directly in this conversation", result.system_prompt)
        self.assertIn("2 to 5 finishable phase tasks", result.system_prompt)
        self.assertNotIn("call todo_write before", result.system_prompt)

    def test_project_template_overrides_builtin_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / ".prompts"
            prompts.mkdir()
            (prompts / "todo.md").write_text("CUSTOM TODO TEMPLATE", encoding="utf-8")
            runtime = PromptRuntime(workspace=root)

            result = runtime.assemble(
                mode=PromptMode.NORMAL,
                tool_schemas=[{"name": "todo_write"}],
            )

            self.assertIn("CUSTOM TODO TEMPLATE", result.system_prompt)

    def test_project_identity_template_overrides_builtin_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / ".prompts"
            prompts.mkdir()
            (prompts / "identity.md").write_text(
                "PROJECT CODING AGENT",
                encoding="utf-8",
            )

            result = PromptRuntime(workspace=root).assemble(
                mode=PromptMode.NORMAL,
                tool_schemas=[],
            )

            self.assertTrue(result.system_prompt.startswith("PROJECT CODING AGENT"))

    def test_selected_memory_replaces_memory_catalog(self) -> None:
        result = PromptRuntime(workspace=Path.cwd()).assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[{"name": "load_memory"}],
            selected_memory_context="selected memory",
            memory_catalog="memory catalog",
        )

        self.assertIn("selected memory", result.system_prompt)
        self.assertNotIn("memory catalog", result.system_prompt)
        self.assertIn("memory.selected", [item.id for item in result.trace])

    def test_dynamic_budget_clips_low_priority_runtime_content(self) -> None:
        runtime = PromptRuntime(
            workspace=Path.cwd(),
            config=PromptConfig(dynamic_budget_chars=200),
        )

        result = runtime.assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[],
            selected_memory_context="x" * 1000,
        )

        self.assertLessEqual(
            sum(item.chars for item in result.trace if item.section == "dynamic"),
            200,
        )
        self.assertTrue(any(item.clipped for item in result.trace))


if __name__ == "__main__":
    unittest.main()
