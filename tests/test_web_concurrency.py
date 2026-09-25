from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import EnvironmentConfig
from codeagent.context import ContextManager
from codeagent.context.models import RuntimeState
from codeagent.events import TokenTotals
from codeagent.tools import TodoStore
from codeagent.web.questions import WebUserQuestions
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository, StorageConflictError


class ControlledFactory:
    def __init__(self):
        self.entered = queue.Queue()
        self.gates: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.closed = False
        self.close_count = 0
        self.repo = None

    def hold(self, prompt):
        self.gates[prompt] = threading.Event()

    def create(self, *, event_emitter, cancellation, permission_broker, **_kwargs):
        factory = self

        class TestAgent:
            def __init__(self):
                self.messages = []
                self.context = ContextManager(state=RuntimeState(), todo_store=TodoStore())

            def run(self, prompt):
                self.messages.append({"role": "user", "content": prompt})
                with factory.lock:
                    factory.active += 1
                    factory.peak = max(factory.peak, factory.active)
                factory.entered.put((prompt, event_emitter.context.run_id))
                try:
                    if prompt == "question":
                        WebUserQuestions(factory.repo, event_emitter, cancellation).ask("format?", ["CSV"])
                    elif prompt == "approval":
                        permission_broker.request("bash", {}, "test", cancellation=cancellation)
                    elif prompt == "failure":
                        raise ValueError("deliberate test failure")
                    gate = factory.gates.get(prompt)
                    while gate is not None and not gate.wait(0.01):
                        if prompt != "uncancellable":
                            cancellation.raise_if_cancelled()
                    cancellation.raise_if_cancelled()
                    self.messages.append({"role": "assistant", "content": f"done:{prompt}"})
                    event_emitter.emit("model.text_delta", {"text": prompt})
                    return SimpleNamespace(final_text=f"done:{prompt}", stop_reason="end_turn", usage=TokenTotals())
                finally:
                    with factory.lock:
                        factory.active -= 1

        return TestAgent()

    def close(self):
        assert self.active == 0, "closed factory while Agents are running"
        self.closed = True
        self.close_count += 1


class ConcurrentSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "state.db")
        self.factory = ControlledFactory()
        self.factory.repo = self.repo
        self.scheduler = RunScheduler(self.repo, self.factory, max_concurrent_runs=2)

    def tearDown(self):
        for gate in self.factory.gates.values():
            gate.set()
        self.scheduler.stop()
        self.repo.close()
        self.temp.cleanup()

    def submit(self, prompt, conversation=None):
        # Deliberately use the same workspace: there is no project-wide Run lock.
        conversation = conversation or self.repo.create_conversation(workspace=self.temp.name)
        return self.scheduler.submit(conversation.id, prompt)

    def entered(self, prompt):
        actual, run_id = self.factory.entered.get(timeout=3)
        self.assertEqual(actual, prompt)
        return run_id

    def finished(self, run, status="completed"):
        deadline = time.monotonic() + 3
        while self.scheduler.is_run_pending(run.id) and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertFalse(self.scheduler.is_run_pending(run.id))
        self.assertEqual(self.repo.get_run(run.id).status, status)

    def test_capacity_fifo_and_conversation_data_isolation(self):
        self.factory.hold("first")
        self.factory.hold("second")
        first = self.submit("first")
        self.entered("first")
        second = self.submit("second")
        self.entered("second")
        third = self.submit("third")
        fourth = self.submit("fourth")
        self.assertEqual(self.factory.active, 2)
        self.assertEqual(self.repo.get_run(third.id).status, "queued")
        self.assertEqual(self.repo.get_run(fourth.id).queue_position, 2)
        self.factory.gates["first"].set()
        self.entered("third")
        self.entered("fourth")
        self.finished(third)
        self.finished(fourth)
        self.factory.gates["second"].set()
        for run, prompt in ((first, "first"), (second, "second"), (third, "third"), (fourth, "fourth")):
            self.finished(run)
            self.assertEqual([m.content for m in self.repo.list_messages(run.conversation_id)], [prompt, f"done:{prompt}"])
            events = self.repo.list_events(run.id)
            self.assertTrue(all(e.run_id == run.id and e.conversation_id == run.conversation_id for e in events))
            self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))
            self.assertIsNotNone(self.repo.get_checkpoint_for_run(run.id))
        self.assertEqual(self.factory.peak, 2)

    def test_same_conversation_simultaneous_submissions_only_accept_one(self):
        self.factory.hold("held")
        conversation = self.repo.create_conversation(workspace=self.temp.name)
        barrier = threading.Barrier(3)

        def submit():
            barrier.wait(timeout=3)
            try:
                return self.submit("held", conversation)
            except StorageConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(submit) for _ in range(2)]
            barrier.wait(timeout=3)
            accepted = [f.result(timeout=3) for f in results]
        self.assertEqual(sum(run is not None for run in accepted), 1)
        self.assertEqual(len(self.repo.list_messages(conversation.id)), 1)

    def test_queued_and_running_cancel_do_not_affect_another_conversation(self):
        for prompt in ("first", "second"):
            self.factory.hold(prompt)
        first = self.submit("first")
        self.entered("first")
        second = self.submit("second")
        self.entered("second")
        queued = self.submit("never")
        self.scheduler.cancel(queued.id)
        self.assertFalse(self.scheduler.is_run_pending(queued.id))
        self.scheduler.cancel(first.id)
        self.finished(first, "cancelled")
        self.finished(queued, "cancelled")
        self.assertEqual(self.repo.get_run(second.id).status, "running")
        following = self.submit("following")
        self.entered("following")
        self.finished(following)
        self.assertNotIn("run.started", [e.type for e in self.repo.list_events(queued.id)])

    def test_question_and_approval_waiting_leave_other_slot_available(self):
        for prompt in ("question", "approval"):
            with self.subTest(prompt=prompt):
                waiting = self.submit(prompt)
                self.entered(prompt)
                deadline = time.monotonic() + 3
                pending = []
                while time.monotonic() < deadline:
                    pending = (self.repo.list_user_questions(waiting.id) if prompt == "question"
                               else self.repo.list_approvals(run_id=waiting.id))
                    if pending:
                        break
                    threading.Event().wait(0.005)
                self.assertTrue(pending)
                other = self.submit("other")
                self.entered("other")
                self.finished(other)
                self.assertEqual(self.repo.get_run(waiting.id).status, "running")
                if prompt == "question":
                    self.repo.answer_user_question(waiting.id, pending[0]["id"], "CSV")
                else:
                    self.scheduler.resolve_approval(waiting.id, pending[0].id, "allow")
                self.finished(waiting)

    def test_failure_and_checkpoint_error_do_not_lose_workers(self):
        failed = self.submit("failure")
        self.entered("failure")
        self.finished(failed, "failed")
        with patch.object(self.repo, "finish_run_with_checkpoint", side_effect=RuntimeError("checkpoint unavailable")):
            with self.assertLogs("codeagent.web.scheduler", level="ERROR"):
                failed_checkpoint = self.submit("checkpoint")
                self.entered("checkpoint")
                self.finished(failed_checkpoint, "failed")
        for prompt in ("first", "second"):
            self.factory.hold(prompt)
            self.submit(prompt)
            self.entered(prompt)
        self.assertEqual(self.factory.active, 2)

    def test_shutdown_timeout_keeps_resources_then_waits_for_all_workers(self):
        self.factory.hold("uncancellable")
        run = self.submit("uncancellable")
        self.entered("uncancellable")
        with self.assertRaises(TimeoutError):
            self.scheduler.stop(timeout=0)
        self.assertFalse(self.factory.closed)
        self.assertTrue(self.scheduler.is_run_pending(run.id))
        with self.assertRaises(StorageConflictError):
            self.submit("rejected")
        self.factory.gates["uncancellable"].set()
        self.scheduler.stop(timeout=3)
        self.scheduler.stop()
        self.assertTrue(self.factory.closed)
        self.assertEqual(self.factory.close_count, 1)
        self.assertTrue(all(not worker.is_alive() for worker in self.scheduler._threads))

    def test_submission_and_stop_cannot_leave_an_orphaned_run(self):
        barrier = threading.Barrier(3)

        def submit():
            barrier.wait(timeout=3)
            try:
                return self.submit("racing")
            except StorageConflictError:
                return None

        def stop():
            barrier.wait(timeout=3)
            self.scheduler.stop()

        with ThreadPoolExecutor(max_workers=2) as executor:
            submitted = executor.submit(submit)
            stopped = executor.submit(stop)
            barrier.wait(timeout=3)
            run = submitted.result(timeout=3)
            stopped.result(timeout=3)
        if run:
            self.assertNotIn(self.repo.get_run(run.id).status, {"queued", "running"})
        self.assertFalse(self.scheduler._controls)


class ConcurrencyConfigTests(unittest.TestCase):
    def test_default_override_and_validation(self):
        with patch("codeagent.config._load_dotenv"), patch.dict(os.environ, {"MODEL_ID": "test", "CONTEXT_COMPACT_MODE": "off"}, clear=True):
            self.assertEqual(EnvironmentConfig.from_env().web_max_concurrent_runs, 4)
            os.environ["CODEAGENT_WEB_MAX_CONCURRENT_RUNS"] = "1"
            self.assertEqual(EnvironmentConfig.from_env().web_max_concurrent_runs, 1)
            for value in ("0", "-1", "1.5", "oops"):
                os.environ["CODEAGENT_WEB_MAX_CONCURRENT_RUNS"] = value
                with self.subTest(value=value), self.assertRaises((ValueError, RuntimeError)):
                    EnvironmentConfig.from_env()
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunScheduler(None, None, max_concurrent_runs=value)


if __name__ == "__main__":
    unittest.main()
