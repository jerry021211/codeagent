from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from codeagent import Agent, AgentConfig, ModelResponse
from codeagent.runtime import TeamSupervisor
from codeagent.teams import (
    AgentSessionState,
    MessageBus,
    ReadOnlyTeamToolExecutionGate,
    TaskAttemptState,
    create_teammate_tools,
)
from codeagent.tools import ToolRegistry, WorkspaceGuard, WriteFileTool
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from codeagent.teams.tools import TeamAnswerQuestionTool


class _BarrierClient:
    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier

    def create_message(self, **_kwargs):
        self.barrier.wait(timeout=2)
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": "result"}],
        )


class _BlockingClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def create_message(self, **_kwargs):
        self.started.set()
        self.release.wait(timeout=5)
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": "released"}],
        )


class TeamSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.db"
        self.repository = SQLiteRepository(self.database, recover_incomplete=False)
        self.conversation = self.repository.create_conversation(
            title="Supervisor",
            workspace=self.temp_dir.name,
        )
        self.run = self.repository.create_run(self.conversation.id, status="running")
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id
        self.team = self.repository.create_team_run(
            conversation_id=self.conversation.id,
            root_run_id=self.run.id,
            task_list_id=self.task_list_id,
            base_commit="c" * 40,
            team_run_id="team_supervisor",
            lead_agent_id="agent_lead",
            max_teammates=3,
        )
        plan = self.repository.create_team_plan_revision(
            self.team.id,
            plan={"base_commit": "c" * 40, "tasks": ["parallel"]},
            created_by="agent_lead",
            command_id="create-plan",
        )
        self.repository.submit_team_plan_revision(
            self.team.id, plan.revision, command_id="submit-plan"
        )
        self.repository.decide_team_plan_revision(
            self.team.id,
            plan.revision,
            decision="approve",
            decided_by="user",
            reason="approved",
            command_id="approve-plan",
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def _task(self, suffix: str, *, kind: str = "analysis", metadata=None):
        values = {"kind": kind, "risk_level": "low"}
        values.update(metadata or {})
        return self.repository.create_task(
            self.task_list_id,
            subject=f"Task {suffix}",
            description=f"Objective {suffix}",
            metadata=values,
        )

    def _teammate(self, suffix: str):
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

    @staticmethod
    def _agent(client, cancellation):
        return Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake"),
            cancellation=cancellation,
            allow_subagents=False,
        )

    def _wait_for_supervisor(self, supervisor: TeamSupervisor, timeout: float = 3) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            supervisor.dispatch_once(self.team.id)
            if not supervisor.active_attempt_ids():
                return
            time.sleep(0.01)
        self.fail("Supervisor workers did not finish")

    def test_two_analysis_tasks_execute_concurrently_with_bounded_workers(self) -> None:
        self._task("one")
        self._task("two")
        self._teammate("one")
        self._teammate("two")
        barrier = threading.Barrier(2)

        def builder(_session, _attempt, cancellation):
            return self._agent(_BarrierClient(barrier), cancellation)

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            max_workers=2,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 2)
            self._wait_for_supervisor(supervisor)
            attempts = self.repository.list_task_attempts(self.team.id)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(
                {attempt.state for attempt in attempts},
                {TaskAttemptState.WAITING},
            )
        finally:
            supervisor.stop()

    def test_question_answer_restarts_worker_with_same_attempt_and_context(self) -> None:
        self._task("question")
        self._teammate("question")
        self.repository.create_agent_session(self.team.id, self.team.lead_agent_id)
        calls = []

        class Client:
            def create_message(self, **kwargs):
                messages = str(kwargs["messages"])
                calls.append(messages)
                if "Analysis result submitted" in messages:
                    return ModelResponse("end_turn", [{"type": "text", "text": "done"}])
                if "Use Decimal" in messages:
                    return ModelResponse("tool_use", [{"type": "tool_use", "id": "report",
                        "name": "team_submit_analysis_result", "input": {"summary": "Use Decimal; report only."}}])
                return ModelResponse("tool_use", [{"type": "tool_use", "id": "question",
                    "name": "team_ask_lead", "input": {"question": "Which amount type?", "blocking": True}}])

        def builder(_session, attempt, cancellation):
            agent = self._agent(Client(), cancellation)
            for tool in create_teammate_tools(
                self.repository, None, attempt, task_kind="analysis",
                yield_callback=agent.request_yield,
            ):
                agent.tools.register(tool)
            return agent

        supervisor = TeamSupervisor(self.repository, builder, enabled=True, max_workers=1)
        try:
            supervisor.dispatch_once(self.team.id)
            self._wait_for_supervisor(supervisor)
            attempt = self.repository.list_task_attempts(self.team.id)[0]
            question = next(m for m in self.repository.list_team_messages(self.team.id) if m.type == "QUESTION")
            TeamAnswerQuestionTool(self.repository, self.team.id, self.team.lead_agent_id).run(
                question.id, "Use Decimal", False,
            )
            supervisor.dispatch_once(self.team.id)
            self._wait_for_supervisor(supervisor)
            attempts = self.repository.list_task_attempts(self.team.id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].id, attempt.id)
            self.assertEqual(attempts[0].state, TaskAttemptState.SUCCEEDED)
            self.assertIn("Which amount type?", calls[-1])
            self.assertIn("Objective question", calls[-1])
            self.assertIn("Use Decimal", calls[-1])
        finally:
            supervisor.stop()

    def test_missing_terminal_team_tool_is_corrected_once_then_paused(self) -> None:
        self._task("protocol")
        self._teammate("protocol")
        calls: list[int] = []

        class _Client:
            def create_message(self, **_kwargs):
                calls.append(1)
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done without protocol tool"}],
                )

        supervisor = TeamSupervisor(
            self.repository,
            lambda _session, _attempt, cancellation: self._agent(
                _Client(), cancellation
            ),
            enabled=True,
            max_workers=1,
        )
        try:
            supervisor.dispatch_once(self.team.id)
            self._wait_for_supervisor(supervisor)
            attempts = self.repository.list_task_attempts(self.team.id)
            self.assertEqual(len(attempts), 1)
            attempt = attempts[0]
            self.assertEqual(attempt.state, TaskAttemptState.WAITING)
            self.assertEqual(len(calls), 2)
            session = self.repository.get_agent_session(attempt.session_id)
            self.assertEqual(session.waiting_reason, "protocol_incomplete")
            corrections = [
                item
                for item in self.repository.list_team_messages(self.team.id)
                if item.dedupe_key == f"protocol-correction:{attempt.id}:1"
            ]
            self.assertEqual(len(corrections), 1)
            self.assertEqual(corrections[0].type, "SYSTEM_ERROR")
        finally:
            supervisor.stop()

    def test_protocol_correction_does_not_prescribe_a_code_tool_to_analysis(self) -> None:
        task = self._task("legacy-protocol")
        agent, session = self._teammate("legacy-protocol")
        attempt = self.repository.claim_task_attempt(
            self.team.id, task_id=task.task.id, agent_id=agent.id, session_id=session.id,
            expected_task_revision=task.revision, command_id="legacy-protocol-claim",
        )
        supervisor = TeamSupervisor(self.repository, lambda *_: None, enabled=True)
        try:
            for invalid in (
                replace(attempt, state=TaskAttemptState.PLAN_REQUIRED),
                replace(attempt, write_enabled=True),
                replace(attempt, state=TaskAttemptState.WAITING),
                replace(attempt, result_unknown=True),
            ):
                self.assertFalse(supervisor._request_protocol_correction(invalid))
            self.assertFalse(any(
                message.type == "SYSTEM_ERROR"
                for message in self.repository.list_team_messages(self.team.id)
            ))
            self.assertTrue(supervisor._request_protocol_correction(attempt))
            correction = next(
                message for message in self.repository.list_team_messages(self.team.id)
                if message.type == "SYSTEM_ERROR"
            )
            self.assertIn("team_submit_analysis_result", correction.payload["reason"])
            self.assertNotIn("team_submit_attempt_plan", correction.payload["reason"])
        finally:
            supervisor.stop()

    def test_invalid_task_configuration_is_not_dispatched(self) -> None:
        self._task("invalid-analysis", metadata={"plan_required": True})
        self._teammate("invalid-analysis")
        built = []
        supervisor = TeamSupervisor(
            self.repository, lambda *_: built.append(True), enabled=True,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 0)
            self.assertEqual(built, [])
            self.assertEqual(self.repository.list_task_attempts(self.team.id), [])
            self.assertIn(
                "task_configuration_conflict",
                self.repository.list_task_scheduling(self.team.id)[0].reasons,
            )
        finally:
            supervisor.stop()

    def test_approved_plan_creates_teammates_before_dispatch(self) -> None:
        conversation = self.repository.create_conversation(
            title="Approved participants",
            workspace=self.temp_dir.name,
        )
        run = self.repository.create_run(conversation.id, status="running")
        assert conversation.active_task_list_id is not None
        team = self.repository.create_team_run(
            conversation_id=conversation.id,
            root_run_id=run.id,
            task_list_id=conversation.active_task_list_id,
            base_commit="d" * 40,
            team_run_id="team_approved_participants",
            lead_agent_id="agent_approved_lead",
            max_teammates=2,
        )
        plan = self.repository.create_team_plan_revision(
            team.id,
            plan={
                "base_commit": "d" * 40,
                "tasks": [],
                "teammate_count": 2,
            },
            created_by=team.lead_agent_id,
            command_id="create-approved-participants-plan",
        )
        self.repository.submit_team_plan_revision(
            team.id, plan.revision, command_id="submit-approved-participants-plan"
        )
        self.repository.decide_team_plan_revision(
            team.id,
            plan.revision,
            decision="approve",
            decided_by="user",
            reason="approved",
            command_id="approve-approved-participants-plan",
        )
        supervisor = TeamSupervisor(
            self.repository,
            lambda *_args: self.fail("No worker should run without a Task"),
            enabled=True,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(team.id), 0)
            self.assertEqual(len(self.repository.list_team_agents(team.id)), 3)
            self.assertEqual(
                len(self.repository.list_agent_sessions(team.id, role="teammate")),
                2,
            )
        finally:
            supervisor.stop()

    def test_code_task_is_not_schedulable_before_worktree_milestone(self) -> None:
        task = self._task(
            "code",
            kind="code",
            metadata={"write_scopes": ["codeagent/teams"]},
        )
        self._teammate("one")

        decision = next(
            item
            for item in self.repository.list_task_scheduling(
                self.team.id, allow_code=False
            )
            if item.task_id == task.task.id
        )

        self.assertTrue(decision.dependency_ready)
        self.assertFalse(decision.schedulable)
        self.assertIn("team_write_disabled", decision.reasons)

    def test_analysis_attempt_has_no_worktree_and_submits_a_read_only_result(self) -> None:
        task = self._task("read-only-analysis")
        agent, session = self._teammate("analysis")
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id="claim-read-only-analysis",
        )
        self.assertIsNone(
            self.repository.get_attempt_worktree_binding(attempt.id)
        )

        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(self.temp_dir.name)))
        )
        guarded = ReadOnlyTeamToolExecutionGate(
            self.repository, attempt_id=attempt.id
        ).wrap(registry)
        output = guarded.execute(
            "write_file",
            {"file_path": "forbidden.txt", "content": "must not exist\n"},
        )
        self.assertIn("read-only repository access", output)
        self.assertFalse((Path(self.temp_dir.name) / "forbidden.txt").exists())

        result_tool = create_teammate_tools(
            self.repository,
            None,
            attempt,
            task_kind="analysis",
            yield_callback=lambda _reason: None,
        )[0]
        result_tool.run(summary="The requested read-only analysis is complete.")

        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.SUCCEEDED,
        )
        self.assertEqual(
            self.repository.get_agent_session(session.id).state,
            AgentSessionState.IDLE,
        )
        lead_messages = self.repository.list_team_messages(
            self.team.id, recipient_agent_id=self.team.lead_agent_id
        )
        self.assertIn("ANALYSIS_RESULT", [message.type for message in lead_messages])

    def test_overlapping_write_scopes_are_leased_atomically(self) -> None:
        first = self._task(
            "first",
            kind="code",
            metadata={"write_scopes": ["codeagent/teams"]},
        )
        second = self._task(
            "second",
            kind="code",
            metadata={"write_scopes": ["codeagent/teams/messages.py"]},
        )
        first_agent, first_session = self._teammate("one")
        second_agent, second_session = self._teammate("two")
        self.repository.claim_task_attempt(
            self.team.id,
            task_id=first.task.id,
            agent_id=first_agent.id,
            session_id=first_session.id,
            expected_task_revision=first.revision,
            command_id="claim-first",
        )

        with self.assertRaisesRegex(StorageConflictError, "already leased"):
            self.repository.claim_task_attempt(
                self.team.id,
                task_id=second.task.id,
                agent_id=second_agent.id,
                session_id=second_session.id,
                expected_task_revision=second.revision,
                command_id="claim-second",
            )

    def test_cancel_does_not_release_lease_before_worker_exits(self) -> None:
        self._task(
            "blocking",
            metadata={"exclusive_resources": ["service:test"]},
        )
        self._teammate("one")
        started = threading.Event()
        release = threading.Event()

        def builder(_session, _attempt, cancellation):
            return self._agent(_BlockingClient(started, release), cancellation)

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            max_workers=1,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 1)
            self.assertTrue(started.wait(1))
            attempt_id = supervisor.active_attempt_ids()[0]
            supervisor.cancel_attempt(attempt_id, command_id="cancel-blocking")
            self.assertEqual(
                self.repository.list_resource_leases(
                    self.team.id, attempt_id=attempt_id
                )[0].state,
                "active",
            )
            self.assertNotEqual(
                self.repository.get_task_attempt(attempt_id).state,
                TaskAttemptState.CANCELLED,
            )
            release.set()
            self._wait_for_supervisor(supervisor)
            self.assertEqual(
                self.repository.get_task_attempt(attempt_id).state,
                TaskAttemptState.CANCELLED,
            )
            self.assertEqual(
                self.repository.list_resource_leases(
                    self.team.id, attempt_id=attempt_id
                )[0].state,
                "released",
            )
        finally:
            release.set()
            supervisor.stop()

    def test_stale_worker_is_quarantined_until_it_exits(self) -> None:
        self._task(
            "stale",
            metadata={"exclusive_resources": ["service:stale"]},
        )
        self._teammate("one")
        started = threading.Event()
        release = threading.Event()

        def builder(_session, _attempt, cancellation):
            # Stuck outside a declared model/tool operation, not a slow model.
            started.set()
            release.wait(timeout=5)
            return self._agent(_BlockingClient(started, release), cancellation)

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            max_workers=1,
            heartbeat_timeout=0.02,
        )
        try:
            supervisor.dispatch_once(self.team.id)
            self.assertTrue(started.wait(1))
            attempt_id = supervisor.active_attempt_ids()[0]
            time.sleep(0.04)
            supervisor.dispatch_once(self.team.id)
            attempt = self.repository.get_task_attempt(attempt_id)
            self.assertEqual(
                self.repository.get_agent_session(attempt.session_id).state,
                AgentSessionState.SUSPECT,
            )
            self.assertEqual(
                self.repository.list_resource_leases(
                    self.team.id, attempt_id=attempt_id
                )[0].state,
                "active",
            )
            release.set()
            self._wait_for_supervisor(supervisor)
            self.assertEqual(
                self.repository.get_task_attempt(attempt_id).state,
                TaskAttemptState.ORPHANED,
            )
            self.assertEqual(
                self.repository.list_resource_leases(
                    self.team.id, attempt_id=attempt_id
                )[0].state,
                "orphaned",
            )
        finally:
            release.set()
            supervisor.stop()

    def test_long_model_call_is_not_cancelled_by_old_heartbeat_limit(self) -> None:
        self._task("slow-model")
        self._teammate("one")
        started, release = threading.Event(), threading.Event()
        supervisor = TeamSupervisor(
            self.repository,
            lambda _s, _a, token: self._agent(_BlockingClient(started, release), token),
            enabled=True, max_workers=1, heartbeat_timeout=0.02,
        )
        try:
            supervisor.dispatch_once(self.team.id)
            self.assertTrue(started.wait(1))
            time.sleep(0.04)
            supervisor.dispatch_once(self.team.id)
            attempt = self.repository.get_task_attempt(supervisor.active_attempt_ids()[0])
            self.assertEqual(attempt.state, TaskAttemptState.RUNNING)
            self.assertEqual(self.repository.get_agent_session(attempt.session_id).state, AgentSessionState.WORK)
        finally:
            release.set()
            self._wait_for_supervisor(supervisor)
            supervisor.stop()

    def test_model_timeout_retains_checkpoint_and_resumes_without_late_tool(self) -> None:
        self._task("timeout", metadata={"exclusive_resources": ["service:timeout"]})
        self._teammate("one")
        started, release = threading.Event(), threading.Event()
        calls = []

        class Client:
            def create_message(self, **kwargs):
                calls.append(str(kwargs["messages"]))
                if len(calls) > 2:
                    return ModelResponse("end_turn", [{"type": "text", "text": "submitted"}])
                if len(calls) == 1:
                    started.set()
                    release.wait(5)
                return ModelResponse("tool_use", [{
                    "type": "tool_use", "id": "late" if len(calls) == 1 else "resumed",
                    "name": "team_submit_analysis_result", "input": {"summary": "done"},
                }])

        def builder(_session, attempt, token):
            agent = self._agent(Client(), token)
            for tool in create_teammate_tools(
                self.repository, None, attempt, task_kind="analysis", write_enabled=False,
                yield_callback=agent.request_yield,
            ):
                agent.tools.register(tool)
            return agent

        # Analysis completion requires an active Lead.
        self.repository.create_agent_session(self.team.id, self.team.lead_agent_id)
        supervisor = TeamSupervisor(self.repository, builder, enabled=True, max_workers=1,
                                    model_response_timeout=0.03)
        try:
            supervisor.dispatch_once(self.team.id)
            self.assertTrue(started.wait(1))
            attempt_id = supervisor.active_attempt_ids()[0]
            time.sleep(0.05)
            supervisor.dispatch_once(self.team.id)
            with self.assertRaisesRegex(Exception, "still running"):
                supervisor.resume_attempt(attempt_id, resumed_by="user", reason="check", command_id="too-early")
            self.assertEqual(self.repository.list_resource_leases(self.team.id, attempt_id=attempt_id)[0].state, "active")
            release.set()
            self._wait_for_supervisor(supervisor)
            attempt = self.repository.get_task_attempt(attempt_id)
            self.assertEqual(attempt.state, TaskAttemptState.WAITING)
            self.assertFalse(attempt.result_unknown)
            self.assertEqual(attempt.error["type"], "model_response_timeout")
            session = self.repository.get_agent_session(attempt.session_id)
            self.assertEqual(session.state, AgentSessionState.WAITING)
            cp = self.repository.get_latest_agent_session_checkpoint(session.id)
            self.assertEqual(cp.safe_boundary, "model_timeout_before_dispatch")
            self.assertNotIn("'id': 'late'", str(cp.messages))
            from codeagent.web.api import _team_recoveries
            recovery = _team_recoveries(self.repository, attempts=[attempt], sessions=[session], worktrees=[], messages=[])[0]
            self.assertTrue(recovery["recoverable"])
            self.assertEqual(recovery["reason_code"], "model_response_timeout")
            supervisor.resume_attempt(attempt.id, resumed_by="user", reason="checked", command_id="resume-once")
            supervisor.dispatch_once(self.team.id)
            self._wait_for_supervisor(supervisor)
            self.assertEqual(self.repository.get_task_attempt(attempt.id).state, TaskAttemptState.SUCCEEDED)
            self.assertEqual(len(calls), 3)
            self.assertIn("TASK_ASSIGNED", calls[1])
            self.assertIn("ATTEMPT_RESUMED", calls[1])
            self.assertNotIn("'id': 'late'", calls[1])
            self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)
        finally:
            release.set()
            supervisor.stop()

    def test_restart_marks_active_attempt_unknown_without_replay(self) -> None:
        task = self._task(
            "restart",
            metadata={"exclusive_resources": ["service:restart"]},
        )
        agent, session = self._teammate("one")
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id="claim-restart",
        )
        lead_session = self.repository.list_agent_sessions(
            self.team.id, role="lead"
        )[0]
        pending = MessageBus(self.repository).send(
            self.team.id,
            sender_type="runtime",
            recipient_type="lead",
            recipient_agent_id=self.team.lead_agent_id,
            recipient_generation=lead_session.generation,
            message_type="SYSTEM_ERROR",
            payload={
                "reason_code": "restart-test",
                "reason": "Inspect the interrupted Attempt",
                "effective_scope": "attempt",
            },
            dedupe_key="restart-lead-message",
            priority="control",
        )
        self.repository.close()
        self.repository = SQLiteRepository(self.database, recover_incomplete=True)

        recovered = self.repository.get_task_attempt(attempt.id)
        self.assertEqual(recovered.state, TaskAttemptState.WAITING)
        self.assertTrue(recovered.result_unknown)
        self.assertEqual(
            self.repository.get_agent_session(session.id).state,
            AgentSessionState.LOST,
        )
        self.assertEqual(
            self.repository.list_resource_leases(
                self.team.id, attempt_id=attempt.id
            )[0].state,
            "active",
        )
        self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)
        lead_sessions = self.repository.list_agent_sessions(
            self.team.id, role="lead"
        )
        active_leads = [
            item
            for item in lead_sessions
            if item.state not in {
                AgentSessionState.LOST,
                AgentSessionState.FAILED,
                AgentSessionState.SHUTDOWN,
            }
        ]
        self.assertEqual(len(active_leads), 1)
        self.assertEqual(active_leads[0].generation, lead_session.generation + 1)
        recovered_message = self.repository.get_team_message(pending.id)
        self.assertEqual(
            recovered_message.recipient_generation, active_leads[0].generation
        )
        self.assertEqual(
            [item.id for item in self.repository.fetch_unacked_team_messages(
                active_leads[0].id
            )],
            [pending.id],
        )

    def test_supervisor_can_interrupt_foreground_lead_model_wait(self) -> None:
        from codeagent.runtime.activity import ExecutionActivity
        from codeagent.runtime.cancellation import CancellationToken, ModelCallTimeout

        now = [0.0]
        activity = ExecutionActivity(CancellationToken(), clock=lambda: now[0])
        closed = []
        supervisor = TeamSupervisor(
            self.repository, lambda *_: None, enabled=True,
            lead_activity_provider=lambda: (activity,),
        )
        try:
            with self.assertRaises(ModelCallTimeout):
                with activity.model_request():
                    activity.set_request_closer(lambda: closed.append(True))
                    now[0] = 301
                    supervisor._mark_stale_workers()
            self.assertEqual(closed, [True])
            self.assertEqual(self.repository.list_task_attempts(self.team.id), [])
        finally:
            supervisor.stop()

    def test_one_hundred_concurrent_claims_produce_one_attempt(self) -> None:
        task = self._task("contended")
        agents_and_sessions = [self._teammate(str(index)) for index in range(100)]
        barrier = threading.Barrier(100)
        attempts = []
        errors = []
        result_lock = threading.Lock()

        def claim(index: int) -> None:
            agent, session = agents_and_sessions[index]
            try:
                barrier.wait()
                result = self.repository.claim_task_attempt(
                    self.team.id,
                    task_id=task.task.id,
                    agent_id=agent.id,
                    session_id=session.id,
                    expected_task_revision=task.revision,
                    command_id=f"contended-claim-{index}",
                )
                with result_lock:
                    attempts.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(errors), 99)
        self.assertTrue(all(isinstance(error, StorageConflictError) for error in errors))
        self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)


if __name__ == "__main__":
    unittest.main()
