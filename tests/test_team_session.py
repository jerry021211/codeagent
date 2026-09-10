from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path

from codeagent import Agent, AgentConfig, ModelResponse, ToolRegistry
from codeagent.context import ContextManager
from codeagent.teams import (
    AgentSessionRunner,
    AgentSessionState,
    MessageBus,
    create_teammate_tools,
)
from codeagent.web.storage import SQLiteRepository, StorageConflictError, InvalidStateTransitionError
from codeagent.teams.tools import TeamQuestionTool, TeamAnswerQuestionTool
from codeagent.runtime.cancellation import ModelCallTimeout
from codeagent.teams.session import _message_context


class _EndTurnClient:
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": self.text}],
        )


class TeamSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.db"
        self.repository = SQLiteRepository(self.database, recover_incomplete=False)
        self.conversation = self.repository.create_conversation(
            title="Team sessions",
            workspace=self.temp_dir.name,
        )
        self.run = self.repository.create_run(self.conversation.id, status="running")
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id
        self.team = self.repository.create_team_run(
            conversation_id=self.conversation.id,
            root_run_id=self.run.id,
            task_list_id=self.task_list_id,
            base_commit="b" * 40,
            team_run_id="team_sessions",
            lead_agent_id="agent_lead",
        )
        plan = self.repository.create_team_plan_revision(
            self.team.id,
            plan={"base_commit": "b" * 40, "tasks": ["task"], "shared_context": "Use Decimal for money."},
            created_by="agent_lead",
            command_id="plan-create",
        )
        self.repository.submit_team_plan_revision(
            self.team.id,
            plan.revision,
            command_id="plan-submit",
        )
        self.repository.decide_team_plan_revision(
            self.team.id,
            plan.revision,
            decision="approve",
            decided_by="user",
            reason="approved",
            command_id="plan-approve",
        )
        self.lead_session = self.repository.create_agent_session(
            self.team.id,
            "agent_lead",
            session_id="session_lead",
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def _create_claimed_teammate(self, suffix: str = "one"):
        task = self.repository.create_task(
            self.task_list_id,
            subject=f"Task {suffix}",
            description=f"Objective {suffix}",
            metadata={
                "kind": "analysis",
                "risk_level": "low",
                "write_scopes": [],
            },
        )
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
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id=f"claim-{suffix}",
        )
        return task, agent, session, attempt

    def _agent(self, client=None):
        return Agent(
            client=client or _EndTurnClient(),
            tools=ToolRegistry(),
            config=AgentConfig(model="fake"),
            context=ContextManager(),
            allow_subagents=False,
        )

    def _question(self, attempt, blocking=True):
        reasons = []
        TeamQuestionTool(self.repository, attempt, reasons.append).run("Which API?", blocking)
        question = [m for m in self.repository.list_team_messages(self.team.id) if m.type == "QUESTION"][-1]
        if blocking:
            self.assertEqual(reasons, [f"waiting_for_lead_answer:{question.id}"])
        return question

    def _answer(self, question, answer="Use the existing API.", scope_changed=False):
        return TeamAnswerQuestionTool(self.repository, self.team.id, self.team.lead_agent_id).run(
            question.id, answer, scope_changed,
        )

    def _wait_for_question(self, session, question):
        self.repository.save_agent_session_checkpoint(
            session.id, messages=[{"role": "user", "content": "Original task context"}],
            context={}, safe_boundary="agent_yield", target_state="waiting",
            waiting_reason=f"waiting_for_lead_answer:{question.id}",
        )

    def test_answer_before_or_after_checkpoint_resumes_with_context(self) -> None:
        for early in (True, False):
            with self.subTest(early=early):
                _, _, session, attempt = self._create_claimed_teammate(str(early))
                question = self._question(attempt)
                if early:
                    self._answer(question)
                self._wait_for_question(session, question)
                if not early:
                    self.assertEqual(self.repository.get_agent_session(session.id).state.value, "waiting")
                    self._answer(question)
                self.assertEqual(self.repository.get_agent_session(session.id).state.value, "work")
                self.assertEqual(self.repository.get_task_attempt(attempt.id).state.value, "running")
                self.assertFalse(self.repository.get_task_attempt(attempt.id).write_enabled)
                client = _EndTurnClient()
                AgentSessionRunner(self.repository).run(self._agent(client), session.id)
                sent = str(client.calls[0]["messages"])
                self.assertIn("Original task context", sent)
                self.assertIn("Use the existing API.", sent)
                answers = [m for m in self.repository.list_team_messages(self.team.id) if m.type == "ANSWER" and m.correlation_id == question.id]
                self.assertEqual(len(answers), 1)
                self.assertIsNotNone(answers[0].acked_at)

    def test_duplicate_answer_does_not_wake_a_different_question(self) -> None:
        _, _, session, attempt = self._create_claimed_teammate()
        first = self._question(attempt)
        self._wait_for_question(session, first)
        original = self._answer(first)
        second = self._question(attempt)
        self._wait_for_question(session, second)
        self.assertEqual(self._answer(first), original)
        self.assertEqual(self.repository.get_agent_session(session.id).state.value, "waiting")
        with self.assertRaisesRegex(StorageConflictError, "different answer"):
            self._answer(first, "A different API.")

    def test_answer_cannot_wake_other_wait_or_unknown_attempt(self) -> None:
        _, _, session, attempt = self._create_claimed_teammate()
        question = self._question(attempt)
        self.repository.save_agent_session_checkpoint(
            session.id, messages=[], context={}, safe_boundary="agent_yield",
            target_state="waiting", waiting_reason="runtime_validation",
        )
        with self.assertRaisesRegex(StorageConflictError, "not waiting for this question"):
            self._answer(question)
        self._wait_for_question(session, question)
        with self.repository._transaction(immediate=True) as connection:
            connection.execute("UPDATE task_attempts SET state = 'waiting', result_unknown = 1 WHERE id = ?", (attempt.id,))
        with self.assertRaisesRegex(StorageConflictError, "frozen"):
            self._answer(question)
        self.assertFalse(any(m.type == "ANSWER" for m in self.repository.list_team_messages(self.team.id)))

    def test_stale_generation_cannot_receive_answer(self) -> None:
        _, _, session, attempt = self._create_claimed_teammate()
        question = self._question(attempt)
        with self.repository._transaction(immediate=True) as connection:
            connection.execute("UPDATE agent_sessions SET generation = generation + 1 WHERE id = ?", (session.id,))
        with self.assertRaisesRegex(StorageConflictError, "stale Session"):
            self._answer(question)

    def test_scope_change_survives_late_checkpoint_and_cannot_be_reanswered(self) -> None:
        _, _, session, attempt = self._create_claimed_teammate()
        question = self._question(attempt)
        self._answer(question, "This requires an approved code Task.", scope_changed=True)
        self._wait_for_question(session, question)
        current = self.repository.get_agent_session(session.id)
        self.assertEqual(current.state.value, "waiting")
        self.assertEqual(current.waiting_reason, "team_plan_change_required")
        current_attempt = self.repository.get_task_attempt(attempt.id)
        self.assertEqual(current_attempt.state.value, "waiting")
        self.assertFalse(current_attempt.write_enabled)
        self.assertFalse(current_attempt.result_unknown)
        with self.assertRaisesRegex(StorageConflictError, "different answer"):
            self._answer(question)
        with self.assertRaisesRegex(StorageConflictError, "plan change"):
            self.repository.resume_task_attempt(
                attempt.id, resumed_by="user", reason="continue", command_id="wrong-resume",
                validated_worktree_fingerprint=None,
            )
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.complete_analysis_attempt(
                attempt.id, summary="Pretend this is finished", submitted_by=attempt.agent_id,
                command_id="wrong-completion",
            )

    def test_nonblocking_question_does_not_interrupt_work(self) -> None:
        _, _, session, attempt = self._create_claimed_teammate()
        question = self._question(attempt, blocking=False)
        self._answer(question)
        self.assertEqual(self.repository.get_agent_session(session.id).state.value, "work")

    def test_assignment_contains_only_direct_dependency_reports(self) -> None:
        task, _, _, attempt = self._create_claimed_teammate("design")
        _, _, _, unrelated = self._create_claimed_teammate("unrelated")
        for prior, summary in ((attempt, "Decimal interface contract"), (unrelated, "Unrelated private report")):
            self.repository.complete_analysis_attempt(
                prior.id, summary=summary, submitted_by=prior.agent_id,
                command_id=f"complete:{prior.id}",
            )
        child = self.repository.create_task(
            self.task_list_id, subject="Implement", description="Own full assignment",
            blocked_by=[task.task.id], metadata={"kind": "analysis"},
        )
        worker = self.repository.create_team_agent(self.team.id, name="Child")
        session = self.repository.create_agent_session(self.team.id, worker.id)
        self.repository.claim_task_attempt(
            self.team.id, task_id=child.task.id, agent_id=worker.id, session_id=session.id,
            expected_task_revision=child.revision, command_id="claim-child",
        )
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        self.assertEqual(assigned.payload["objective"], "Own full assignment")
        reports = assigned.payload["dependency_results"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["task_id"], task.task.id)
        self.assertEqual(reports[0]["summary"], "Decimal interface contract")
        self.assertNotIn("Unrelated private report", str(assigned.payload))
        client = _EndTurnClient()
        AgentSessionRunner(self.repository).run(self._agent(client), session.id)
        brief = str(client.calls[0]["messages"][0]["content"])
        self.assertIn(f"前置任务 #{task.task.id} 的分析结果", brief)
        self.assertIn("Decimal interface contract", brief)
        self.assertNotIn("Unrelated private report", brief)

    def test_session_runner_injects_and_acks_messages_at_checkpoint_boundary(self) -> None:
        _, agent_record, session, _ = self._create_claimed_teammate()
        assigned = self.repository.list_team_messages(
            self.team.id, recipient_agent_id=agent_record.id
        )[0]
        client = _EndTurnClient("candidate summary")
        agent = self._agent(client)

        result = AgentSessionRunner(self.repository).run(agent, session.id)

        self.assertEqual(result.final_text, "candidate summary")
        self.assertFalse(result.yielded)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("TASK_ASSIGNED", str(client.calls[0]["messages"][0]["content"]))
        brief = str(client.calls[0]["messages"][0]["content"])
        self.assertIn("## 本次任务", brief)
        self.assertIn("只读分析", brief)
        self.assertNotIn('"payload"', brief)
        self.assertNotIn("validation_commands", brief)
        self.assertNotIn(assigned.id, brief)
        self.assertNotIn(assigned.attempt_id, brief)
        self.assertIn("Use Decimal for money.", str(client.calls[0]["messages"][0]["content"]))
        self.assertIsNotNone(self.repository.get_team_message(assigned.id).acked_at)
        self.assertEqual(
            self.repository.fetch_unacked_team_messages(session.id),
            [],
        )
        checkpoint = self.repository.get_latest_agent_session_checkpoint(session.id)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.safe_boundary, "agent_completed")
        self.assertEqual(checkpoint.revision, 1)
        self.assertEqual(self.repository.get_team_message(assigned.id).payload, assigned.payload)
        self.assertIn("attempt_ordinal", assigned.payload)

    def test_code_brief_keeps_scope_base_validation_and_exact_contract(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("code-brief")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        payload = {
            **assigned.payload,
            "task_kind": "code", "objective": "Implement save(path, records).",
            "shared_context": 'JSON schema: {"version":1}; __main__.py calls cli.main().',
            "risk_level": "high", "plan_required": True,
            "write_scopes": ["ledger/", "tests/test_cli.py"],
            "validation_commands": ["python -m unittest discover -s tests -v"],
            "acceptance_criteria": ["Invalid data must never overwrite the original file."],
            "exclusive_resources": ["local:test-resource"],
            "budget": {"token_limit": 10000},
        }
        before = json.dumps(payload, ensure_ascii=False)
        message = replace(assigned, payload=payload, artifact_refs=("report:design-v1",))
        brief = _message_context(message)
        for required in (
            payload["objective"], payload["shared_context"], payload["attempt_base_commit"],
            "ledger/", "tests/test_cli.py", payload["validation_commands"][0],
            payload["acceptance_criteria"][0], "Attempt Plan 审批", "local:test-resource",
            "report:design-v1", "当前绑定的 Worktree", "不自行提交或集成",
        ):
            self.assertIn(required, brief)
        for internal in (assigned.id, assigned.attempt_id, "attempt_ordinal", "token_limit", '"payload"'):
            self.assertNotIn(internal, brief)
        self.assertEqual(json.dumps(payload, ensure_ascii=False), before)

    def test_code_brief_without_scope_does_not_imply_unrestricted_write(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("exclusive-brief")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        brief = _message_context(replace(
            assigned, payload={**assigned.payload, "task_kind": "code", "write_scopes": []},
        ))
        self.assertIn("Runtime 确认仓库级独占写租约", brief)

    def test_brief_deduplicates_only_identical_whole_text(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("same-text")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        text = "Decimal only.\n\nDo not overwrite damaged data."
        brief = _message_context(replace(
            assigned, payload={**assigned.payload, "objective": text, "shared_context": text},
        ))
        self.assertEqual(brief.count(text), 1)
        self.assertNotIn("## 公共约定", brief)
        different = _message_context(replace(
            assigned, payload={**assigned.payload, "objective": text,
                               "shared_context": text + " Exit code must be 2."},
        ))
        self.assertIn("Exit code must be 2.", different)
        self.assertIn("## 公共约定", different)

    def test_brief_does_not_truncate_long_requirements_or_report_tails(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("long-brief")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        text = "A confirmed interface constraint.\n" * 400 + "Final requirement: never round via float."
        report = "Prior evidence.\n" * 400 + "Conclusion: corrupt files must remain unchanged."
        brief = _message_context(replace(
            assigned, payload={**assigned.payload, "objective": text,
                               "dependency_results": [{"task_id": "previous", "summary": report}]},
        ))
        self.assertIn(text, brief)
        self.assertIn(report, brief)

    def test_brief_exposes_legacy_analysis_approval_conflict_without_granting_writes(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("invalid-brief")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        payload = {**assigned.payload, "plan_required": True}
        brief = _message_context(replace(assigned, payload=payload))
        self.assertIn("配置冲突", brief)
        self.assertIn("analysis Tasks cannot require an Attempt Plan", brief)
        self.assertIn("不要据此写入或调用未提供的工具", brief)
        self.assertNotIn("team_submit_attempt_plan", brief)
        self.assertIs(payload["plan_required"], True)

    def test_other_message_envelopes_keep_question_and_recovery_identity(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("other-messages")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        for message_type in ("QUESTION", "ANSWER", "ATTEMPT_RESUMED", "SYSTEM_ERROR"):
            with self.subTest(message_type=message_type):
                message = replace(assigned, type=message_type, correlation_id="question-123",
                                  payload={"reason": "Keep the precise message contract"})
                envelope = json.loads(_message_context(message).split("\n", 1)[1])
                self.assertEqual(envelope["message_id"], message.id)
                self.assertEqual(envelope["attempt_id"], message.attempt_id)
                self.assertEqual(envelope["correlation_id"], "question-123")
                self.assertEqual(envelope["payload"], message.payload)

    def test_restoring_checkpoint_does_not_rewrite_legacy_assignment(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("legacy-checkpoint")
        assigned = self.repository.fetch_unacked_team_messages(session.id)[0]
        legacy = json.dumps({"type": "TASK_ASSIGNED", "payload": assigned.payload})
        self.repository.save_agent_session_checkpoint(
            session.id, messages=[{"role": "user", "content": legacy}], context={},
            safe_boundary="agent_yield", acknowledged_message_ids=[assigned.id],
        )
        client = _EndTurnClient()
        AgentSessionRunner(self.repository).run(self._agent(client), session.id)
        self.assertEqual(client.calls[0]["messages"][0]["content"], legacy)
        self.assertEqual(self.repository.fetch_unacked_team_messages(session.id), [])

    def test_failed_model_call_does_not_ack_pending_team_messages(self) -> None:
        _, agent_record, session, _ = self._create_claimed_teammate("failure")
        assigned = self.repository.list_team_messages(
            self.team.id, recipient_agent_id=agent_record.id
        )[0]

        class _FailingAgent(Agent):
            def run_until_yield(self, _prompt):
                raise RuntimeError("model unavailable")

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            AgentSessionRunner(self.repository).run(
                _FailingAgent(
                    client=_EndTurnClient(), tools=ToolRegistry(),
                    config=AgentConfig(model="fake"), allow_subagents=False,
                ), session.id
            )

        self.assertIsNone(self.repository.get_team_message(assigned.id).acked_at)
        pending = self.repository.fetch_unacked_team_messages(session.id)
        self.assertEqual([message.id for message in pending], [assigned.id])
        self.assertIsNone(
            self.repository.get_latest_agent_session_checkpoint(session.id)
        )

    def test_timeout_does_not_checkpoint_or_ack_unmatched_tool_use(self) -> None:
        _, _, session, _ = self._create_claimed_teammate("unmatched")

        class _InterruptedAgent(Agent):
            def run_until_yield(self, _prompt):
                self.messages.append({"role": "assistant", "content": [{
                    "type": "tool_use", "id": "unknown-write", "name": "write_file",
                    "input": {"path": "result.txt"},
                }]})
                raise ModelCallTimeout("model_call_timeout")

        agent = _InterruptedAgent(
            client=_EndTurnClient(), tools=ToolRegistry(),
            config=AgentConfig(model="fake"), allow_subagents=False,
        )
        with self.assertRaises(ModelCallTimeout):
            AgentSessionRunner(self.repository).run(agent, session.id)
        self.assertIsNone(self.repository.get_latest_agent_session_checkpoint(session.id))
        self.assertEqual(len(self.repository.fetch_unacked_team_messages(session.id)), 1)

    def test_automatic_lead_cycle_does_not_consume_user_instruction(self) -> None:
        bus = MessageBus(self.repository)
        user_instruction = bus.send(
            self.team.id,
            sender_type="runtime",
            recipient_type="lead",
            recipient_agent_id="agent_lead",
            recipient_generation=self.lead_session.generation,
            message_type="USER_INSTRUCTION",
            payload={"content": "User-owned turn", "run_id": self.run.id},
            dedupe_key="user-owned-lead-turn",
            priority="control",
        )
        system_error = bus.send(
            self.team.id,
            sender_type="runtime",
            recipient_type="lead",
            recipient_agent_id="agent_lead",
            recipient_generation=self.lead_session.generation,
            message_type="SYSTEM_ERROR",
            payload={
                "reason_code": "test",
                "reason": "Automatic Lead work",
                "effective_scope": "team",
            },
            dedupe_key="automatic-lead-work",
            priority="control",
        )
        client = _EndTurnClient("handled automatic work")

        AgentSessionRunner(self.repository).run(
            self._agent(client),
            self.lead_session.id,
            included_message_ids=frozenset({system_error.id}),
        )

        self.assertNotIn("User-owned turn", str(client.calls[0]["messages"]))
        self.assertIn("Automatic Lead work", str(client.calls[0]["messages"]))
        self.assertIsNone(
            self.repository.get_team_message(user_instruction.id).acked_at
        )
        self.assertIsNotNone(
            self.repository.get_team_message(system_error.id).acked_at
        )

    def test_agent_yields_before_model_call_and_session_becomes_waiting(self) -> None:
        _, _, session, _ = self._create_claimed_teammate()
        client = _EndTurnClient()
        agent = self._agent(client)
        agent.request_yield("lead_answer")

        result = AgentSessionRunner(self.repository).run(agent, session.id)

        self.assertTrue(result.yielded)
        self.assertEqual(result.stop_reason, "waiting:lead_answer")
        self.assertEqual(client.calls, [])
        restored = self.repository.get_agent_session(session.id)
        self.assertEqual(restored.state, AgentSessionState.WAITING)
        self.assertEqual(restored.waiting_reason, "lead_answer")

    def test_team_session_rejects_recursive_subagent_tooling(self) -> None:
        _, _, session, _ = self._create_claimed_teammate()
        unsafe_agent = Agent(
            client=_EndTurnClient(),
            tools=ToolRegistry(),
            config=AgentConfig(model="fake"),
        )

        with self.assertRaisesRegex(ValueError, "allow_subagents=False"):
            AgentSessionRunner(self.repository).run(unsafe_agent, session.id)

    def test_team_sessions_do_not_share_messages_or_context(self) -> None:
        _, _, first_session, _ = self._create_claimed_teammate("one")
        _, _, second_session, _ = self._create_claimed_teammate("two")
        first = self._agent(_EndTurnClient("first"))
        second = self._agent(_EndTurnClient("second"))

        AgentSessionRunner(self.repository).run(first, first_session.id)
        AgentSessionRunner(self.repository).run(second, second_session.id)

        self.assertIsNot(first.messages, second.messages)
        self.assertIsNot(first.context, second.context)
        self.assertIn("Task one", str(first.messages))
        self.assertNotIn("Task two", str(first.messages))
        self.assertIn("Task two", str(second.messages))
        self.assertNotIn("Task one", str(second.messages))

    def test_message_routes_dedupe_and_old_generation_are_enforced(self) -> None:
        _, first_agent, first_session, first_attempt = self._create_claimed_teammate(
            "one"
        )
        _, second_agent, _, _ = self._create_claimed_teammate("two")
        bus = MessageBus(self.repository)
        question = bus.send(
            self.team.id,
            sender_type="teammate",
            sender_agent_id=first_agent.id,
            recipient_type="lead",
            recipient_agent_id="agent_lead",
            recipient_generation=self.lead_session.generation,
            message_type="QUESTION",
            payload={"question": "Which API?", "blocking": True},
            dedupe_key="question:one",
            task_id=first_attempt.task_id,
            attempt_id=first_attempt.id,
            correlation_id="question-correlation",
        )
        repeated = bus.send(
            self.team.id,
            sender_type="teammate",
            sender_agent_id=first_agent.id,
            recipient_type="lead",
            recipient_agent_id="agent_lead",
            recipient_generation=self.lead_session.generation,
            message_type="QUESTION",
            payload={"question": "Which API?", "blocking": True},
            dedupe_key="question:one",
            task_id=first_attempt.task_id,
            attempt_id=first_attempt.id,
            correlation_id="question-correlation",
        )
        self.assertEqual(question, repeated)

        with self.assertRaisesRegex(ValueError, "not allowed"):
            bus.send(
                self.team.id,
                sender_type="teammate",
                sender_agent_id=first_agent.id,
                recipient_type="teammate",
                recipient_agent_id=second_agent.id,
                recipient_generation=1,
                message_type="PROGRESS",
                payload={"stage": "working", "summary": "hidden delegation"},
                dedupe_key="forbidden-peer-message",
            )

        idle_agent = self.repository.create_team_agent(
            self.team.id,
            name="Teammate idle",
            agent_id="agent_idle",
        )
        idle_session = self.repository.create_agent_session(
            self.team.id,
            idle_agent.id,
            session_id="session_idle",
        )
        runtime_message = bus.send(
            self.team.id,
            sender_type="runtime",
            recipient_type="teammate",
            recipient_agent_id=idle_agent.id,
            recipient_generation=idle_session.generation,
            message_type="SYSTEM_ERROR",
            payload={
                "reason_code": "test",
                "reason": "old generation",
                "effective_scope": "session",
            },
            dedupe_key="old-generation-message",
            priority="control",
        )
        self.repository.transition_agent_session(
            idle_session.id,
            AgentSessionState.LOST.value,
        )
        new_session = self.repository.create_agent_session(
            self.team.id,
            idle_agent.id,
            session_id="session_idle_generation_2",
        )
        self.assertEqual(new_session.generation, 2)
        self.assertEqual(self.repository.fetch_unacked_team_messages(new_session.id), [])
        self.assertIsNone(self.repository.get_team_message(runtime_message.id).acked_at)

    def test_teammate_messages_follow_recovered_lead_generation(self) -> None:
        self.repository.transition_agent_session(
            self.lead_session.id, AgentSessionState.LOST.value
        )
        recovered_lead = self.repository.create_agent_session(
            self.team.id,
            "agent_lead",
            session_id="session_lead_generation_2",
        )
        _, _, _, attempt = self._create_claimed_teammate("new-lead")
        progress_tool = next(
            tool
            for tool in create_teammate_tools(
                self.repository,
                None,
                attempt,
                task_kind="analysis",
                yield_callback=lambda _reason: None,
            )
            if tool.definition.name == "team_report_progress"
        )

        progress_tool.run(stage="analysis", summary="Using recovered Lead inbox")

        message = next(
            item
            for item in self.repository.list_team_messages(
                self.team.id, recipient_agent_id="agent_lead"
            )
            if item.type == "PROGRESS"
        )
        self.assertEqual(recovered_lead.generation, 2)
        self.assertEqual(message.recipient_generation, 2)

    def test_message_payload_version_and_size_are_checked(self) -> None:
        _, agent_record, session, _ = self._create_claimed_teammate()
        bus = MessageBus(self.repository)
        with self.assertRaisesRegex(ValueError, "version"):
            bus.send(
                self.team.id,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=agent_record.id,
                recipient_generation=session.generation,
                message_type="SYSTEM_ERROR",
                payload={
                    "reason_code": "test",
                    "reason": "bad version",
                    "effective_scope": "session",
                },
                dedupe_key="bad-version",
                payload_version=2,
            )
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            bus.send(
                self.team.id,
                sender_type="runtime",
                recipient_type="teammate",
                recipient_agent_id=agent_record.id,
                recipient_generation=session.generation,
                message_type="SYSTEM_ERROR",
                payload={
                    "reason_code": "test",
                    "reason": "x" * (65 * 1024),
                    "effective_scope": "session",
                },
                dedupe_key="too-large",
            )


if __name__ == "__main__":
    unittest.main()
