from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from codeagent.context import ContextManager
from codeagent.context.models import RuntimeState
from codeagent.events import TokenTotals
from codeagent.runtime import CancelledError
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

    def create(self, *, event_emitter, cancellation, permission_broker, checkpoint=None):
        del permission_broker, checkpoint
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
        scheduler = RunScheduler(self.repository, _FakeFactory(gate))
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
