from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, ModelResponse
from codeagent.config import EnvironmentConfig
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import (
    MemoryAccessController,
    MemoryConfig,
    MemoryStore,
    MemoryWriteBlocked,
)
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.prompts import PromptMode
from codeagent.teams import LeadTeamPlanTool
from codeagent.teams.tool_gate import TeamPlannerToolExecutionGate
from codeagent.tools import ToolRegistry
from codeagent.tools.tasks import create_task_tools
from codeagent.web.factory import WebAgentFactory
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from codeagent.worktrees import DirtyWorkspaceConfirmationRequired, WorktreeManagerRegistry


class LeadTeamPlanToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self._git("init")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "CodeAgent Tests")
        (self.workspace / "README.md").write_text("base\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base_commit = self._git("rev-parse", "HEAD")

        self.repository = SQLiteRepository(
            self.root / "state.db", recover_incomplete=False
        )
        self.conversation = self.repository.create_conversation(
            title="Lead Team entry", workspace=str(self.workspace.resolve())
        )
        self.run = self.repository.create_run(self.conversation.id, status="running")
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id
        self.first = self._create_code_task("components/calculator")
        self.second = self._create_code_task("components/text_tools")
        self.worktrees = WorktreeManagerRegistry(
            self.repository, self.root / "managed-worktrees"
        )
        self.tool = LeadTeamPlanTool(
            self.repository,
            self.worktrees,
            self.conversation.id,
            self.run.id,
            self.task_list_id,
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.workspace), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def _create_code_task(self, scope: str):
        return self.repository.create_task(
            self.task_list_id,
            subject=f"Implement {scope}",
            description=f"Only modify {scope}.",
            metadata={
                "kind": "code",
                "write_scopes": [scope],
                "risk_level": "low",
                "plan_required": False,
                "validation_commands": ["python -m unittest"],
            },
        )

    def _plan(self) -> dict[str, object]:
        return {
            "summary": "Implement two independent components in parallel.",
            "tasks": [
                {
                    "task_id": self.first.task.id,
                    "kind": "code",
                    "write_scopes": ["components/calculator"],
                    "risk_level": "low",
                },
                {
                    "task_id": self.second.task.id,
                    "kind": "code",
                    "write_scopes": ["components/text_tools"],
                    "risk_level": "low",
                },
            ],
        }

    def test_submit_creates_only_pending_approval_control_plane_records(self) -> None:
        result = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )

        team = self.repository.get_team_run(result["team_run_id"])
        self.assertEqual(team.state.value, "waiting_approval")
        self.assertIsNone(team.active_plan_revision)
        plan = self.repository.get_team_plan_revision(team.id, 1)
        self.assertEqual(plan.status.value, "pending_user_approval")
        self.assertEqual(plan.plan["teammate_count"], 2)
        self.assertEqual(len(self.repository.list_team_agents(team.id)), 1)
        sessions = self.repository.list_agent_sessions(team.id)
        self.assertEqual(len(sessions), 1)
        lead_sessions = [
            session
            for session in sessions
            if session.agent_id == team.lead_agent_id
        ]
        self.assertEqual(len(lead_sessions), 1)
        self.assertEqual(lead_sessions[0].state.value, "idle")
        self.assertEqual(self.repository.list_task_attempts(team.id), [])
        self.assertFalse((self.root / "managed-worktrees").exists())
        self.assertFalse(result["execution_started"])

    def test_read_only_task_cannot_request_document_writes(self) -> None:
        task = self.repository.create_task(
            self.task_list_id, subject="Create DESIGN.md", description="Write docs/DESIGN.md",
            metadata={"kind": "analysis", "write_scopes": ["docs/"]},
        )
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot"):
            self.tool.run(
                baseCommit=self.base_commit, teammateCount=1,
                plan={"tasks": [{"task_id": task.task.id, "kind": "analysis",
                                 "write_scopes": ["docs/"], "risk_level": "low"}]},
            )
        self.assertEqual(self.repository.list_team_runs(), [])
        self.assertFalse((self.root / "managed-worktrees").exists())

    def test_planner_returns_metadata_error_without_creating_task_and_allows_correction(self) -> None:
        registry = ToolRegistry()
        for tool in create_task_tools(self.repository, self.task_list_id):
            registry.register(tool)
        gate = TeamPlannerToolExecutionGate(
            self.repository, self.conversation.id, self.task_list_id,
        )
        registry = gate.wrap(registry)
        before = self.repository.list_task_resources(self.task_list_id)
        args = {"subject": "Clarify one unknown", "description": "Report the finding",
                "metadata": {"kind": "analysis", "plan_required": "true"}}
        rejected = registry.execute("TaskCreate", args)
        self.assertIn("JSON boolean", rejected)
        self.assertEqual(len(self.repository.list_task_resources(self.task_list_id)), len(before))
        args["metadata"]["plan_required"] = False
        corrected = registry.execute("TaskCreate", args)
        self.assertNotIn("Error:", corrected)
        self.assertEqual(len(self.repository.list_task_resources(self.task_list_id)), len(before) + 1)
        self.assertEqual(self.repository.list_team_runs(), [])

    def test_planner_update_validates_merged_metadata_without_mutating_on_error(self) -> None:
        registry = ToolRegistry()
        for tool in create_task_tools(self.repository, self.task_list_id):
            registry.register(tool)
        registry = TeamPlannerToolExecutionGate(
            self.repository, self.conversation.id, self.task_list_id,
        ).wrap(registry)
        rejected = registry.execute("TaskUpdate", {
            "taskId": self.first.task.id, "metadata": {"kind": "analysis"},
        })
        self.assertIn("analysis Tasks cannot declare", rejected)
        unchanged = self.repository.get_task_resource(self.task_list_id, self.first.task.id)
        self.assertEqual(unchanged.revision, self.first.revision)
        corrected = registry.execute("TaskUpdate", {
            "taskId": self.first.task.id,
            "metadata": {"kind": "analysis", "write_scopes": [], "plan_required": None},
        })
        self.assertNotIn("Error:", corrected)
        updated = self.repository.get_task_resource(self.task_list_id, self.first.task.id)
        self.assertNotIn("plan_required", updated.task.metadata)

    def test_planner_model_receives_error_and_corrects_with_unchanged_tool_set(self) -> None:
        calls = []

        class Client:
            def create_message(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 3:
                    return ModelResponse("end_turn", [{"type": "text", "text": "Task corrected"}])
                return ModelResponse("tool_use", [{
                    "type": "tool_use", "id": f"create-{len(calls)}", "name": "TaskCreate",
                    "input": {"subject": "Analyze one unknown", "description": "Return a report",
                              "metadata": {"kind": "analysis", "plan_required": "false" if len(calls) == 1 else False}},
                }])

        registry = ToolRegistry()
        for tool in create_task_tools(self.repository, self.task_list_id):
            registry.register(tool)
        registry = TeamPlannerToolExecutionGate(
            self.repository, self.conversation.id, self.task_list_id,
        ).wrap(registry)
        agent = Agent(client=Client(), tools=registry, config=AgentConfig(model="fake", max_iterations=4),
                      allow_subagents=False)
        agent.prompt_mode = PromptMode.TEAM_PLANNER
        agent.run("Plan the work without modifying repository files")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["tools"], calls[1]["tools"])
        errors = [block["content"] for message in calls[1]["messages"]
                  for block in message.get("content", [])
                  if isinstance(block, dict) and block.get("type") == "tool_result"]
        self.assertTrue(any("JSON boolean" in text for text in errors))
        created = self.repository.list_task_resources(self.task_list_id)
        self.assertEqual(len(created), 3)  # Two fixture tasks plus one corrected task.
        self.assertIs(created[-1].task.metadata["plan_required"], False)
        self.assertEqual(self.repository.list_team_runs(), [])

    def test_plan_submission_rejects_invalid_approval_metadata_before_creating_team(self) -> None:
        for value in ("true", "false", 1, None):
            # create_task stores opaque metadata for ordinary single-Agent Tasks.
            task = self.repository.create_task(
                self.task_list_id, subject="Report", description="Read-only finding",
                metadata={"kind": "analysis", "plan_required": value},
            )
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "JSON boolean"):
                self.tool.run(
                    baseCommit=self.base_commit, teammateCount=1,
                    plan={"tasks": [{"task_id": task.task.id, "kind": "analysis",
                                     "write_scopes": [], "risk_level": "low"}]},
                )
        self.assertEqual(self.repository.list_team_runs(), [])
        self.assertFalse((self.root / "managed-worktrees").exists())

    def test_submitted_plan_locks_approval_metadata(self) -> None:
        result = json.loads(self.tool.run(
            baseCommit=self.base_commit, plan=self._plan(), teammateCount=1,
        ))
        revision = self.repository.get_team_plan_revision(result["team_run_id"], 1)
        self.assertIs(revision.plan["tasks"][0]["plan_required"], False)
        self.repository.update_task(
            self.task_list_id, self.first.task.id, changes={"metadata": {"plan_required": True}},
        )
        with self.assertRaisesRegex(StorageConflictError, "plan_required differs"):
            self.repository.decide_team_plan_revision(
                result["team_run_id"], 1, decision="approve", decided_by="user",
                reason="approved", command_id="stale-approval-mode",
            )
        self.assertEqual(self.repository.list_task_attempts(result["team_run_id"]), [])

    def test_team_creation_freezes_and_terminal_state_restores_project_memory(self) -> None:
        controller = MemoryAccessController(
            self.repository.has_active_team_run_for_workspace
        )
        tool = LeadTeamPlanTool(
            self.repository,
            self.worktrees,
            self.conversation.id,
            self.run.id,
            self.task_list_id,
            controller,
        )
        store = MemoryStore(
            self.root / "runtime-memory",
            access_policy=controller.policy(self.workspace),
        )
        store.remember(
            name="Before Team",
            description="Ordinary Agent memory.",
            content="writable",
        )

        result = json.loads(
            tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )

        with self.assertRaises(MemoryWriteBlocked):
            store.remember(
                name="During Team",
                description="Must stay read-only.",
                content="blocked",
            )
        self.assertEqual(store.load("Before Team").content, "writable")

        self.repository.cancel_team_run(
            result["team_run_id"],
            cancelled_by="user",
            reason="test complete",
            command_id="cancel-memory-freeze-test",
        )
        store.remember(
            name="After Team",
            description="Ordinary Agent memory is writable again.",
            content="restored",
        )
        self.assertEqual(store.load("After Team").content, "restored")

    def test_repeated_identical_submission_is_idempotent(self) -> None:
        first = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )
        repeated = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )

        self.assertEqual(first["team_run_id"], repeated["team_run_id"])
        self.assertEqual(len(self.repository.list_team_runs()), 1)
        self.assertEqual(
            len(self.repository.list_team_plan_revisions(first["team_run_id"])), 1
        )
        self.assertEqual(
            len(self.repository.list_team_agents(first["team_run_id"])), 1
        )

    def test_rejected_r1_is_preserved_and_explicit_resubmission_creates_r2(self) -> None:
        first = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
            )
        )
        self.repository.decide_team_plan_revision(
            first["team_run_id"],
            1,
            decision="reject",
            decided_by="user",
            reason="Clarify validation",
            command_id="reject-r1-lead-tool",
        )
        revised_plan = self._plan()
        revised_plan["summary"] = "Clarified independent validation and review."

        second = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=revised_plan,
                teammateCount=2,
            )
        )

        self.assertEqual(second["team_run_id"], first["team_run_id"])
        revisions = self.repository.list_team_plan_revisions(first["team_run_id"])
        self.assertEqual([item.revision for item in revisions], [1, 2])
        self.assertEqual(revisions[0].status.value, "rejected")
        self.assertEqual(revisions[1].status.value, "pending_user_approval")
        self.assertEqual(self.repository.list_task_attempts(first["team_run_id"]), [])

    def test_dirty_source_is_rejected_before_team_persistence(self) -> None:
        (self.workspace / "README.md").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(DirtyWorkspaceConfirmationRequired):
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
            )

        self.assertEqual(self.repository.list_team_runs(), [])

    def test_scope_mismatch_is_rejected_before_team_persistence(self) -> None:
        plan = self._plan()
        plan["tasks"][0]["write_scopes"] = ["components"]

        with self.assertRaisesRegex(ValueError, "must exactly match"):
            self.tool.run(
                baseCommit=self.base_commit,
                plan=plan,
                teammateCount=2,
            )

        self.assertEqual(self.repository.list_team_runs(), [])

    def test_scheduler_excludes_tasks_omitted_from_approved_plan(self) -> None:
        omitted = self._create_code_task("components/omitted")
        result = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )
        self.repository.decide_team_plan_revision(
            result["team_run_id"],
            1,
            decision="approve",
            decided_by="user",
            reason="Approved for test",
            command_id="approve-team-lead-test",
        )

        decisions = {
            item.task_id: item
            for item in self.repository.list_task_scheduling(
                result["team_run_id"], allow_code=True
            )
        }
        self.assertIn(
            "task_not_in_active_team_plan", decisions[omitted.task.id].reasons
        )
        self.assertFalse(decisions[omitted.task.id].schedulable)

    def test_factory_registers_entry_only_for_explicit_team_planner(self) -> None:
        emitter = EventEmitter(
            context=ExecutionContext(
                conversation_id=self.conversation.id,
                run_id=self.run.id,
                agent_id="agent_root",
            )
        )
        client = type("FakeClient", (), {})()
        enabled = EnvironmentConfig(
            model_id="test-model",
            enable_skills=False,
            memory_config=MemoryConfig(enabled=False),
            team_runtime_enabled=True,
            team_write_enabled=True,
            team_worktree_root=self.root / "managed-worktrees",
        )
        disabled = EnvironmentConfig(
            model_id="test-model",
            enable_skills=False,
            memory_config=MemoryConfig(enabled=False),
            team_runtime_enabled=False,
            team_write_enabled=False,
            team_worktree_root=self.root / "managed-worktrees",
        )
        with patch.object(
            EnvironmentConfig, "create_anthropic_client", return_value=client
        ):
            normal_agent = WebAgentFactory(
                enabled, self.workspace, self.repository
            ).create(
                event_emitter=emitter,
                cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(),
            )
            planner_agent = WebAgentFactory(
                enabled, self.workspace, self.repository
            ).create(
                event_emitter=emitter,
                cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(),
                root_prompt_mode=PromptMode.TEAM_PLANNER,
            )
            disabled_planner = WebAgentFactory(
                disabled, self.workspace, self.repository
            ).create(
                event_emitter=emitter,
                cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(),
                root_prompt_mode=PromptMode.TEAM_PLANNER,
            )

        self.assertNotIn("TeamPlanSubmit", normal_agent.tools)
        self.assertIn("TeamPlanSubmit", planner_agent.tools)
        self.assertNotIn("write_file", planner_agent.tools)
        self.assertNotIn("subagent", planner_agent.tools)
        self.assertNotIn("TeamPlanSubmit", disabled_planner.tools)

    def test_rejected_plan_resumes_lead_in_planner_profile_with_history(self) -> None:
        submitted = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
            )
        )
        team_id = submitted["team_run_id"]
        self.repository.decide_team_plan_revision(
            team_id,
            1,
            decision="reject",
            decided_by="user",
            reason="Narrow the calculator scope",
            command_id="reject-for-planner-profile",
        )
        lead_session = self.repository.list_agent_sessions(
            team_id, role="lead"
        )[0]
        emitter = EventEmitter(
            context=ExecutionContext(
                conversation_id=self.conversation.id,
                run_id=self.run.id,
                agent_id=self.repository.get_team_run(team_id).lead_agent_id,
            )
        )
        env = EnvironmentConfig(
            model_id="test-model",
            enable_skills=False,
            memory_config=MemoryConfig(enabled=False),
            team_runtime_enabled=True,
            team_write_enabled=True,
            team_worktree_root=self.root / "managed-worktrees",
        )
        with patch.object(
            EnvironmentConfig,
            "create_anthropic_client",
            return_value=type("FakeClient", (), {})(),
        ):
            agent = WebAgentFactory(env, self.workspace, self.repository).create(
                event_emitter=emitter,
                cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(),
                team_session=lead_session,
                worktree_manager=self.worktrees,
            )

        self.assertEqual(agent.prompt_mode, PromptMode.TEAM_PLANNER)
        self.assertIn("TeamPlanSubmit", agent.tools)
        self.assertIn("team_get_status", agent.tools)
        self.assertNotIn("write_file", agent.tools)
        status = agent.tools.execute("team_get_status", {})
        self.assertIn("Narrow the calculator scope", status)

    def test_prebuilt_root_agent_uses_external_memory_and_dynamic_team_gate(self) -> None:
        emitter = EventEmitter(
            context=ExecutionContext(
                conversation_id=self.conversation.id,
                run_id=self.run.id,
                agent_id="agent_root",
            )
        )
        data_dir = self.root / "runtime-data"
        env = EnvironmentConfig(
            model_id="test-model",
            enable_skills=False,
            memory_config=MemoryConfig(enabled=True, selection_mode="simple"),
            data_dir=data_dir,
            team_runtime_enabled=True,
            team_write_enabled=True,
            team_worktree_root=Path("team-worktrees"),
        )
        client = type("FakeClient", (), {})()
        with patch.object(
            EnvironmentConfig, "create_anthropic_client", return_value=client
        ):
            agent = WebAgentFactory(env, self.workspace, self.repository).create(
                event_emitter=emitter,
                cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(),
            )

        memory_root = data_dir / "workspaces"
        self.assertTrue(agent.memory_manager.store.root.is_relative_to(memory_root))
        self.assertFalse(agent.memory_manager.store.root.is_relative_to(self.workspace))
        self.assertTrue(agent.context.config.transcript_dir.is_relative_to(data_dir))

        result = json.loads(
            self.tool.run(
                baseCommit=self.base_commit,
                plan=self._plan(),
                teammateCount=2,
                maxTeammates=2,
            )
        )
        blocked = agent.tools.execute(
            "remember",
            {
                "name": "During Team",
                "description": "Must be blocked.",
                "content": "blocked",
                "type": "project",
            },
        )
        self.assertIn("read-only while an Agent Team is active", blocked)
        blocked_write = agent.tools.execute(
            "write_file",
            {
                "file_path": "must-not-be-created.txt",
                "content": "blocked\n",
            },
        )
        self.assertIn("Root/Lead is read-only", blocked_write)
        self.assertFalse((self.workspace / "must-not-be-created.txt").exists())
        self.assertFalse((self.workspace / ".memory").exists())

        self.repository.cancel_team_run(
            result["team_run_id"],
            cancelled_by="user",
            reason="restore memory",
            command_id="cancel-prebuilt-root-memory-test",
        )
        restored = agent.tools.execute(
            "remember",
            {
                "name": "After Team",
                "description": "Writes are restored.",
                "content": "restored",
                "type": "project",
            },
        )
        self.assertIn("[memory saved] After Team", restored)
        write_restored = agent.tools.execute(
            "write_file",
            {
                "file_path": "ordinary-agent.txt",
                "content": "ordinary Agent writes are restored\n",
            },
        )
        self.assertIn("Wrote", write_restored)
        self.assertTrue((self.workspace / "ordinary-agent.txt").is_file())

if __name__ == "__main__":
    unittest.main()
