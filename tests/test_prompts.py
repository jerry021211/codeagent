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
            base_system_prompt="base prompt",
            model="fake-model",
            tool_schemas=[
                {"name": "todo_write"},
                {"name": "task"},
                {"name": "load_skill"},
            ],
            skill_catalog="Available skills:\n- python-refactor: Refactor Python.",
        )

        self.assertIn("base prompt", result.system_prompt)
        self.assertIn("call todo_write before", result.system_prompt)
        self.assertIn("Use the task tool", result.system_prompt)
        self.assertIn("Available skills:", result.system_prompt)
        self.assertIn("SYSTEM_PROMPT_DYNAMIC_BOUNDARY", result.system_prompt)
        self.assertTrue(result.reminder_messages)

    def test_subagent_mode_does_not_include_task_guidance(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())

        result = runtime.assemble(
            mode=PromptMode.SUBAGENT,
            base_system_prompt="base prompt",
            model="fake-model",
            tool_schemas=[{"name": "task"}, {"name": "read_file"}],
        )

        self.assertIn("focused coding subagent", result.system_prompt)
        self.assertNotIn("Use the task tool", result.system_prompt)

    def test_project_template_overrides_builtin_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / ".prompts"
            prompts.mkdir()
            (prompts / "todo.md").write_text("CUSTOM TODO TEMPLATE", encoding="utf-8")
            runtime = PromptRuntime(workspace=root)

            result = runtime.assemble(
                mode=PromptMode.NORMAL,
                base_system_prompt="base prompt",
                model="fake-model",
                tool_schemas=[{"name": "todo_write"}],
            )

            self.assertIn("CUSTOM TODO TEMPLATE", result.system_prompt)

    def test_dynamic_budget_clips_low_priority_runtime_content(self) -> None:
        runtime = PromptRuntime(
            workspace=Path.cwd(),
            config=PromptConfig(dynamic_budget_chars=200),
        )

        result = runtime.assemble(
            mode=PromptMode.NORMAL,
            base_system_prompt="base prompt",
            model="fake-model",
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
