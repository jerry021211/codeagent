from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codeagent import Agent, AgentConfig
from codeagent.planning import PlanningBackend, resolve_planning_backend
from codeagent.tools import TodoStore, TodoWriteTool, create_default_registry
from codeagent.web.storage import SQLiteRepository, StorageConflictError


class TaskSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.repository = SQLiteRepository(
            self.workspace / "state.db",
            recover_incomplete=False,
        )
        self.conversation = self.repository.create_conversation(
            title="Tasks",
            workspace=self.workspace,
        )
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def test_task_record_has_exactly_nine_semantic_fields(self) -> None:
        resource = self.repository.create_task(
            self.task_list_id,
            subject="Build storage",
            description="Implement and verify storage",
            active_form="Building storage",
            metadata={"priority": "high"},
        )

        self.assertEqual(
            set(resource.task.to_dict(camel_case=True)),
            {
                "id",
                "subject",
                "description",
                "activeForm",
                "owner",
                "status",
                "blocks",
                "blockedBy",
                "metadata",
            },
        )
        self.assertEqual(resource.task.id, "1")
        self.assertEqual(resource.revision, 1)

    def test_dependencies_block_execution_and_reject_cycles(self) -> None:
        first = self.repository.create_task(
            self.task_list_id, subject="First", description="First task"
        )
        second = self.repository.create_task(
            self.task_list_id,
            subject="Second",
            description="Second task",
            blocked_by=[first.task.id],
        )
        self.assertEqual(second.task.blocked_by, (first.task.id,))

        with self.assertRaises(StorageConflictError):
            self.repository.update_task(
                self.task_list_id,
                second.task.id,
                changes={"status": "in_progress"},
                actor_owner="owner",
            )
        with self.assertRaises(StorageConflictError):
            self.repository.update_task(
                self.task_list_id,
                first.task.id,
                changes={"add_blocked_by": [second.task.id]},
                human_override=True,
            )

        self.repository.update_task(
            self.task_list_id,
            first.task.id,
            changes={"status": "in_progress"},
            actor_owner="owner",
        )
        self.repository.update_task(
            self.task_list_id,
            first.task.id,
            changes={"status": "completed"},
            actor_owner="owner",
        )
        started = self.repository.update_task(
            self.task_list_id,
            second.task.id,
            changes={"status": "in_progress"},
            actor_owner="owner",
        )
        self.assertEqual(started.task.owner, "owner")

    def test_owner_can_only_have_one_in_progress_task(self) -> None:
        first = self.repository.create_task(
            self.task_list_id, subject="First", description="First"
        )
        second = self.repository.create_task(
            self.task_list_id, subject="Second", description="Second"
        )
        self.repository.update_task(
            self.task_list_id,
            first.task.id,
            changes={"status": "in_progress"},
            actor_owner="owner",
        )
        with self.assertRaises(StorageConflictError):
            self.repository.update_task(
                self.task_list_id,
                second.task.id,
                changes={"status": "in_progress"},
                actor_owner="owner",
            )

    def test_private_list_must_be_promoted_before_cross_conversation_binding(self) -> None:
        other = self.repository.create_conversation(
            title="Other",
            workspace=self.workspace,
        )
        with self.assertRaises(StorageConflictError):
            self.repository.bind_conversation_task_list(other.id, self.task_list_id)

        promoted = self.repository.update_task_list(
            self.task_list_id,
            promote=True,
        )
        rebound = self.repository.bind_conversation_task_list(other.id, promoted.id)
        self.assertEqual(rebound.active_task_list_id, promoted.id)

    def test_task_tools_replace_todo_write(self) -> None:
        registry = create_default_registry(
            planning_backend=PlanningBackend.TASKS,
            task_service=self.repository,
            task_list_id=self.task_list_id,
            conversation_id=self.conversation.id,
        )
        names = {schema["name"] for schema in registry.schemas()}

        self.assertTrue({"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"} <= names)
        self.assertNotIn("todo_write", names)
        created = registry.execute(
            "TaskCreate",
            {"subject": "Tool task", "description": "Created through tool"},
        )
        self.assertIn('"id": "1"', created)
        dependent = registry.execute(
            "TaskCreate",
            {
                "subject": "Dependent task",
                "description": "Wait for the first task",
                "blockedBy": ["1"],
            },
        )
        self.assertIn('"blockedBy": [\n    "1"', dependent)

    def test_task_list_summaries_allow_filtered_selection_and_detail_lookup(self) -> None:
        registry = create_default_registry(
            planning_backend=PlanningBackend.TASKS,
            task_service=self.repository,
            task_list_id=self.task_list_id,
            conversation_id=self.conversation.id,
        )
        self.assertEqual(json.loads(registry.execute("TaskList")), [])
        first = json.loads(registry.execute("TaskCreate", {
            "subject": "Build storage",
            "description": "Implement storage and verify persistence after reopening.",
            "activeForm": "Building storage",
            "metadata": {"validation_commands": ["python -m unittest"]},
        }))
        second = json.loads(registry.execute("TaskCreate", {
            "subject": "Integrate storage",
            "description": "Connect the verified storage to the API.",
            "blockedBy": [first["id"]],
        }))
        started = json.loads(registry.execute("TaskUpdate", {
            "taskId": first["id"], "status": "in_progress",
        }))

        summaries = json.loads(registry.execute("TaskList"))
        self.assertEqual(len(summaries), 2)
        by_id = {task["id"]: task for task in summaries}
        for summary in summaries:
            self.assertEqual(set(summary), {
                "id", "subject", "status", "owner", "blocks", "blockedBy",
            })
            detail = json.loads(registry.execute("TaskGet", {"taskId": summary["id"]}))
            for key, value in summary.items():
                self.assertEqual(detail[key], value)
        self.assertEqual(by_id[first["id"]]["blocks"], [second["id"]])
        self.assertEqual(by_id[second["id"]]["blockedBy"], [first["id"]])
        self.assertEqual(json.loads(registry.execute("TaskList", {
            "status": "in_progress", "owner": started["owner"],
        })), [by_id[first["id"]]])
        self.assertEqual(json.loads(registry.execute("TaskList", {
            "status": "completed",
        })), [])
        detail = json.loads(registry.execute("TaskGet", {"taskId": first["id"]}))
        for key in ("description", "activeForm", "metadata"):
            self.assertEqual(detail[key], first[key])

    def test_planning_backend_defaults_and_agent_mutual_exclusion(self) -> None:
        self.assertEqual(
            resolve_planning_backend("auto", interactive=True),
            PlanningBackend.TASKS,
        )
        self.assertEqual(
            resolve_planning_backend("auto", interactive=False),
            PlanningBackend.TODO,
        )
        registry = create_default_registry(
            planning_backend=PlanningBackend.TASKS,
            task_service=self.repository,
            task_list_id=self.task_list_id,
        )
        registry.register(TodoWriteTool(store=TodoStore()))
        with self.assertRaisesRegex(ValueError, "cannot be registered together"):
            Agent(
                client=object(),
                tools=registry,
                config=AgentConfig(model="fake"),
            )


if __name__ == "__main__":
    unittest.main()
