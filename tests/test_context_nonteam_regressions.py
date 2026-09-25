"""Ordinary Agent and subagent context lifecycle regressions; no Team runtime."""

from __future__ import annotations

import re
import tempfile
import unittest
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, EventEmitter, ModelResponse, RecoveryConfig, RecoveryRuntime, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.context.budget import inspect_request
from codeagent.events import TokenUsage
from codeagent.hooks import HookManager
from codeagent.memory import MemoryConfig, MemoryManager, MemoryStore
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.prompts import PromptAssemblyResult
from codeagent.runtime import CancellationToken
from codeagent.runtime.cancellation import CancelledError
from codeagent.tools import LoadContextHistoryTool, LoadToolOutputTool, TodoStore, TodoWriteTool
from codeagent.tools.todo import create_todo_reminder_hook, create_todo_final_status_hook


def _response(text="done"):
    return ModelResponse("end_turn", [{"type": "text", "text": text}])


def _history(rounds):
    result = [{"role": "user", "content": "exact active user task"}]
    for index in range(rounds):
        identifier = f"old_{index}"
        result.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": identifier, "name": "echo", "input": {"value": str(index)}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier, "content": f"old evidence {index}"}]},
        ])
    return result


class Client:
    def __init__(self, responses=(), *, calls=None, kind="main", cancel_summary=None):
        self.responses = list(responses)
        self.calls = [] if calls is None else calls
        self.kind = kind
        self.cancel_summary = cancel_summary

    def fork(self, **kwargs):
        child = Client(calls=self.calls, kind=kwargs.get("call_kind", self.kind), cancel_summary=self.cancel_summary)
        child.responses = self.responses
        return child

    def create_message(self, **kwargs):
        self.calls.append((self.kind, deepcopy(kwargs)))
        if kwargs["model"] == "summary":
            if self.cancel_summary is not None:
                self.cancel_summary.cancel("user cancelled during summary response")
            return _response("Older results are verified. Continue the exact current task.")
        if self.kind.startswith("memory"):
            return _response('{"selected_memories":[]}')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class LongTaskClient(Client):
    def __init__(self, *, index=0, rounds=45):
        super().__init__()
        self.index = index
        self.rounds = rounds

    def fork(self, **kwargs):
        # Summary calls share the journal but do not consume an execution step.
        return self

    def create_message(self, **kwargs):
        self.calls.append(("summary" if kwargs["model"] == "summary" else "main", deepcopy(kwargs)))
        if kwargs["model"] == "summary":
            return _response("Verified older evidence remains available; continue the unchanged current request.")
        index = self.index
        self.index += 1
        if index >= self.rounds:
            return _response("long task complete")
        return ModelResponse("tool_use", [{
            "type": "tool_use", "id": f"long_{index:02d}", "name": "echo", "input": {"value": str(index)},
        }], usage=TokenUsage(input_tokens=100 + index, output_tokens=5))


class NonTeamContextRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def context(self, **changes):
        return ContextManager(config=ContextConfig(**{
            "summarization_model": "summary", "transcript_dir": self.root / "transcripts",
            "tool_output_dir": self.root / "outputs", **changes,
        }))

    def agent(self, client, *, context=None, messages=None, tools=None, iterations=60, cancellation=None, recovery=None):
        tools = tools or ToolRegistry()
        if "echo" not in tools:
            tools.register_handler(ToolDefinition("echo", "Return task evidence", {"type": "object"}), lambda value: f"evidence-{value}:" + value * 750)
        return Agent(
            client=client, tools=tools, config=AgentConfig(model="main", max_tokens=1000, max_iterations=iterations),
            context=context or self.context(), messages=messages or [], cancellation=cancellation,
            recovery_runtime=recovery or RecoveryRuntime(RecoveryConfig(max_retries=0, sleep_enabled=False)),
        )

    def test_summary_client_is_not_constructed_when_compaction_disabled(self):
        client = Client([_response()])
        context = self.context(mode="off")
        context.begin_turn(0)
        agent = self.agent(client, context=context, messages=_history(12))
        with patch.object(Agent, "_context_client", side_effect=RuntimeError("invalid summary client config")) as constructor:
            result = agent.run()
        self.assertEqual(result.final_text, "done")
        constructor.assert_not_called()
        self.assertEqual(len(client.calls), 1)

    def test_summary_client_is_not_constructed_without_minimum_foldable_history(self):
        client = Client([_response()])
        agent = self.agent(client, context=self.context(compact_threshold_chars=1))
        with patch.object(Agent, "_context_client", side_effect=RuntimeError("invalid summary client config")) as constructor:
            result = agent.run("small current task")
        self.assertEqual(result.final_text, "done")
        constructor.assert_not_called()
        self.assertEqual(len(client.calls), 1)

    def test_cancellation_during_summary_prevents_state_commit(self):
        token = CancellationToken()
        client = Client(cancel_summary=token)
        context = self.context(compact_threshold_chars=1000)
        context.begin_turn(0)
        history = _history(10)
        original = deepcopy(history)
        agent = self.agent(client, context=context, messages=history, cancellation=token)
        before = asdict(context.state)
        with self.assertRaises(CancelledError):
            agent.run()
        self.assertEqual(asdict(context.state), before)
        self.assertEqual(agent.messages, original)
        self.assertEqual([call[1]["model"] for call in client.calls], ["summary"])
        self.assertEqual(list(context.config.transcript_dir.glob("*.jsonl")), [])

    def test_irreducible_system_with_long_history_never_pays_for_summary(self):
        client = Client()
        context = self.context(max_request_chars=5000)
        context.begin_turn(0)
        agent = self.agent(client, context=context, messages=_history(30))
        assembly = PromptAssemblyResult(system_prompt="irreducible system " * 1000, trace=[], prompt_hash="large")
        with patch.object(Agent, "_assemble_prompt", return_value=assembly):
            result = agent.run()
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual(client.calls, [])
        self.assertEqual(context.state.summary_revision, 0)
        self.assertIn("serialized request characters", result.final_text)

    def test_irreducible_tools_with_long_history_never_pay_for_summary(self):
        client = Client()
        context = self.context(max_request_chars=5000)
        context.begin_turn(0)
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("large", "schema description " * 1000, {"type": "object"}), lambda: "ok")
        result = self.agent(client, context=context, messages=_history(30), tools=tools).run()
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual(client.calls, [])
        self.assertEqual(context.state.summary_revision, 0)

    def test_fallback_uses_its_own_smaller_configured_window(self):
        client = Client([RuntimeError("provider overloaded"), _response("must never reach fallback")])
        context = self.context(context_window_tokens=100000, model_context_windows={"main": 100000, "small": 500})
        recovery = RecoveryRuntime(RecoveryConfig(fallback_model="small", overload_fallback_after=1, sleep_enabled=False))
        result = self.agent(client, context=context, recovery=recovery).run("current user request")
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual([call[1]["model"] for call in client.calls], ["main"])
        self.assertIn("window budget 500", result.final_text)
        self.assertEqual(context.state.summary_revision, 0)

    def test_default_subagents_own_compact_callbacks_and_private_archive_readers(self):
        context = self.context(single_tool_output_max_chars=500)
        tools = ToolRegistry()
        tools.register(LoadToolOutputTool(context.config.tool_output_dir))
        tools.register(LoadContextHistoryTool(context.config.transcript_dir))
        parent = self.agent(Client(), context=context, tools=tools)
        child = parent._create_subagent(EventEmitter())
        sibling = parent._create_subagent(EventEmitter())
        child.tools.execute("compact", {})
        self.assertTrue(child._compact_requested)
        self.assertFalse(parent._compact_requested)
        self.assertFalse(sibling._compact_requested)
        self.assertNotIn("subagent", child.tools)
        self.assertNotEqual(child.context.config.tool_output_dir, parent.context.config.tool_output_dir)
        self.assertNotEqual(child.context.config.tool_output_dir, sibling.context.config.tool_output_dir)
        self.assertNotEqual(child.context.config.transcript_dir, sibling.context.config.transcript_dir)

        call = ToolUse("same_id", "echo", {})
        context.finalize_tool_results([call], ["parent secret " * 200])
        child.context.finalize_tool_results([call], ["child evidence " * 200])
        sibling.context.finalize_tool_results([call], ["sibling secret " * 200])
        parent_path = context.state.tool_artifacts[-1]
        child_path = child.context.state.tool_artifacts[-1]
        sibling_path = sibling.context.state.tool_artifacts[-1]
        self.assertIn("child evidence", child.tools.execute("load_tool_output", {"file_path": child_path}))
        self.assertTrue(child.tools.execute("load_tool_output", {"file_path": parent_path}).startswith("Error:"))
        self.assertTrue(child.tools.execute("load_tool_output", {"file_path": sibling_path}).startswith("Error:"))
        parent_transcript = context.write_transcript([{"role": "user", "content": "parent history"}], reason="test")
        child_transcript = child.context.write_transcript([{"role": "user", "content": "child history"}], reason="test")
        self.assertIn("child history", child.tools.execute("load_context_history", {"file_path": str(child_transcript)}))
        self.assertTrue(child.tools.execute("load_context_history", {"file_path": str(parent_transcript)}).startswith("Error:"))

    def test_default_subagent_planning_state_and_reminder_counters_are_independent(self):
        parent_todos = TodoStore()
        parent_todos.replace([{"content": "parent step", "status": "in_progress"}])
        tools = ToolRegistry()
        tools.register(TodoWriteTool(parent_todos))
        context = self.context()
        context.todo_store = parent_todos
        parent = self.agent(Client(), context=context, tools=tools)
        hooks = HookManager()
        stop_logs = []
        guard_calls = []
        hooks.register("BeforeModelCall", create_todo_reminder_hook(parent_todos, interval=3))
        hooks.register("Stop", create_todo_final_status_hook(parent_todos, stop_logs.append))
        hooks.register("PreToolUse", lambda call: guard_calls.append(call.name))
        parent.hooks = hooks
        messages = [{"role": "assistant", "content": "started"}]
        self.assertIsNone(parent.hooks.trigger("BeforeModelCall", messages))

        child = parent._create_subagent(EventEmitter())
        sibling = parent._create_subagent(EventEmitter())
        child.tools.execute("todo_write", {"todos": [{"content": "child step", "status": "in_progress"}]})
        sibling.tools.execute("todo_write", {"todos": [{"content": "sibling step", "status": "pending"}]})

        self.assertEqual(parent_todos.todos, [{"content": "parent step", "status": "in_progress"}])
        self.assertIn("child step", child.context._task_state())
        self.assertNotIn("parent step", child.context._task_state())
        self.assertIn("sibling step", sibling.context._task_state())
        # First call notices the child's own revision; only its following three
        # calls advance its reminder counter, not the parent's.
        self.assertIsNone(child.hooks.trigger("BeforeModelCall", messages))
        self.assertIsNone(child.hooks.trigger("BeforeModelCall", messages))
        self.assertIsNone(child.hooks.trigger("BeforeModelCall", messages))
        reminder = child.hooks.trigger("BeforeModelCall", messages)
        self.assertIn("child step", reminder)
        self.assertNotIn("parent step", reminder)
        self.assertIsNone(parent.hooks.trigger("BeforeModelCall", messages))
        self.assertIn("parent step", parent.hooks.trigger("BeforeModelCall", messages))
        child.hooks.trigger("Stop", messages)
        parent.hooks.trigger("Stop", messages)
        self.assertIn("child step", stop_logs[0])
        self.assertIn("parent step", stop_logs[1])
        child.hooks.trigger("PreToolUse", ToolUse("read", "read_file", {}))
        self.assertEqual(guard_calls, ["read_file"])

    def test_agent_memory_selection_obeys_same_side_request_budget(self):
        store = MemoryStore(self.root / "memory")
        store.remember(name="fact", description="d" * 12000, content="stable fact")
        memory = MemoryManager(store, MemoryConfig())
        client = Client([_response("main still proceeds")])
        context = self.context(max_request_chars=5000)
        agent = self.agent(client, context=context)
        agent.memory_manager = memory
        result = agent.run("small task")
        self.assertEqual(result.final_text, "main still proceeds")
        self.assertEqual([kind for kind, _ in client.calls], ["main"])
        self.assertLessEqual(inspect_request(**client.calls[0][1]).request_chars, 5000)

    def test_45_round_task_rolls_summaries_and_resumes_checkpoint_without_duplicate_work(self):
        prompt = "EXACT 用户目标：修改 /project/真实文件.py，保留 public_api；不要重新执行已有成功操作。"
        client = LongTaskClient(rounds=45)
        context = self.context(max_request_chars=30000, compact_threshold_chars=12000)
        live = self.agent(client, context=context, iterations=22)
        partial = live.run(prompt)
        self.assertTrue(partial.stop_reason.startswith("max_iterations"))
        self.assertEqual(client.index, 22)
        checkpoint_messages = deepcopy(live.messages)
        checkpoint_state = asdict(context.state)
        self.assertGreaterEqual(context.state.summary_revision, 2)

        resumed_client = LongTaskClient(index=22, rounds=45)
        restored_context = ContextManager(config=context.config, state=RuntimeState(**checkpoint_state))
        restored = self.agent(resumed_client, context=restored_context, messages=deepcopy(checkpoint_messages))
        live.config.max_iterations = 60
        before_continuation = len(client.calls)
        live_result = live.run()
        restored_result = restored.run()
        self.assertEqual(live_result.final_text, "long task complete")
        self.assertEqual(restored_result.final_text, live_result.final_text)
        self.assertEqual(restored.messages, live.messages)
        self.assertEqual(live.messages[:len(checkpoint_messages)], checkpoint_messages)
        self.assertGreaterEqual(context.state.summary_revision, 5)
        self.assertEqual(restored_context.state.summary_revision, context.state.summary_revision)
        self.assertEqual(restored_context.state.compacted_message_count, context.state.compacted_message_count)

        calls = [block["id"] for message in live.messages if isinstance(message["content"], list)
                 for block in message["content"] if block.get("type") == "tool_use"]
        results = [block["tool_use_id"] for message in live.messages if isinstance(message["content"], list)
                   for block in message["content"] if block.get("type") == "tool_result"]
        expected = [f"long_{index:02d}" for index in range(45)]
        self.assertEqual(calls, expected)
        self.assertEqual(results, expected)
        self.assertEqual(Counter(calls), Counter(expected))
        self.assertEqual(sum(message.get("content") == prompt for message in live.messages), 1)
        self.assertNotIn("<context_summary", str(live.messages))
        validate_tool_history(live.messages)

        sent_live = [request for _, request in client.calls[before_continuation:] if request["model"] == "main"]
        sent_resumed = [request for _, request in resumed_client.calls if request["model"] == "main"]
        self.assertEqual(sent_live[0], sent_resumed[0])
        self.assertEqual(len(sent_live), len(sent_resumed))
        for left, right in zip(sent_live, sent_resumed):
            # Fresh archive timestamps may differ after the shared checkpoint.
            self.assertEqual(self._without_archive_location(left["messages"]), self._without_archive_location(right["messages"]))
        for _, request in client.calls + resumed_client.calls:
            limit = context.config.summary_input_max_chars if request["model"] == "summary" else context.config.max_request_chars
            self.assertLessEqual(inspect_request(**request).request_chars, limit)
            if request["model"] == "main":
                validate_tool_history(request["messages"])
                self.assertIn({"role": "user", "content": prompt}, request["messages"])

    @staticmethod
    def _without_archive_location(messages):
        result = deepcopy(messages)
        for message in result:
            if isinstance(message["content"], str) and message["content"].startswith("<context_summary"):
                message["content"] = re.sub(r"原始已接收历史：[^\n]*", "原始已接收历史：<archive>", message["content"])
        return result


if __name__ == "__main__":
    unittest.main()
