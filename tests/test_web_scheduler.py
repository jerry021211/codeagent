from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from codeagent import Agent, AgentConfig, ModelResponse, ToolRegistry
from codeagent.context import ContextManager
from codeagent.context.models import RuntimeState
from codeagent.events import TokenTotals
from codeagent.prompts import PromptMode
from codeagent.runtime import CancelledError
from codeagent.teams import AgentSessionState, MessageBus
from codeagent.tools import TodoStore
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository


class _FakeAgent:
    def __init__(self, emitter, cancellation, prompt_gate=None) -> None:
        self.emitter = emitter
        self.cancellation = cancellation
        self.prompt_gate = prompt_gate
        self.messages = []
        todos = TodoStore()
        self.context = ContextManager(state=RuntimeState(), todo_store=todos)

    def run(self, prompt):
        self.messages.append({"role": "user", "content": prompt})
        self.emitter.emit("model.text_delta", {"text": "完成"})
        if self.prompt_gate is not None:
            self.prompt_gate.set()
            while not self.cancellation.is_cancelled:
                time.sleep(0.01)
            raise CancelledError(self.cancellation.reason)
        self.messages.append({"role": "assistant", "content": f"答复:{prompt}"})
        return SimpleNamespace(
            final_text=f"答复:{prompt}",
            stop_reason="end_turn",
            usage=TokenTotals(input_tokens=3, output_tokens=2, model_calls=1),
        )


class _FakeFactory:
    def __init__(self, gate=None):
        self.gate = gate
        self.prompt_modes = []

    def create(
        self,
        *,
        event_emitter,
        cancellation,
        permission_broker,
        checkpoint=None,
        root_prompt_mode=None,
    ):
        del permission_broker, checkpoint
        self.prompt_modes.append(root_prompt_mode)
        return _FakeAgent(event_emitter, cancellation, self.gate)


class _WorkspaceFactory(_FakeFactory):
    def __init__(self, selected=None):
        super().__init__()
        self.selected = selected if selected is not None else []

    def for_workspace(self, workspace):
        self.selected.append(workspace)
        return _WorkspaceFactory(self.selected)


class _ApprovalAgent(_FakeAgent):
    def __init__(self, emitter, cancellation, broker):
        super().__init__(emitter, cancellation)
        self.broker = broker

    def run(self, prompt):
        del prompt
        allowed = self.broker.request(
            "bash",
            {"command": "Remove-Item temp.txt"},
            "Potentially destructive command",
            cancellation=self.cancellation,
            timeout=2,
        )
        return SimpleNamespace(
            final_text="已允许" if allowed else "已拒绝",
            stop_reason="end_turn",
            usage=TokenTotals(),
        )


class _ApprovalFactory:
    def create(self, *, event_emitter, cancellation, permission_broker, checkpoint=None):
        del checkpoint
        return _ApprovalAgent(event_emitter, cancellation, permission_broker)


class _TeamLeadClient:
    def __init__(self, calls) -> None:
        self.calls = calls

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": "Lead 已处理团队指令"}],
        )


