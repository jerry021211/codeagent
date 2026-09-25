"""Regression coverage for cancelled tool batches and resuming their history."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from codeagent import Agent, AgentConfig, CallbackEventSink, EnvironmentConfig, EventEmitter, HookManager, ModelResponse, ToolDefinition, ToolRegistry
from codeagent.events import ExecutionContext
from codeagent.permissions import WaitingPermissionBroker
from codeagent.web.factory import WebAgentFactory
from codeagent.context import ContextConfig, ContextManager
from codeagent.messages import ToolUse, reconcile_tool_history, validate_tool_history
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository


CALLS = [{"type": "tool_use", "id": f"call-{i}", "name": "probe", "input": {"number": i}} for i in range(3)]


class StrictClient:
    def __init__(self, first=True):
        self.first = first
        self.calls = []

    def create_message(self, **kwargs):
        validate_tool_history(kwargs["messages"])
        self.calls.append(deepcopy(kwargs["messages"]))
        if self.first:
            self.first = False
            return ModelResponse(stop_reason="tool_use", content=deepcopy(CALLS))
        return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "continued"}])


class ToolCancellationTests(unittest.TestCase):
    def make_agent(self, handler, *, hooks=None, token=None):
        registry = ToolRegistry()
        registry.register_handler(ToolDefinition("probe", "probe", {"type": "object"}), handler)
        self.events = []
        return Agent(client=StrictClient(), tools=registry, config=AgentConfig(model="fake"),
                     cancellation=token or CancellationToken(), hooks=hooks or HookManager(),
                     event_emitter=EventEmitter(CallbackEventSink(self.events.append)))

    def results(self, agent):
        validate_tool_history(agent.messages)
        return next(message["content"] for message in agent.messages if isinstance(message["content"], list) and message["content"] and message["content"][0].get("type") == "tool_result")

    def test_cancel_after_return_keeps_success_and_never_starts_next_tool(self):
        token = CancellationToken()
        calls = []
        def handler(number):
            calls.append(number)
            token.cancel()
            return "file created"
        agent = self.make_agent(handler, token=token)
        with self.assertRaises(CancelledError):
            agent.run("start")
        results = self.results(agent)
        self.assertEqual(calls, [0])
        self.assertEqual(results[0]["content"], "file created")
        self.assertFalse(results[0].get("is_error", False))
        self.assertIn("未执行", results[1]["content"])
        self.assertEqual([e.type for e in self.events if e.type in {"tool.completed", "tool.cancelled"}], ["tool.completed", "tool.cancelled", "tool.cancelled"])
        agent.cancellation = CancellationToken()
        self.assertEqual(agent.run("continue without tools").final_text, "continued")
        self.assertEqual(calls, [0])

    def test_interrupted_dispatch_is_unknown_and_registry_propagates_cancel(self):
        calls = []
        def handler(number):
            calls.append(number)
            if number == 1:
                raise CancelledError("during command")
            return "saved"
        agent = self.make_agent(handler)
        with self.assertRaises(CancelledError):
            agent.run("start")
        results = self.results(agent)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(results[0]["content"], "saved")
        self.assertIn("结果未知", results[1]["content"])
        self.assertIn("未执行", results[2]["content"])
        self.assertTrue(any(e.type == "tool.interrupted" for e in self.events))

    def test_cancel_after_last_return_keeps_all_real_results(self):
        token = CancellationToken()
        calls = []
        def handler(number):
            calls.append(number)
            if number == 2:
                token.cancel()
            return f"saved {number}"
        agent = self.make_agent(handler, token=token)
        with self.assertRaises(CancelledError):
            agent.run("start")
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual([item["content"] for item in self.results(agent)], ["saved 0", "saved 1", "saved 2"])
        self.assertTrue(all(not item.get("is_error") for item in self.results(agent)))

    def test_permission_wait_cancellation_does_not_dispatch(self):
        token = CancellationToken()
        hooks = HookManager()
        hooks.register("PreToolUse", lambda *_: token.cancel())
        calls = []
        agent = self.make_agent(lambda number: calls.append(number) or "saved", hooks=hooks, token=token)
        with self.assertRaises(CancelledError):
            agent.run("start")
        self.assertEqual(calls, [])
        self.assertTrue(all("未执行" in item["content"] for item in self.results(agent)))

    def test_post_hook_failure_preserves_real_result(self):
        hooks = HookManager()
        def fail(*_):
            raise RuntimeError("hook failed")
        hooks.register("PostToolUse", fail)
        agent = self.make_agent(lambda number: "file created", hooks=hooks)
        with self.assertRaisesRegex(RuntimeError, "hook failed"):
            agent.run("start")
        self.assertEqual(self.results(agent)[0]["content"], "file created")
        self.assertFalse(self.results(agent)[0].get("is_error", False))

    def test_formatter_failure_does_not_mask_cancel_or_break_pairing(self):
        token = CancellationToken()
        agent = self.make_agent(lambda number: token.cancel() or "saved", token=token)
        with patch.object(ContextManager, "finalize_tool_results", side_effect=OSError("disk full")):
            with self.assertRaises(CancelledError) as caught:
                agent.run("start")
        self.assertEqual(self.results(agent)[0]["content"], "saved")
        self.assertIn("disk full", str(caught.exception.__notes__))

    def test_archive_failure_retains_truth_and_respects_budgets(self):
        context = ContextManager(config=ContextConfig(single_tool_output_max_chars=120, tool_result_budget_chars=150, persisted_preview_chars=70))
        with patch.object(ContextManager, "_write_tool_output", side_effect=OSError("disk full")):
            outputs = context.finalize_tool_results([ToolUse("a", "probe", {}), ToolUse("b", "probe", {})], ["hello" * 500, "world" * 500])
        self.assertEqual(len(outputs), 2)
        self.assertLessEqual(sum(map(len, outputs)), 150)
        self.assertTrue(all(len(output) <= 120 for output in outputs))
        self.assertTrue(all("归档失败" in output for output in outputs))
        self.assertNotIn("path:", str(outputs))

    def test_blocked_tool_has_result_and_normal_batch_continues(self):
        hooks = HookManager()
        hooks.register("PreToolUse", lambda tool: "denied" if tool.input["number"] == 0 else None)
        calls = []
        agent = self.make_agent(lambda number: calls.append(number) or "ok", hooks=hooks)
        agent.run("start")
        self.assertEqual(calls, [1, 2])
        self.assertTrue(self.results(agent)[0]["is_error"])

    def test_legacy_repair_is_conservative_idempotent_and_keeps_followup(self):
        history = [{"role": "assistant", "content": deepcopy(CALLS)}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-0", "content": "saved"}, {"type": "text", "text": "continue"}]}]
        original = deepcopy(history)
        repaired, ids = reconcile_tool_history(history, repair_missing=True)
        self.assertEqual(history, original)
        self.assertEqual(ids, ["call-1", "call-2"])
        self.assertEqual(repaired[1]["content"][0]["content"], "saved")
        self.assertEqual(repaired[1]["content"][-1]["text"], "continue")
        self.assertIn("结果未知", repaired[1]["content"][1]["content"])
        self.assertEqual(reconcile_tool_history(repaired, repair_missing=True), (repaired, []))
        no_results, _ = reconcile_tool_history([original[0], {"role": "user", "content": "new question"}], repair_missing=True)
        self.assertEqual(no_results[-1]["content"], "new question")
        validate_tool_history(no_results)

    def test_ambiguous_or_new_incomplete_history_fails_locally(self):
        with self.assertRaises(ValueError):
            validate_tool_history([{"role": "assistant", "content": CALLS}])
        for history in [
            [{"role": "assistant", "content": [CALLS[0], CALLS[0]]}],
            [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "orphan", "content": "x"}]}],
        ]:
            with self.assertRaises(ValueError):
                reconcile_tool_history(history, repair_missing=True)

    def test_real_factory_repairs_legacy_checkpoint_without_replaying_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SQLiteRepository(root / "state.db", recover_incomplete=False)
            env = EnvironmentConfig(model_id="fake", data_dir=root / "data", enable_skills=False, mcp_config_path=root / "missing.json")
            factory = WebAgentFactory(env, root, repo)
            try:
                conversation = repo.create_conversation(workspace=directory)
                old_run = repo.create_run(conversation.id)
                legacy = [{"role": "user", "content": "old request"}, {"role": "assistant", "content": deepcopy(CALLS)}]
                repo.finish_run_with_checkpoint(old_run.id, status="cancelled", messages=legacy, todos=[], context={})
                checkpoint = repo.get_latest_checkpoint(conversation.id)
                events = []
                emitter = EventEmitter(CallbackEventSink(events.append), context=ExecutionContext(conversation_id=conversation.id))
                client = StrictClient(first=False)
                with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
                    agent = factory.create(event_emitter=emitter, cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker(), checkpoint=checkpoint)
                    validate_tool_history(agent.messages)
                    self.assertEqual(agent.run("仅根据历史回答，不执行工具").final_text, "continued")
                self.assertTrue(any(event.type == "history.repaired" for event in events))
                self.assertEqual(repo.get_checkpoint_for_run(old_run.id).messages, legacy)
                self.assertEqual(len(client.calls), 1)
                self.assertIn("结果未知", repr(client.calls[0]))
            finally:
                factory.close()
                repo.close()

    def test_durable_event_failure_preserves_returned_result_and_stops_batch(self):
        calls = []
        agent = self.make_agent(lambda number: calls.append(number) or "saved")
        class FailingSink:
            durable = True
            def emit(self, event):
                if event.type == "tool.completed":
                    raise OSError("event disk full")
        agent.event_emitter = EventEmitter(FailingSink())
        with self.assertRaisesRegex(RuntimeError, "Unable to persist run event"):
            agent.run("start")
        self.assertEqual(calls, [0])
        self.assertEqual(self.results(agent)[0]["content"], "saved")

    def test_lost_checkpoint_blocks_silent_resume_of_older_context(self):
        with TemporaryDirectory() as directory:
            repo = SQLiteRepository(Path(directory) / "state.db", recover_incomplete=False)
            class NeverFactory:
                def create(self, **kwargs):
                    raise AssertionError("must not execute with stale context")
            scheduler = RunScheduler(repo, NeverFactory())
            try:
                conversation = repo.create_conversation(workspace=directory)
                old = repo.create_run(conversation.id)
                repo.update_run_status(old.id, "failed", metadata={"tool_checkpoint_required": True})
                run = scheduler.submit(conversation.id, "continue")
                deadline = time.monotonic() + 5
                while scheduler.is_run_pending(run.id) and time.monotonic() < deadline:
                    time.sleep(0.01)
                saved = repo.get_run(run.id)
                self.assertEqual(saved.status, "failed")
                self.assertIn("未能保存完整上下文", str(saved.error))
                self.assertIsNone(repo.get_checkpoint_for_run(run.id))
            finally:
                scheduler.stop(timeout=6)
                repo.close()

    def test_checkpoint_write_failure_is_visible_and_next_run_cannot_execute(self):
        clients = []
        class Factory:
            def create(self, *, event_emitter, cancellation, **kwargs):
                client = StrictClient(first=False)
                clients.append(client)
                return Agent(client=client, tools=ToolRegistry(), config=AgentConfig(model="fake"), cancellation=cancellation, event_emitter=event_emitter)
        with TemporaryDirectory() as directory:
            repo = SQLiteRepository(Path(directory) / "state.db", recover_incomplete=False)
            scheduler = RunScheduler(repo, Factory())
            def wait(run):
                deadline = time.monotonic() + 5
                while scheduler.is_run_pending(run.id) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(scheduler.is_run_pending(run.id))
            try:
                conversation = repo.create_conversation(workspace=directory)
                with patch.object(repo, "finish_run_with_checkpoint", side_effect=OSError("checkpoint disk full")):
                    with self.assertLogs("codeagent.web.scheduler", level="ERROR"):
                        first = scheduler.submit(conversation.id, "first")
                        wait(first)
                self.assertEqual(repo.get_run(first.id).status, "failed")
                self.assertIsNone(repo.get_checkpoint_for_run(first.id))
                second = scheduler.submit(conversation.id, "continue")
                wait(second)
                self.assertEqual(repo.get_run(second.id).status, "failed")
                self.assertIn("未能保存完整上下文", str(repo.get_run(second.id).error))
                self.assertEqual(len(clients), 1)
            finally:
                scheduler.stop(timeout=6)
                repo.close()

    def test_scheduler_cancel_checkpoint_then_same_conversation_continues(self):
        entered = threading.Event()
        executed = []
        class Factory:
            def create(factory_self, *, event_emitter, cancellation, checkpoint=None, **kwargs):
                registry = ToolRegistry()
                def handler(number):
                    executed.append(number)
                    if number == 0:
                        probe.write_text("created once", encoding="utf-8")
                    if number == 1:
                        entered.set()
                        if not cancellation.wait(5):
                            raise AssertionError("test did not cancel")
                        cancellation.raise_if_cancelled()
                    return "file saved"
                registry.register_handler(ToolDefinition("probe", "probe", {"type": "object"}), handler)
                return Agent(client=StrictClient(first=checkpoint is None), tools=registry, config=AgentConfig(model="fake"), cancellation=cancellation, event_emitter=event_emitter, messages=deepcopy(checkpoint.messages) if checkpoint else [])
        def wait_done(scheduler, run):
            deadline = time.monotonic() + 5
            while scheduler.is_run_pending(run.id) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(scheduler.is_run_pending(run.id))
        with TemporaryDirectory() as directory:
            probe = Path(directory) / "a02-probe.txt"
            repo = SQLiteRepository(Path(directory) / "state.db", recover_incomplete=False)
            scheduler = RunScheduler(repo, Factory())
            try:
                conversation = repo.create_conversation(workspace=directory)
                first = scheduler.submit(conversation.id, "start")
                self.assertTrue(entered.wait(5))
                scheduler.cancel(first.id)
                wait_done(scheduler, first)
                self.assertEqual(repo.get_run(first.id).status, "cancelled")
                checkpoint = repo.get_latest_checkpoint(conversation.id)
                validate_tool_history(checkpoint.messages)
                self.assertEqual(checkpoint.metadata["tool_history_version"], 1)
                second = scheduler.submit(conversation.id, "continue without tools")
                wait_done(scheduler, second)
                self.assertEqual(repo.get_run(second.id).status, "completed")
                self.assertEqual(executed, [0, 1])
                self.assertEqual(probe.read_text(encoding="utf-8"), "created once")
            finally:
                scheduler.stop(timeout=6)
                repo.close()
