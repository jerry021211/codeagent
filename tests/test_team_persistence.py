from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from codeagent.teams import (
    AgentSessionState,
    DependencyRequirement,
    TaskAttemptState,
    TeamPlanStatus,
    TeamRunState,
)
from codeagent.web.storage import (
    InvalidStateTransitionError,
    SQLiteRepository,
    StorageConflictError,
)


class TeamPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.db"
        self.repository = SQLiteRepository(
            self.database,
            recover_incomplete=False,
        )
        self.conversation = self.repository.create_conversation(
            title="Agent Team",
            workspace=self.temp_dir.name,
        )
        self.run = self.repository.create_run(
            self.conversation.id,
            status="running",
        )
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id
        self.team = self.repository.create_team_run(
            conversation_id=self.conversation.id,
            root_run_id=self.run.id,
            task_list_id=self.task_list_id,
            base_commit="a" * 40,
            team_run_id="team_1",
            lead_agent_id="agent_lead",
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def _create_plan(self, suffix: str = "1"):
        plan = self.repository.create_team_plan_revision(
            self.team.id,
            plan={
                "base_commit": "a" * 40,
                "tasks": [{"title": f"Task {suffix}", "write_scopes": ["codeagent/"]}],
            },
            created_by=self.team.lead_agent_id,
            command_id=f"create-plan-{suffix}",
        )
        return self.repository.submit_team_plan_revision(
            self.team.id,
            plan.revision,
            command_id=f"submit-plan-{suffix}",
        )

    def _approve_plan(self):
        plan = self._create_plan()
        return self.repository.decide_team_plan_revision(
            self.team.id,
            plan.revision,
            decision="approve",
            decided_by="user",
            reason="approved for test",
            command_id="approve-plan-1",
        )

    def _create_teammate(self, suffix: str):
        agent = self.repository.create_team_agent(
            self.team.id,
            name=f"Teammate {suffix}",
            agent_id=f"agent_{suffix}",
        )
        session = self.repository.create_agent_session(
            self.team.id,
            agent.id,
            session_id=f"session_{suffix}",
        )
        return agent, session

    def test_analysis_write_scope_rejected_at_submit_approve_and_claim(self) -> None:
        task = self.repository.create_task(
            self.task_list_id, subject="Document", description="Write a document",
            metadata={"kind": "analysis", "write_scopes": ["docs/"]},
        )
        plan = self.repository.create_team_plan_revision(
            self.team.id, plan={"tasks": [{"task_id": task.task.id}]},
            created_by=self.team.lead_agent_id, command_id="bad-plan",
        )
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot"):
            self.repository.submit_team_plan_revision(
                self.team.id, plan.revision, command_id="bad-submit",
            )
        self.repository.update_task(self.task_list_id, task.task.id,
                                    changes={"metadata": {"kind": "analysis", "write_scopes": []}})
        self.repository.submit_team_plan_revision(self.team.id, plan.revision, command_id="good-submit")
        self.repository.update_task(self.task_list_id, task.task.id,
                                    changes={"metadata": {"kind": "analysis", "write_scopes": ["docs/"]}})
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot"):
            self.repository.decide_team_plan_revision(
                self.team.id, plan.revision, decision="approve", decided_by="user",
                reason="test", command_id="bad-approve",
            )
        self.repository.update_task(self.task_list_id, task.task.id,
                                    changes={"metadata": {"kind": "analysis", "write_scopes": []}})
        self.repository.decide_team_plan_revision(
            self.team.id, plan.revision, decision="approve", decided_by="user",
            reason="test", command_id="good-approve",
        )
        changed = self.repository.update_task(
            self.task_list_id, task.task.id,
            changes={"metadata": {"kind": "analysis", "write_scopes": ["docs/"]}},
        )
        agent, session = self._create_teammate("bad-analysis")
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot"):
            self.repository.claim_task_attempt(
                self.team.id, task_id=task.task.id, agent_id=agent.id, session_id=session.id,
                expected_task_revision=changed.revision, command_id="bad-claim",
            )
        self.assertEqual(self.repository.list_task_attempts(self.team.id), [])
        self.assertEqual(self.repository.list_resource_leases(self.team.id), [])
        self.assertIn("task_configuration_conflict", self.repository.list_task_scheduling(self.team.id)[0].reasons)

    def test_project_memory_stays_read_only_until_team_is_terminal(self) -> None:
        other_workspace = Path(self.temp_dir.name) / "other"

        self.assertTrue(
            self.repository.has_active_team_run_for_workspace(self.temp_dir.name)
        )
        self.assertFalse(
            self.repository.has_active_team_run_for_workspace(other_workspace)
        )

        self.repository.cancel_team_run(
            self.team.id,
            cancelled_by="user",
            reason="done",
            command_id="cancel-for-memory-policy",
        )

        self.assertFalse(
            self.repository.has_active_team_run_for_workspace(self.temp_dir.name)
        )

    def test_claim_derives_approval_mode_and_rejects_caller_overrides_atomically(self) -> None:
        self._approve_plan()
        for index, (metadata, expected, wrong) in enumerate([
            ({"kind": "analysis"}, TaskAttemptState.RUNNING, True),
            ({"kind": "code", "risk_level": "high"}, TaskAttemptState.PLAN_REQUIRED, False),
            ({"kind": "code", "plan_required": True}, TaskAttemptState.PLAN_REQUIRED, False),
        ]):
            with self.subTest(metadata=metadata):
                # Separate scopes avoid intentionally conflicting repository-wide leases.
                if metadata["kind"] == "code":
                    metadata["write_scopes"] = [f"part{index}/"]
                task = self.repository.create_task(
                    self.task_list_id, subject=f"Approval {index}", description="Objective",
                    metadata=metadata,
                )
                agent, session = self._create_teammate(f"approval-{index}")
                kwargs = dict(task_id=task.task.id, agent_id=agent.id, session_id=session.id,
                              expected_task_revision=task.revision, command_id=f"mode-{index}")
                count = len(self.repository.list_task_attempts(self.team.id))
                leases = self.repository.list_resource_leases(self.team.id)
                with self.assertRaisesRegex(StorageConflictError, "differs from Task metadata"):
                    self.repository.claim_task_attempt(self.team.id, **kwargs, plan_required=wrong)
                self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), count)
                self.assertEqual(self.repository.list_resource_leases(self.team.id), leases)
                self.assertEqual(self.repository.get_agent_session(session.id).state, AgentSessionState.IDLE)
                attempt = self.repository.claim_task_attempt(self.team.id, **kwargs)
                self.assertEqual(attempt.state, expected)
                self.assertFalse(attempt.write_enabled)
                message = self.repository.fetch_unacked_team_messages(session.id)[0]
                self.assertEqual(message.payload["plan_required"], expected is TaskAttemptState.PLAN_REQUIRED)

    def test_invalid_legacy_metadata_is_not_claimed(self) -> None:
        self._approve_plan()
        for index, metadata in enumerate([
            {"kind": "analysis", "plan_required": True},
            {"kind": "analysis", "plan_required": "true"},
            {"kind": "code", "plan_required": "false"},
        ]):
            task = self.repository.create_task(
                self.task_list_id, subject=f"Legacy {index}", description="Existing task",
                metadata=metadata,
            )
            agent, session = self._create_teammate(f"legacy-{index}")
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.repository.claim_task_attempt(
                    self.team.id, task_id=task.task.id, agent_id=agent.id, session_id=session.id,
                    expected_task_revision=task.revision, command_id=f"legacy-claim-{index}",
                )
        self.assertEqual(self.repository.list_task_attempts(self.team.id), [])
        self.assertEqual(self.repository.list_resource_leases(self.team.id), [])

    def test_restart_recreates_lead_session_even_before_attempts_exist(self) -> None:
        original = self.repository.list_agent_sessions(
            self.team.id, role="lead"
        )[0]

        self.repository.close()
        self.repository = SQLiteRepository(
            self.database,
            recover_incomplete=True,
        )

        sessions = self.repository.list_agent_sessions(self.team.id, role="lead")
        active = [
            session
            for session in sessions
            if session.state not in {
                AgentSessionState.LOST,
                AgentSessionState.FAILED,
                AgentSessionState.SHUTDOWN,
            }
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].state, AgentSessionState.IDLE)
        self.assertEqual(active[0].generation, original.generation + 1)

    def test_conversation_cannot_have_two_active_team_runs(self) -> None:
        self.repository.update_run_status(self.run.id, "completed")
        second_run = self.repository.create_run(
            self.conversation.id, status="running"
        )

        with self.assertRaisesRegex(
            StorageConflictError, "already has an active TeamRun"
        ):
            self.repository.create_team_run(
                conversation_id=self.conversation.id,
                root_run_id=second_run.id,
                task_list_id=self.task_list_id,
                base_commit="b" * 40,
                team_run_id="team_2",
                lead_agent_id="agent_lead_2",
            )

    def test_team_plan_rejection_is_immutable_idempotent_and_returns_to_planning(self) -> None:
        submitted = self._create_plan()
        self.assertEqual(submitted.status, TeamPlanStatus.PENDING_USER_APPROVAL)
        self.assertEqual(
            self.repository.get_team_run(self.team.id).state,
            TeamRunState.WAITING_APPROVAL,
        )

        rejected = self.repository.decide_team_plan_revision(
            self.team.id,
            submitted.revision,
            decision="reject",
            decided_by="user",
            reason="scope is too broad",
            command_id="reject-plan-1",
        )
        repeated = self.repository.decide_team_plan_revision(
            self.team.id,
            submitted.revision,
            decision="reject",
            decided_by="user",
            reason="scope is too broad",
            command_id="reject-plan-1",
        )

        self.assertEqual(rejected, repeated)
        self.assertEqual(rejected.status, TeamPlanStatus.REJECTED)
        self.assertEqual(rejected.decided_by, "user")
        self.assertEqual(rejected.decision_reason, "scope is too broad")
        team = self.repository.get_team_run(self.team.id)
        self.assertEqual(team.state, TeamRunState.PLANNING)
        self.assertIsNone(team.active_plan_revision)
        self.assertEqual(len(self.repository.list_team_plan_revisions(self.team.id)), 1)
        rejection_events = [
            event
            for event in self.repository.list_events(self.run.id)
            if event.type == "team.plan.rejected"
        ]
        self.assertEqual(len(rejection_events), 1)

        with self.assertRaises(InvalidStateTransitionError):
            self.repository.decide_team_plan_revision(
                self.team.id,
                submitted.revision,
                decision="approve",
                decided_by="user",
                reason="changed mind",
                command_id="approve-rejected-plan",
            )

    def test_rejected_plan_and_unapproved_r2_cannot_create_attempt(self) -> None:
        task = self.repository.create_task(
            self.task_list_id,
            subject="Implement",
            description="Implement the requested change",
        )
        agent, session = self._create_teammate("one")
        first = self._create_plan()
        self.repository.decide_team_plan_revision(
            self.team.id,
            first.revision,
            decision="reject",
            decided_by="user",
            reason="revise it",
            command_id="reject-plan-1",
        )

        with self.assertRaises(InvalidStateTransitionError):
            self.repository.claim_task_attempt(
                self.team.id,
                task_id=task.task.id,
                agent_id=agent.id,
                session_id=session.id,
                expected_task_revision=task.revision,
                command_id="claim-before-r2",
            )

        second = self._create_plan("2")
        self.assertEqual(second.status, TeamPlanStatus.PENDING_USER_APPROVAL)
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.claim_task_attempt(
                self.team.id,
                task_id=task.task.id,
                agent_id=agent.id,
                session_id=session.id,
                expected_task_revision=task.revision,
                command_id="claim-unapproved-r2",
            )
        self.assertEqual(self.repository.list_task_attempts(self.team.id), [])

        self.repository.decide_team_plan_revision(
            self.team.id,
            second.revision,
            decision="approve",
            decided_by="user",
            reason="scope fixed",
            command_id="approve-plan-2",
        )
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id="claim-approved-r2",
        )
        self.assertEqual(attempt.team_plan_revision, second.revision)
        self.assertEqual(attempt.state, TaskAttemptState.RUNNING)
        self.assertFalse(attempt.write_enabled)

    def test_claim_is_idempotent_and_enforces_one_active_attempt_per_agent(self) -> None:
        self._approve_plan()
        first = self.repository.create_task(
            self.task_list_id, subject="First", description="First task"
        )
        second = self.repository.create_task(
            self.task_list_id, subject="Second", description="Second task"
        )
        agent, session = self._create_teammate("one")

        claimed = self.repository.claim_task_attempt(
            self.team.id,
            task_id=first.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=first.revision,
            command_id="claim-first",
        )
        repeated = self.repository.claim_task_attempt(
            self.team.id,
            task_id=first.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=first.revision,
            command_id="claim-first",
        )
        self.assertEqual(claimed, repeated)
        self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)
        self.assertEqual(len(self.repository.list_team_messages(self.team.id)), 1)
        self.assertEqual(
            self.repository.get_agent_session(session.id).state,
            AgentSessionState.WORK,
        )

        with self.assertRaises(StorageConflictError):
            self.repository.claim_task_attempt(
                self.team.id,
                task_id=second.task.id,
                agent_id=agent.id,
                session_id=session.id,
                expected_task_revision=second.revision,
                command_id="claim-second",
            )

    def test_concurrent_claims_create_only_one_active_attempt_for_task(self) -> None:
        self._approve_plan()
        task = self.repository.create_task(
            self.task_list_id, subject="Shared", description="Claim once"
        )
        first_agent, first_session = self._create_teammate("one")
        second_agent, second_session = self._create_teammate("two")
        barrier = threading.Barrier(2)
        attempts = []
        errors = []
        result_lock = threading.Lock()

        def claim(agent_id: str, session_id: str, command_id: str) -> None:
            local = SQLiteRepository(self.database, recover_incomplete=False)
            try:
                barrier.wait()
                result = local.claim_task_attempt(
                    self.team.id,
                    task_id=task.task.id,
                    agent_id=agent_id,
                    session_id=session_id,
                    expected_task_revision=task.revision,
                    command_id=command_id,
                )
                with result_lock:
                    attempts.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            finally:
                local.close()

        threads = [
            threading.Thread(
                target=claim,
                args=(first_agent.id, first_session.id, "claim-one"),
            ),
            threading.Thread(
                target=claim,
                args=(second_agent.id, second_session.id, "claim-two"),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StorageConflictError)
        self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)

    def test_dependency_requirement_extends_existing_dag_without_ready_state(self) -> None:
        blocker = self.repository.create_task(
            self.task_list_id, subject="Blocker", description="First"
        )
        blocked = self.repository.create_task(
            self.task_list_id,
            subject="Blocked",
            description="Second",
            blocked_by=[blocker.task.id],
        )
        self.assertEqual(
            self.repository.get_task_dependency_requirement(
                self.task_list_id,
                blocker_id=blocker.task.id,
                blocked_id=blocked.task.id,
            ),
            DependencyRequirement.TASK_COMPLETED,
        )
        self.repository.set_task_dependency_requirement(
            self.task_list_id,
            blocker_id=blocker.task.id,
            blocked_id=blocked.task.id,
            requirement=DependencyRequirement.CANDIDATE_INTEGRATED.value,
        )
        self.assertEqual(
            self.repository.get_task_dependency_requirement(
                self.task_list_id,
                blocker_id=blocker.task.id,
                blocked_id=blocked.task.id,
            ),
            DependencyRequirement.CANDIDATE_INTEGRATED,
        )


if __name__ == "__main__":
    unittest.main()
