from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent import create_default_registry
from codeagent.skills import SkillLoader
from codeagent.tools import (
    COMPACT_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
    TASK_TOOL_NAME,
    TaskTool,
    TodoStore,
    TodoWriteTool,
    create_todo_final_status_hook,
)


class DefaultToolTests(unittest.TestCase):
    def test_default_registry_exposes_plain_tool_schemas(self) -> None:
        registry = create_default_registry()
        schemas = {schema["name"]: schema for schema in registry.schemas()}

        self.assertEqual(
            set(schemas),
            {
                "bash",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "todo_write",
            },
        )
        self.assertIn("input_schema", schemas["read_file"])
        self.assertNotIn("function", schemas["read_file"])

    def test_task_tool_delegates_to_injected_spawn_function(self) -> None:
        calls = []
        tool = TaskTool(spawn_fn=lambda description: calls.append(description) or "done")

        result = tool.run("inspect the repository")

        self.assertEqual(result, "done")
        self.assertEqual(calls, ["inspect the repository"])
        self.assertEqual(tool.definition.name, TASK_TOOL_NAME)

    def test_default_registry_can_include_task_with_spawn_function(self) -> None:
        registry = create_default_registry(task_spawn_fn=lambda description: "summary")
        schemas = {schema["name"]: schema for schema in registry.schemas()}

        self.assertIn(TASK_TOOL_NAME, schemas)
        self.assertEqual(
            registry.execute(TASK_TOOL_NAME, {"description": "delegate"}),
            "summary",
        )

    def test_default_registry_can_include_load_skill_with_loader(self) -> None:
        loader = SkillLoader(roots=[])

        registry = create_default_registry(skill_loader=loader)
        schemas = {schema["name"] for schema in registry.schemas()}

        self.assertIn(LOAD_SKILL_TOOL_NAME, schemas)

    def test_default_registry_can_include_compact_with_callback(self) -> None:
        registry = create_default_registry(compact_fn=lambda: "compacted")
        schemas = {schema["name"] for schema in registry.schemas()}

        self.assertIn(COMPACT_TOOL_NAME, schemas)
        self.assertEqual(registry.execute(COMPACT_TOOL_NAME, {}), "compacted")

    def test_registry_can_copy_without_named_tools(self) -> None:
        registry = create_default_registry()

        copied = registry.copy_without({"bash", "todo_write"})
        schemas = {schema["name"] for schema in copied.schemas()}

        self.assertNotIn("bash", schemas)
        self.assertNotIn("todo_write", schemas)
        self.assertIn("read_file", schemas)

    def test_file_tools_write_read_and_edit(self) -> None:
        registry = create_default_registry()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"

            write_result = registry.execute(
                "write_file",
                {"file_path": str(path), "content": "alpha\nbeta\n"},
            )
            self.assertIn("Wrote 2 lines", write_result)

            read_result = registry.execute(
                "read_file",
                {"file_path": str(path), "offset": 2, "limit": 1},
            )
            self.assertEqual(read_result, "2\tbeta")

            edit_result = registry.execute(
                "edit_file",
                {
                    "file_path": str(path),
                    "old_string": "beta",
                    "new_string": "gamma",
                },
            )
            self.assertIn("Edited", edit_result)
            self.assertEqual(path.read_text(encoding="utf-8"), "alpha\ngamma\n")

    def test_search_tools_find_files_and_content(self) -> None:
        registry = create_default_registry()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "main.py"
            target.parent.mkdir()
            target.write_text("print('needle')\n", encoding="utf-8")

            glob_result = registry.execute(
                "glob",
                {"path": str(root), "pattern": "**/*.py"},
            )
            self.assertIn(str(target), glob_result)

            grep_result = registry.execute(
                "grep",
                {"path": str(root), "pattern": "needle", "include": "*.py"},
            )
            self.assertIn(f"{target}:1:", grep_result)

    def test_bash_blocks_obvious_destructive_commands(self) -> None:
        registry = create_default_registry()

        result = registry.execute("bash", {"command": "rm -rf /"})

        self.assertIn("Blocked", result)

    def test_todo_write_updates_only_in_memory_plan(self) -> None:
        store = TodoStore()
        tool = TodoWriteTool(store=store)

        result = tool.run(
            [
                {"content": "Inspect the relevant files", "status": "completed"},
                {"content": "Implement the focused change", "status": "in_progress"},
                {"content": "Run focused tests", "status": "pending"},
            ]
        )

        self.assertIn("Updated 3 todos", result)
        self.assertEqual(
            store.todos,
            [
                {"content": "Inspect the relevant files", "status": "completed"},
                {"content": "Implement the focused change", "status": "in_progress"},
                {"content": "Run focused tests", "status": "pending"},
            ],
        )

    def test_todo_write_prints_created_updated_and_completed_events(self) -> None:
        store = TodoStore()
        events: list[str] = []
        tool = TodoWriteTool(store=store, on_change=events.append)

        tool.run(
            [
                {"content": "Inspect files", "status": "in_progress"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        tool.run(
            [
                {"content": "Inspect files", "status": "in_progress"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        tool.run(
            [
                {"content": "Inspect files", "status": "completed"},
                {"content": "Run tests", "status": "in_progress"},
            ]
        )
        tool.run(
            [
                {"content": "Inspect files", "status": "completed"},
                {"content": "Run tests", "status": "completed"},
            ]
        )

        self.assertEqual(len(events), 3)
        self.assertIn("[todo created]", events[0])
        self.assertIn("1. [>] Inspect files", events[0])
        self.assertIn("[todo updated]", events[1])
        self.assertIn("Inspect files: in_progress -> completed", events[1])
        self.assertIn("[todo completed]", events[2])
        self.assertIn("2. [x] Run tests", events[2])

    def test_todo_final_status_hook_reports_open_work_once(self) -> None:
        store = TodoStore(
            todos=[
                {"content": "Inspect files", "status": "completed"},
                {"content": "Run tests", "status": "pending"},
            ],
            revision=1,
        )
        events: list[str] = []
        hook = create_todo_final_status_hook(store, events.append)

        hook([])
        hook([])

        self.assertEqual(len(events), 1)
        self.assertIn("[todo final]", events[0])
        self.assertIn("2. [ ] Run tests", events[0])

    def test_default_registry_uses_injected_todo_store(self) -> None:
        store = TodoStore()
        registry = create_default_registry(todo_store=store)

        result = registry.execute(
            "todo_write",
            {"todos": [{"content": "Plan isolated agent work", "status": "pending"}]},
        )

        self.assertIn("Updated 1 todos", result)
        self.assertEqual(
            store.todos,
            [{"content": "Plan isolated agent work", "status": "pending"}],
        )

    def test_todo_write_accepts_safe_string_inputs(self) -> None:
        store = TodoStore()
        tool = TodoWriteTool(store=store)

        json_result = tool.run(
            '[{"content": "inspect repo", "status": "pending"}]'
        )
        self.assertIn("Updated 1 todos", json_result)
        self.assertEqual(
            store.todos,
            [{"content": "inspect repo", "status": "pending"}],
        )

        literal_result = tool.run(
            "[{'content': 'write tests', 'status': 'in_progress'}]"
        )
        self.assertIn("Updated 1 todos", literal_result)
        self.assertEqual(
            store.todos,
            [{"content": "write tests", "status": "in_progress"}],
        )

    def test_todo_write_rejects_unsafe_or_conflicting_inputs(self) -> None:
        store = TodoStore()
        tool = TodoWriteTool(store=store)

        result = tool.run("__import__('os').system('echo bad')")
        self.assertIn("Error:", result)
        self.assertEqual(store.todos, [])

        result = tool.run(
            [
                {"content": "first active", "status": "in_progress"},
                {"content": "second active", "status": "in_progress"},
            ]
        )
        self.assertIn("only one todo can be in_progress", result)


if __name__ == "__main__":
    unittest.main()