class _TeamAwareFactory(_FakeFactory):
    def __init__(self) -> None:
        super().__init__()
        self.team_calls = []

    def for_workspace(self, _workspace):
        return self

    def create(
        self,
        *,
        event_emitter,
        cancellation,
        permission_broker,
        checkpoint=None,
        team_session=None,
        team_attempt=None,
        worktree_manager=None,
        root_prompt_mode=None,
    ):
        del permission_broker, team_attempt, worktree_manager, root_prompt_mode
        if team_session is None:
            return _FakeAgent(event_emitter, cancellation)
        self.assert_no_root_checkpoint(checkpoint)
        return Agent(
            client=_TeamLeadClient(self.team_calls),
            tools=ToolRegistry(),
            config=AgentConfig(model="fake"),
            context=ContextManager(),
            event_emitter=event_emitter,
            cancellation=cancellation,
            allow_subagents=False,
        )

    @staticmethod
    def assert_no_root_checkpoint(checkpoint) -> None:
        if checkpoint is not None:
            raise AssertionError("Lead Session must not reuse the Root checkpoint")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tempdir.name) / "state.db")
        self.conversation = self.repository.create_conversation(title="测试")

    def tearDown(self):
        self.repository.close()
        self.tempdir.cleanup()

    def test_submit_runs_and_persists_replay_and_checkpoint(self):
        scheduler = RunScheduler(self.repository, _FakeFactory())
        run = scheduler.submit(self.conversation.id, "你好")
        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.repository.get_run(run.id)
            if current and current.status == "completed":
                break
            time.sleep(0.01)
        scheduler.stop()

        self.assertEqual(self.repository.get_run(run.id).status, "completed")
        self.assertEqual(
            [item.role for item in self.repository.list_messages(self.conversation.id)],
            ["user", "assistant"],
        )
        event_types = [item.type for item in self.repository.list_events(run.id)]
        self.assertIn("run.queued", event_types)
        self.assertIn("run.started", event_types)
        self.assertIn("run.completed", event_types)
        self.assertIsNotNone(self.repository.get_checkpoint_for_run(run.id))

    def test_discuss_profile_is_snapshotted_and_next_run_can_exit(self):
        factory = _FakeFactory()
        scheduler = RunScheduler(self.repository, factory)
        try:
            for mode in ("discuss", "normal"):
                run = scheduler.submit(self.conversation.id, "inspect", mode=mode)
                deadline = time.time() + 3
                while time.time() < deadline:
                    current = self.repository.get_run(run.id)
                    if current.status in {"completed", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(current.status, "completed")
                self.assertEqual(current.metadata["agent_profile"], mode)
            users = [m for m in self.repository.list_messages(self.conversation.id) if m.role == "user"]
            self.assertEqual([m.metadata["mode"] for m in users], ["discuss", "normal"])
            self.assertEqual(factory.prompt_modes, [PromptMode.DISCUSS, None])
        finally:
            scheduler.stop()

    def test_discuss_rejects_team_before_creating_run(self):
        scheduler = RunScheduler(self.repository, _FakeFactory())
        with self.assertRaises(ValueError):
            scheduler.submit(self.conversation.id, "inspect", mode="discuss", use_team=True)
        self.assertEqual(self.repository.list_runs(conversation_id=self.conversation.id), [])

    def test_run_uses_the_workspace_bound_to_its_conversation(self):
        workspace = str(Path(self.tempdir.name, "project").resolve())
        conversation = self.repository.create_conversation(
            title="Project",
            workspace=workspace,
        )
        factory = _WorkspaceFactory()
        scheduler = RunScheduler(self.repository, factory)
        run = scheduler.submit(conversation.id, "inspect")
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.repository.get_run(run.id).status == "completed":
                break
            time.sleep(0.01)
        scheduler.stop()
        self.assertEqual(factory.selected, [workspace])

    def test_explicit_team_run_selects_planner_before_first_model_call(self):
        factory = _FakeFactory()
        scheduler = RunScheduler(self.repository, factory)
        run = scheduler.submit(self.conversation.id, "plan with a team", use_team=True)
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.repository.get_run(run.id).status in {"completed", "failed"}:
                break
            time.sleep(0.01)
        scheduler.stop()

        self.assertEqual(factory.prompt_modes, [PromptMode.TEAM_PLANNER])
        persisted = self.repository.get_run(run.id)
        self.assertEqual(persisted.status, "failed")
        self.assertEqual(persisted.metadata["requested_mode"], "team")
        self.assertEqual(persisted.metadata["agent_profile"], "team_planner")
        selected = [
            event for event in self.repository.list_events(run.id)
            if event.type == "agent.profile.selected"
        ]
        self.assertEqual(selected[0].payload["profile"], "team_planner")
        self.assertTrue(
            any(
                event.type == "team.plan.not_submitted"
                for event in self.repository.list_events(run.id)
            )
        )

    def test_active_team_routes_chat_to_durable_lead_session(self):
        factory = _TeamAwareFactory()
        scheduler = RunScheduler(self.repository, factory)
        ordinary = scheduler.submit(self.conversation.id, "普通单 Agent 回合")
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.repository.get_run(ordinary.id).status == "completed":
                break
            time.sleep(0.01)
        ordinary_checkpoint = self.repository.get_latest_checkpoint(
            self.conversation.id
        )
        self.assertIsNotNone(ordinary_checkpoint)
        assert self.conversation.active_task_list_id is not None
        team = self.repository.create_team_run(
            conversation_id=self.conversation.id,
            root_run_id=ordinary.id,
            task_list_id=self.conversation.active_task_list_id,
            base_commit="a" * 40,
            team_run_id="team_chat_route",
            lead_agent_id="agent_chat_lead",
        )
        original_lead = self.repository.list_agent_sessions(
            team.id, role="lead"
        )[0]
        self.repository.transition_agent_session(
            original_lead.id, AgentSessionState.LOST.value
        )
        recovered_lead = self.repository.create_agent_session(
            team.id, team.lead_agent_id
        )
        scheduler.configure_team_runtime(object())

        lead_run = scheduler.submit(self.conversation.id, "请汇报 Team 当前状态")
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.repository.get_run(lead_run.id).status == "completed":
                break
            time.sleep(0.01)
        scheduler.stop()

        self.assertEqual(self.repository.get_run(lead_run.id).status, "completed")
        self.assertEqual(len(factory.team_calls), 1)
        self.assertIn("USER_INSTRUCTION", str(factory.team_calls[0]["messages"]))
        lead_messages = self.repository.list_team_messages(
            team.id, recipient_agent_id=team.lead_agent_id
        )
        self.assertEqual([message.type for message in lead_messages], ["USER_INSTRUCTION"])
        self.assertEqual(
            lead_messages[0].recipient_generation, recovered_lead.generation
        )
        self.assertIsNotNone(lead_messages[0].acked_at)
        self.assertIsNone(self.repository.get_checkpoint_for_run(lead_run.id))
        latest = self.repository.get_latest_checkpoint(self.conversation.id)
        self.assertEqual(latest.id, ordinary_checkpoint.id)
        assistant = self.repository.list_messages(self.conversation.id)[-1]
        self.assertEqual(assistant.metadata["agent_role"], "lead")

    def test_lead_model_timeout_retains_context_and_allows_new_user_turn(self):
        from codeagent.runtime.activity import ExecutionActivity
        from codeagent.runtime.cancellation import ModelCallTimeout

        now = [0.0]
        calls = []

        class Client:
            def create_message(inner, **kwargs):
                calls.append(kwargs)
                self.assertEqual(len(scheduler.team_lead_activities()), 1)
                if len(calls) == 1:
                    now[0] += 6
                return ModelResponse("end_turn", [{"type": "text", "text": "done"}])

        class Factory(_TeamAwareFactory):
            def create(inner, **kwargs):
                agent = super().create(**kwargs)
                agent.client = Client()
                agent.set_execution_activity(ExecutionActivity(
                    kwargs["cancellation"], response_timeout=5, clock=lambda: now[0],
                ))
                return agent

        scheduler = RunScheduler(self.repository, Factory())
        root_run = self.repository.create_run(self.conversation.id, status="completed")
        team = self.repository.create_team_run(
            conversation_id=self.conversation.id, root_run_id=root_run.id,
            task_list_id=self.conversation.active_task_list_id,
            base_commit="b" * 40, lead_agent_id="lead_timeout",
        )
        session = self.repository.list_agent_sessions(team.id, role="lead")[0]
        MessageBus(self.repository).send(
            team.id, sender_type="runtime", recipient_type="lead",
            recipient_agent_id=team.lead_agent_id, recipient_generation=session.generation,
            message_type="SYSTEM_ERROR", payload={
                "reason": "original context", "reason_code": "test_context",
                "effective_scope": "team",
            },
            dedupe_key="lead-timeout-context",
        )
        scheduler.configure_team_runtime(object())
        try:
            with self.assertRaises(ModelCallTimeout):
                scheduler.run_team_lead_cycle(team.id)
            current = self.repository.get_agent_session(session.id)
            self.assertEqual(current.state, AgentSessionState.WAITING)
            self.assertEqual(current.waiting_reason, "model_response_timeout")
            self.assertEqual(scheduler.team_lead_activities(), ())
            checkpoint = self.repository.get_latest_agent_session_checkpoint(session.id)
            self.assertEqual(checkpoint.safe_boundary, "model_timeout_before_dispatch")

            run = scheduler.submit(self.conversation.id, "继续汇报")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.repository.get_run(run.id).status in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(self.repository.get_run(run.id).status, "completed")
            self.assertEqual(len(calls), 2)
            self.assertIn("original context", str(calls[-1]["messages"]))
            self.assertIn("USER_INSTRUCTION", str(calls[-1]["messages"]))
        finally:
            scheduler.stop()

    def test_lead_text_without_required_decision_is_not_treated_as_review(self):
        factory = _TeamAwareFactory()
        scheduler = RunScheduler(self.repository, factory)
        root_run = self.repository.create_run(
            self.conversation.id, status="completed"
        )
        assert self.conversation.active_task_list_id is not None
        team = self.repository.create_team_run(
            conversation_id=self.conversation.id,
            root_run_id=root_run.id,
            task_list_id=self.conversation.active_task_list_id,
            base_commit="b" * 40,
            team_run_id="team_lead_contract",
            lead_agent_id="agent_contract_lead",
        )
        teammate = self.repository.create_team_agent(
            team.id,
            name="Teammate contract",
            agent_id="agent_contract_teammate",
        )
        lead_session = self.repository.list_agent_sessions(
            team.id, role="lead"
        )[0]
        question = MessageBus(self.repository).send(
            team.id,
            sender_type="teammate",
            sender_agent_id=teammate.id,
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=lead_session.generation,
            message_type="QUESTION",
            payload={"question": "Which interface?", "blocking": True},
            dedupe_key="lead-contract-question",
        )
        scheduler.configure_team_runtime(object())

        result = scheduler.run_team_lead_cycle(team.id)

        self.assertTrue(result.stop_reason.startswith("runtime_contract"))
        restored = self.repository.get_agent_session(lead_session.id)
        self.assertEqual(restored.state, AgentSessionState.WAITING)
        self.assertEqual(restored.waiting_reason, "lead_decision_not_recorded")
        self.assertIsNotNone(self.repository.get_team_message(question.id).acked_at)

    def test_running_job_can_be_cancelled_at_boundary(self):
        gate = threading.Event()
        scheduler = RunScheduler(self.repository, _FakeFactory(gate))
        run = scheduler.submit(self.conversation.id, "等待")
        self.assertTrue(gate.wait(1))
        scheduler.cancel(run.id)
        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.repository.get_run(run.id)
            if current and current.status == "cancelled":
                break
            time.sleep(0.01)
        scheduler.stop()
        self.assertEqual(self.repository.get_run(run.id).status, "cancelled")

    def test_permission_round_trip_is_persisted_and_resumes_run(self):
        scheduler = RunScheduler(self.repository, _ApprovalFactory())
        run = scheduler.submit(self.conversation.id, "执行")
        deadline = time.time() + 2
        approval = None
        while time.time() < deadline and approval is None:
            approvals = self.repository.list_approvals(run_id=run.id)
            approval = approvals[0] if approvals else None
            time.sleep(0.01)
        self.assertIsNotNone(approval)
        scheduler.resolve_approval(run.id, approval.id, "allow")
        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.repository.get_run(run.id)
            if current and current.status == "completed":
                break
            time.sleep(0.01)
        scheduler.stop()
        self.assertEqual(self.repository.get_approval(approval.id).status, "allowed")
        self.assertEqual(self.repository.get_run(run.id).status, "completed")

    def test_queued_cancel_is_not_executed_later(self):
        gate = threading.Event()
        scheduler = RunScheduler(self.repository, _FakeFactory(gate), max_concurrent_runs=1)
        first = scheduler.submit(self.conversation.id, "first")
        self.assertTrue(gate.wait(1))
        second_conversation = self.repository.create_conversation(title="second")
        second = scheduler.submit(second_conversation.id, "second")
        scheduler.cancel(second.id)
        scheduler.cancel(first.id)
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.repository.get_run(first.id).status == "cancelled":
                break
            time.sleep(0.01)
        scheduler.stop()
        self.assertEqual(self.repository.get_run(second.id).status, "cancelled")
        self.assertNotIn(
            "run.started",
            [event.type for event in self.repository.list_events(second.id)],
        )

    def test_approval_cannot_be_changed_after_resolution(self):
        scheduler = RunScheduler(self.repository, _FakeFactory())
        run = self.repository.create_run(self.conversation.id)
        approval = self.repository.create_approval(
            run.id,
            tool_name="bash",
            tool_input={"command": "Remove-Item temp.txt"},
            reason="Potentially destructive command",
        )
        scheduler.resolve_approval(run.id, approval.id, "allow")
        with self.assertRaises(ValueError):
            scheduler.resolve_approval(run.id, approval.id, "deny")


if __name__ == "__main__":
    unittest.main()
