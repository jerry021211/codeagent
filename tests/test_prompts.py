from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.prompts import PromptConfig, PromptMode, PromptRuntime


class PromptRuntimeTests(unittest.TestCase):
    def test_read_only_memory_tools_do_not_advertise_remember(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())
        for mode in (PromptMode.TEAM_LEAD, PromptMode.TEAMMATE_ANALYSIS, PromptMode.TEAMMATE_WORK):
            result = runtime.assemble(
                mode=mode,
                tool_schemas=[{"name": "search_memory"}, {"name": "load_memory"}],
            )
            self.assertIn("Search or load memories", result.system_prompt)
            self.assertNotIn("`remember`", result.system_prompt)
        writable = runtime.assemble(
            mode=PromptMode.NORMAL, tool_schemas=[{"name": "remember"}],
        )
        self.assertIn("Use `remember`", writable.system_prompt)

    def test_runtime_warns_when_restored_tool_schema_changed(self) -> None:
        result = PromptRuntime(workspace=Path.cwd()).assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[{"name": "mcp__context7__query-docs"}],
            tool_schema_changed=True,
        )

        self.assertIn(
            "available tool schemas changed since the previous turn",
            result.system_prompt,
        )
        self.assertIn("tools.changed", [item.id for item in result.trace])

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
        self.assertIn("independent, bounded work unit", result.system_prompt)
        self.assertIn("final integration", result.system_prompt)
        self.assertIn("Diagnose a failed assignment", result.system_prompt)
        self.assertIn("Available skills:", result.system_prompt)
        self.assertIn("Current workspace:", result.system_prompt)
        self.assertEqual(
            [item.id for item in result.trace],
            [
                "base.identity",
                "base.execution",
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

        self.assertIn("focused coding Subagent", result.system_prompt)
        self.assertIn("edit only when explicitly requested", result.system_prompt)
        self.assertIn("## Outcome", result.system_prompt)
        self.assertNotIn("Use the subagent tool", result.system_prompt)

    def test_team_roles_receive_distinct_runtime_prompts(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())

        lead = runtime.assemble(
            mode=PromptMode.TEAM_LEAD,
            tool_schemas=[{"name": "team_get_status"}],
        )
        teammate_plan = runtime.assemble(
            mode=PromptMode.TEAMMATE_PLAN,
            tool_schemas=[{"name": "team_submit_attempt_plan"}],
        )
        teammate_work = runtime.assemble(
            mode=PromptMode.TEAMMATE_WORK,
            tool_schemas=[{"name": "team_submit_candidate"}],
        )
        teammate_analysis = runtime.assemble(
            mode=PromptMode.TEAMMATE_ANALYSIS,
            tool_schemas=[{"name": "team_submit_analysis_result"}],
        )

        self.assertIn("Root Agent acting as the Lead", lead.system_prompt)
        self.assertIn("Runtime owns scheduling", lead.system_prompt)
        self.assertIn("preparing one assigned code Task", teammate_plan.system_prompt)
        self.assertIn("remain read-only", teammate_plan.system_prompt)
        self.assertIn("implementing one assigned code Task", teammate_work.system_prompt)
        self.assertIn("submit one Candidate", teammate_work.system_prompt)
        self.assertIn("read-only analysis Task", teammate_analysis.system_prompt)
        self.assertEqual(lead.trace[0].source, "templates/team_lead_identity.md")
        self.assertEqual(
            teammate_plan.trace[0].source, "templates/teammate_plan.md"
        )

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
        self.assertIn("TaskCreate blockedBy", result.system_prompt)
        self.assertIn("both subject and description", result.system_prompt)
        self.assertNotIn("call todo_write before", result.system_prompt)

    def test_team_planner_has_distinct_concise_guidance(self) -> None:
        result = PromptRuntime(workspace=Path.cwd()).assemble(
            mode=PromptMode.TEAM_PLANNER,
            tool_schemas=[{"name": "TaskCreate"}, {"name": "TeamPlanSubmit"}],
        )

        self.assertIn("planning one explicitly requested Agent Team", result.system_prompt)
        self.assertIn("Keep every Team Task pending and unowned", result.system_prompt)
        self.assertIn("a rejected revision", result.system_prompt.lower())
        self.assertIn("immutable; create a new revision", result.system_prompt.lower())
        self.assertIn("manual", result.system_prompt.lower())
        self.assertIn("integration only", result.system_prompt.lower())
        self.assertNotIn("Perform the work directly", result.system_prompt)
        self.assertEqual(result.trace[0].source, "templates/team_planner.md")

    def test_team_planning_allows_small_teams_without_duplicate_design(self) -> None:
        runtime = PromptRuntime(workspace=Path.cwd())
        tools = [{"name": "TaskCreate"}, {"name": "TeamPlanSubmit"}]
        first = runtime.assemble(mode=PromptMode.TEAM_PLANNER, tool_schemas=tools)
        repeated = runtime.assemble(mode=PromptMode.TEAM_PLANNER, tool_schemas=tools)
        self.assertEqual(first.prompt_hash, repeated.prompt_hash)
        self.assertEqual(first.system_prompt, repeated.system_prompt)
        self.assertIn("one Teammate is valid", first.system_prompt)
        self.assertIn("not to fill roles", first.system_prompt.replace("\n", " "))
        self.assertIn("already sufficient specification", first.system_prompt)
        self.assertIn("another Task's changed files are not", first.system_prompt)
        self.assertIn("JSON boolean", first.system_prompt)
        self.assertIn("short work brief", first.system_prompt)
        self.assertIn("do not repeat it in Task descriptions", first.system_prompt)
        self.assertNotIn("consult(", first.system_prompt)
        self.assertNotIn("delegate(", first.system_prompt)

    def test_project_cannot_override_core_template(self) -> None:
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

            self.assertNotIn("CUSTOM TODO TEMPLATE", result.system_prompt)
            self.assertIn("call todo_write before", result.system_prompt)

    def test_project_instructions_are_appended_without_replacing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / ".prompts"
            prompts.mkdir()
            (prompts / "identity.md").write_text(
                "PROJECT CODING AGENT",
                encoding="utf-8",
            )
            (prompts / "project.md").write_text(
                "Use the project's public API conventions.",
                encoding="utf-8",
            )

            result = PromptRuntime(workspace=root).assemble(
                mode=PromptMode.NORMAL,
                tool_schemas=[],
            )

            self.assertTrue(result.system_prompt.startswith("You are an interactive"))
            self.assertNotIn("PROJECT CODING AGENT", result.system_prompt)
            self.assertIn("Use the project's public API conventions.", result.system_prompt)
            self.assertIn("project.instructions", [item.id for item in result.trace])

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
