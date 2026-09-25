"""Loop protection through the real Agent loop and synchronous Hook dispatch."""

from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from codeagent.agent import Agent, AgentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import CallbackEventSink, EventEmitter, ExecutionContext, TokenUsage
from codeagent.hooks import HookManager
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import validate_tool_history
from codeagent.models import ModelResponse
from codeagent.runtime.execution import BudgetedClient
from codeagent.tools.base import ToolDefinition, ToolInputState, ToolOutput
from codeagent.tools.edit import EditFileTool
from codeagent.tools.registry import ToolRegistry
from codeagent.tools.workspace import WorkspaceGuard


def call(identifier, name="bash", arguments=None):
    return {
        "type": "tool_use", "id": identifier, "name": name,
        "input": {"command": "pytest test_example.py"} if arguments is None else arguments,
    }


def tools(*calls):
    return ModelResponse("tool_use", list(calls))


def done(text="Verification remains incomplete."):
    return ModelResponse("end_turn", [{"type": "text", "text": text}])


def failure(**overrides):
    facts = dict(status="error", exit_code=1, outcome="diagnostic", deterministic=True,
                 result_signature="test_example::test_value:AssertionError")
    facts.update(overrides)
    return ToolOutput("FAILED test_example::test_value - AssertionError: expected 2", **facts)


class ScriptedClient:
    """No network or provider: every request must match the finite script."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create_message(self, **kwargs):
        validate_tool_history(kwargs["messages"])
        self.calls.append(deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Agent requested an unexpected extra model response")
        return self.responses.pop(0)


class LoopGuardIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.code = self.root / "implementation.py"
        self.code.write_text("value = 1\n", encoding="utf-8")
        self.registry = ToolRegistry()
        self.executed = []
        self.events = []

    def register_test(self, *, known=True, output=None):
        def run(command):
            self.executed.append(command)
            return output() if output is not None else failure()

        self.registry.register_handler(
            ToolDefinition("bash", "Run a local test", {
                "type": "object", "required": ["command"],
                "properties": {"command": {"type": "string"}},
            }), run,
            input_state=lambda args: ToolInputState(
                hashlib.sha256(self.code.read_bytes()).hexdigest() if known else "",
                known=known, cwd=str(self.root),
            ),
        )

    def register_read(self):
        def read(file_path, offset=0):
            self.executed.append(("read", file_path, offset))
            return ToolOutput("source line", status="success")

        self.registry.register_handler(ToolDefinition("read_file", "Read source", {
            "properties": {"file_path": {"type": "string"}, "offset": {"type": "integer"}},
            "required": ["file_path"],
        }), read)

    def make_agent(self, responses, *, hooks=None, messages=None, event_context=None, **limits):
        self.events = []
        client = ScriptedClient(responses)
        agent = Agent(
            client=client, tools=self.registry, allow_subagents=False,
            config=AgentConfig(model="fake-local", max_iterations=40,
                               loop_guard=LoopGuardConfig(retry_delay_seconds=0, **limits)),
            context=ContextManager(config=ContextConfig(
                mode="off", transcript_dir=self.root / "transcripts",
                tool_output_dir=self.root / "outputs", tool_projection_enabled=False,
            )),
            hooks=hooks or HookManager(), messages=deepcopy(messages or []),
            event_emitter=EventEmitter(CallbackEventSink(self.events.append), context=event_context),
        )
        return agent, client

    def results(self, agent):
        validate_tool_history(agent.messages)
        return {
            block["tool_use_id"]: block
            for message in agent.messages if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "tool_result"
        }

    def events_of(self, kind):
        return [event for event in self.events if event.type == kind]

    def train(self):
        return [tools(call(f"failure-{index}")) for index in range(3)]

    def test_three_failures_warn_once_then_block_before_execution_and_pair_result(self):
        self.register_test()
        agent, client = self.make_agent(self.train() + [tools(call("blocked")), done()])
        result = agent.run("Fix the failing local test")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(len(self.events_of("tool.started")), 3)
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)
        results = self.results(agent)
        self.assertEqual(set(results), {"failure-0", "failure-1", "failure-2", "blocked"})
        self.assertNotIn("运行时纠正", results["failure-1"]["content"])
        self.assertIn("运行时纠正", results["failure-2"]["content"])
        self.assertTrue(results["blocked"]["is_error"])
        self.assertIn("未执行", results["blocked"]["content"])
        self.assertIn("其他诊断", results["blocked"]["content"])
        self.assertIn("相同输入状态", client.calls[3]["system"])
        self.assertEqual(len(agent._loop_guard.state.recent), 3)
        self.assertEqual(agent._loop_guard.state.blocked_attempts, 1)
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, 4)

    def test_three_blocked_recoveries_stop_without_extra_model_call(self):
        self.register_test()
        agent, client = self.make_agent(self.train() + [tools(call(f"blocked-{i}")) for i in range(3)])
        result = agent.run("Fix test", execution_id="same")
        self.assertEqual(result.stop_reason, "loop_detected:blocked_recovery_exhausted")
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(len(self.results(agent)), 6)
        self.assertEqual(agent._loop_guard.state.blocked_attempts, 3)
        self.assertIn("未标记为完成", result.final_text)
        self.assertFalse(self.events_of("agent.completed"))
        self.assertEqual(len(self.events_of("agent.failed")), 1)

    def test_related_edit_unlocks_same_test_but_preserves_earlier_issue(self):
        self.register_test()
        self.registry.register(EditFileTool(workspace_guard=WorkspaceGuard(self.root)))
        agent, _ = self.make_agent(self.train() + [
            tools(call("edit", "edit_file", {"file_path": "implementation.py", "old_string": "1", "new_string": "2"})),
            tools(call("retest")), done(),
        ])
        agent.run("Fix test")
        self.assertEqual(len(self.executed), 4)
        self.assertNotIn("未执行", self.results(agent)["retest"]["content"])
        self.assertEqual(len(self.events_of("tool.blocked")), 0)
        self.assertEqual(len(agent._loop_guard.state.issues), 2)
        self.assertEqual(self.code.read_text(encoding="utf-8"), "value = 2\n")
        self.assertIn(str(self.code), agent._loop_guard.state.changed_files)

    def test_unrelated_edit_does_not_unlock_known_test(self):
        self.register_test()
        self.registry.register(EditFileTool(workspace_guard=WorkspaceGuard(self.root)))
        unrelated = self.root / "notes.txt"
        unrelated.write_text("before", encoding="utf-8")
        agent, _ = self.make_agent(self.train() + [
            tools(call("edit-notes", "edit_file", {"file_path": "notes.txt", "old_string": "before", "new_string": "after"})),
            tools(call("blocked")), done(),
        ])
        agent.run("Fix test")
        self.assertEqual(len(self.executed), 3)
        self.assertIn("未执行", self.results(agent)["blocked"]["content"])
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "after")

    def test_successful_other_operation_does_not_clear_failure_history(self):
        self.register_test()
        self.register_read()
        agent, _ = self.make_agent(self.train() + [
            tools(call("read", "read_file", {"file_path": "implementation.py"})),
            tools(call("blocked")), done(),
        ])
        agent.run("Diagnose test")
        self.assertEqual(len(self.executed), 4)  # Three tests and one successful read.
        self.assertIn("未执行", self.results(agent)["blocked"]["content"])
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)

    def test_unknown_shell_state_warns_but_never_hard_blocks(self):
        self.register_test(known=False)
        agent, _ = self.make_agent([tools(call(f"test-{i}")) for i in range(7)] + [done()])
        agent.run("Diagnose external test environment")
        self.assertEqual(len(self.executed), 7)
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)
        self.assertIn("仅提醒", self.results(agent)["test-2"]["content"])
        self.assertFalse(self.events_of("tool.blocked"))

    def test_uncertain_results_and_unknown_errors_do_not_hard_block(self):
        for outcome in ("unknown_result", "diagnostic", ""):
            with self.subTest(outcome=outcome):
                self.registry = ToolRegistry()
                self.executed = []
                self.register_test(output=lambda: failure(outcome=outcome, deterministic=False))
                agent, _ = self.make_agent([tools(call(f"test-{i}")) for i in range(6)] + [done()])
                agent.run("Investigate uncertain result")
                self.assertEqual(len(self.executed), 6)
                self.assertFalse(self.events_of("tool.blocked"))
                self.assertEqual(agent._loop_guard.state.blocked_attempts, 0)

    def test_soft_failures_do_not_count_toward_trusted_failure_threshold(self):
        outputs = iter([failure(deterministic=False) for _ in range(3)] + [failure(), failure()])
        self.register_test(output=lambda: next(outputs))
        agent, _ = self.make_agent([
            tools(call(f"test-{i}")) for i in range(5)
        ] + [done()])
        result = agent.run("Recheck uncertain diagnostic evidence")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(self.executed), 5)
        self.assertEqual([item["known_failure"] for item in agent._loop_guard.state.recent],
                         [False, False, False, True, True])
        self.assertFalse(next(iter(agent._loop_guard.state.issues.values()))["hard"])
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)
        self.assertFalse(self.events_of("agent.loop_warning")[0].payload["hard"])
        self.assertFalse(self.events_of("tool.blocked"))
        self.assertNotIn("未执行", self.results(agent)["test-4"]["content"])

    def test_hard_upgrade_requires_new_warning_delivery_before_blocking(self):
        outputs = iter([failure(deterministic=False) for _ in range(3)] + [failure() for _ in range(4)])
        self.register_test(output=lambda: next(outputs))
        captured = {}
        hooks = HookManager()

        def capture_after_guard(use, output):
            captured[use.id] = deepcopy(next(iter(agent._loop_guard.state.issues.values())))

        hooks.register("PostToolUse", capture_after_guard)
        agent, client = self.make_agent(self.train() + [
            tools(*(call(f"trusted-{i}") for i in range(4))),
            tools(call("blocked")), done(),
        ], hooks=hooks)
        agent.run("Confirm the diagnostic before restricting retries")
        self.assertEqual(len(self.executed), 7)
        self.assertFalse(captured["trusted-0"]["hard"])
        self.assertFalse(captured["trusted-1"]["hard"])
        self.assertTrue(captured["trusted-2"]["hard"])
        self.assertEqual(captured["trusted-2"]["count"], 3)
        self.assertIsNone(captured["trusted-2"]["seen_at"])
        self.assertIsNone(captured["trusted-3"]["seen_at"])
        self.assertEqual([event.payload["hard"] for event in self.events_of("agent.loop_warning")],
                         [False, True])
        results = self.results(agent)
        self.assertIn("运行时纠正", results["trusted-2"]["content"])
        self.assertNotIn("未执行", results["trusted-3"]["content"])
        self.assertIn("未执行", results["blocked"]["content"])
        self.assertIn("仅提醒", client.calls[3]["system"])
        self.assertIn("相同输入状态下", client.calls[4]["system"])

    def test_successful_rereads_and_paging_remain_available(self):
        self.register_read()
        calls = [call(f"read-{i}", "read_file", {"file_path": "implementation.py", "offset": offset})
                 for i, offset in enumerate([0, 0, 0, 1, 2, 0, 1, 2])]
        agent, _ = self.make_agent([tools(item) for item in calls] + [done()])
        agent.run("Read code")
        self.assertEqual(len(self.executed), 8)
        self.assertTrue(all(not result.get("is_error") for result in self.results(agent).values()))
        self.assertFalse(self.events_of("agent.loop_warning"))

    def test_alternating_operations_alone_do_not_trigger_a_stop(self):
        self.register_test()
        commands = ["pytest 'case a.py'", 'pytest "case a.py"'] * 2
        agent, _ = self.make_agent([
            tools(call(f"test-{i}", arguments={"command": command}))
            for i, command in enumerate(commands)
        ] + [done()])
        self.assertEqual(agent.run("Compare test invocations").stop_reason, "end_turn")
        self.assertEqual(self.executed, commands)
        self.assertEqual(len(agent._loop_guard.state.issues), 2)
        self.assertFalse(self.events_of("agent.loop_warning"))

    def test_edit_test_edit_test_and_readback_are_not_stopped(self):
        self.register_test()
        self.register_read()
        self.registry.register(EditFileTool(workspace_guard=WorkspaceGuard(self.root)))
        script = []
        for index in range(1, 6):
            script.extend([
                tools(call(f"edit-{index}", "edit_file", {"file_path": "implementation.py", "old_string": str(index), "new_string": str(index + 1)})),
                tools(call(f"read-{index}", "read_file", {"file_path": "implementation.py"})),
                tools(call(f"test-{index}")),
            ])
        agent, _ = self.make_agent(script + [done()])
        result = agent.run("Iteratively diagnose and repair")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(self.executed), 10)
        self.assertFalse(self.events_of("agent.loop_warning"))
        self.assertFalse(self.events_of("tool.blocked"))
        self.assertEqual(len(self.results(agent)), 15)

    def test_invalid_edit_warns_on_second_blocks_third_and_accepts_correction(self):
        for category in ("empty", "noop"):
            with self.subTest(category=category):
                self.registry = ToolRegistry()
                self.code.write_text("value = 1\n", encoding="utf-8")
                self.registry.register(EditFileTool(workspace_guard=WorkspaceGuard(self.root)))
                args = {"file_path": "implementation.py", "new_string": "1"}
                if category == "noop":
                    args["old_string"] = "1"
                second = {**args, "file_path": "unrelated.py", "extra": "unrelated"}
                if category == "empty":
                    second["old_string"] = ""
                agent, _ = self.make_agent([
                    tools(call("invalid-1", "edit_file", args)),
                    tools(call("invalid-2", "edit_file", second)),
                    tools(call("blocked", "edit_file", args)),
                    tools(call("corrected", "edit_file", {"file_path": "implementation.py", "old_string": "1", "new_string": "2"})), done(),
                ])
                agent.run("Edit implementation")
                results = self.results(agent)
                self.assertIn("Error:", results["invalid-1"]["content"])
                self.assertNotIn("运行时纠正", results["invalid-1"]["content"])
                self.assertIn("运行时纠正", results["invalid-2"]["content"])
                self.assertIn("未执行", results["blocked"]["content"])
                self.assertNotIn("is_error", results["corrected"])
                self.assertEqual(len(self.events_of("tool.started")), 1)
                self.assertEqual(len(self.events_of("agent.loop_warning")), 1)
                self.assertEqual(len(agent._loop_guard.state.recent), 1)
                self.assertEqual(self.code.read_text(encoding="utf-8"), "value = 2\n")

    def test_four_repeats_in_one_response_execute_before_model_sees_warning(self):
        self.register_test()
        agent, _ = self.make_agent([
            tools(*(call(f"batch-{i}") for i in range(4))), tools(call("blocked")), done(),
        ])
        agent.run("Diagnose")
        self.assertEqual(len(self.executed), 4)
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)
        results = self.results(agent)
        self.assertNotIn("未执行", results["batch-3"]["content"])
        self.assertNotIn("运行时纠正", results["batch-3"]["content"])
        self.assertIn("未执行", results["blocked"]["content"])
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, 5)
        self.assertEqual(len(agent._loop_guard.state.recent), 4)

    def test_two_empty_or_thinking_only_responses_stop(self):
        for content in ([], [{"type": "text", "text": "  \n"}],
                        [{"type": "thinking", "thinking": "internal reasoning", "signature": "opaque"}]):
            with self.subTest(content=content):
                agent, client = self.make_agent([ModelResponse("end_turn", deepcopy(content)) for _ in range(2)])
                result = agent.run("Answer")
                self.assertEqual(result.stop_reason, "loop_detected:empty_response")
                self.assertEqual(len(client.calls), 2)
                self.assertEqual(agent._loop_guard.state.empty_responses, 2)
                self.assertFalse(self.events_of("agent.completed"))
                self.assertIn("上一轮未返回", client.calls[1]["system"])

    def test_two_empty_max_tokens_responses_stop_before_recovery_can_continue(self):
        agent, client = self.make_agent([ModelResponse("max_tokens", []) for _ in range(2)])
        result = agent.run("Answer without an empty continuation loop")
        self.assertEqual(result.stop_reason, "loop_detected:empty_response")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(agent._loop_guard.state.response_seq, 2)
        self.assertEqual(agent._loop_guard.state.empty_responses, 2)
        self.assertGreater(client.calls[1]["max_tokens"], client.calls[0]["max_tokens"])
        self.assertEqual(agent._loop_guard.budget.state.model_calls, 2)
        self.assertFalse(self.events_of("agent.completed"))
        self.results(agent)

    def test_nonempty_max_tokens_resets_empty_count_before_recovery_continuation(self):
        partial = "The failure is in the parser; inspect its boundary handling."
        agent, client = self.make_agent([
            ModelResponse("max_tokens", []),
            ModelResponse("max_tokens", [{"type": "text", "text": partial}]),
            ModelResponse("end_turn", []), done("Useful final diagnosis"),
        ])
        result = agent.run("Analyze the failure")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.final_text, "Useful final diagnosis")
        self.assertEqual(len(client.calls), 4)
        self.assertGreater(client.calls[1]["max_tokens"], client.calls[0]["max_tokens"])
        self.assertIn(partial, repr(client.calls[2]["messages"]))
        self.assertNotIn("上一轮未返回有效正文", client.calls[2]["system"])
        self.assertIn("上一轮未返回有效正文", client.calls[3]["system"])
        self.assertEqual(agent._loop_guard.state.response_seq, 4)
        self.assertEqual(agent._loop_guard.state.empty_responses, 0)
        self.assertFalse(self.events_of("agent.loop_stopped"))
        self.results(agent)

    def test_real_tool_call_resets_empty_response_count(self):
        self.register_read()
        agent, client = self.make_agent([
            ModelResponse("end_turn", []),
            tools(call("read", "read_file", {"file_path": "implementation.py"})),
            ModelResponse("end_turn", []), done(),
        ])
        self.assertEqual(agent.run("Read then answer").stop_reason, "end_turn")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(agent._loop_guard.state.empty_responses, 0)

    def test_valid_text_resets_empty_count_even_when_stop_hook_continues(self):
        hooks = HookManager()
        stops = []

        def continue_once(messages):
            stops.append(1)
            return "Continue once" if len(stops) == 1 else None

        hooks.register("Stop", continue_once)
        agent, client = self.make_agent([
            ModelResponse("end_turn", []), done("A useful diagnosis"),
            ModelResponse("end_turn", []), done(),
        ], hooks=hooks)
        self.assertEqual(agent.run("Investigate").stop_reason, "end_turn")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(agent._loop_guard.state.empty_responses, 0)

    def test_other_hook_denials_do_not_count_as_executed_failures(self):
        self.register_test()
        hooks = HookManager()
        hooks.register("PreToolUse", lambda use: "Denied by existing permission hook")
        agent, _ = self.make_agent([tools(call(f"denied-{i}")) for i in range(5)] + [done()], hooks=hooks)
        agent.run("Run test")
        self.assertEqual(self.executed, [])
        self.assertFalse(self.events_of("tool.started"))
        self.assertEqual(agent._loop_guard.state.recent, [])
        self.assertEqual(agent._loop_guard.state.issues, {})
        self.assertEqual(agent._loop_guard.state.blocked_attempts, 0)
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, 5)
        self.assertTrue(all(result["is_error"] for result in self.results(agent).values()))

    def test_empty_string_pre_tool_refusal_never_executes_or_records_failure(self):
        self.register_test()
        observed = []
        hooks = HookManager()
        hooks.register("PreToolUse", lambda use: "")
        hooks.register("PostToolUse", lambda use, output: observed.append(use.id))
        agent, _ = self.make_agent([
            tools(call("denied-1"), call("denied-2")), done(),
        ], hooks=hooks)
        self.assertEqual(agent.run("Run test").stop_reason, "end_turn")
        self.assertEqual(self.executed, [])
        self.assertEqual(observed, [])
        self.assertFalse(self.events_of("tool.started"))
        self.assertEqual(len(self.events_of("tool.blocked")), 2)
        self.assertEqual(agent._loop_guard.state.recent, [])
        self.assertEqual(agent._loop_guard.state.issues, {})
        results = self.results(agent)
        self.assertEqual(set(results), {"denied-1", "denied-2"})
        self.assertTrue(all(result["is_error"] for result in results.values()))

    def test_existing_post_hook_and_guard_each_observe_execution_once(self):
        self.register_test()
        observed = []
        hooks = HookManager()
        hooks.register("PostToolUse", lambda use, output: observed.append((use.id, output.status)))
        agent, _ = self.make_agent(self.train() + [tools(call("blocked")), done()], hooks=hooks)
        agent.run("Test")
        self.assertEqual(observed, [(f"failure-{i}", "error") for i in range(3)])
        self.assertEqual(len(agent._loop_guard.state.recent), 3)
        self.assertEqual(len(self.events_of("agent.loop_warning")), 1)

    def test_resume_checkpoint_preserves_same_scope_budget_and_block(self):
        self.register_test()
        first, _ = self.make_agent(self.train() + [done()])
        first.run("Test", execution_id="same")
        checkpoint = json.loads(json.dumps(first.export_execution_state()))
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                resumed, client = self.make_agent([tools(call("blocked")), done()], messages=first.messages)
                resumed.restore_execution_state(checkpoint)
                result = resumed.run("Continue", execution_id="same") if explicit else resumed.run(None)
                self.assertEqual(result.stop_reason, "end_turn")
                self.assertEqual(len(self.executed), 3)
                self.assertEqual(resumed._loop_guard.state.scope_id, "same")
                self.assertEqual(resumed._loop_guard.budget.state.model_calls, checkpoint["budget"]["model_calls"] + 2)
                self.assertEqual(resumed._loop_guard.budget.state.tool_calls, checkpoint["budget"]["tool_calls"] + 1)
                self.assertIn("相同输入状态", client.calls[0]["system"])
                self.assertIn("未执行", self.results(resumed)["blocked"]["content"])

    def test_run_none_retains_explicit_execution_id_despite_different_event_run_id(self):
        self.register_test()
        agent, client = self.make_agent(
            self.train() + [done(), tools(call("blocked")), done()],
            event_context=ExecutionContext(run_id="transport-run"),
        )
        agent.run("Test", execution_id="logical-task")
        before = agent.export_execution_state()
        self.assertEqual(agent.event_emitter.context.run_id, "transport-run")
        agent.run(None)
        self.assertEqual(agent._loop_guard.state.scope_id, "logical-task")
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(agent._loop_guard.budget.state.model_calls, before["budget"]["model_calls"] + 2)
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, before["budget"]["tool_calls"] + 1)
        self.assertEqual(agent._loop_guard.state.blocked_attempts, 1)
        self.assertIn("相同输入状态", client.calls[4]["system"])
        self.assertIn("未执行", self.results(agent)["blocked"]["content"])

    def test_pending_process_checkpoint_blocks_same_command_cwd_but_allows_query(self):
        command = "pytest test_example.py"
        query = "Get-Process -Id 4321"
        cwd = [str(self.root)]

        def shell(command, timeout=120):
            self.executed.append((command, timeout, cwd[0]))
            if command == query:
                return ToolOutput("Process 4321 is still running")
            return ToolOutput("Error: timed out; process still running", status="error",
                              outcome="timeout", process_id=4321, process_running=True)

        self.registry.register_handler(ToolDefinition("bash", "Fake shell", {
            "required": ["command"], "properties": {
                "command": {"type": "string"}, "timeout": {"type": "integer"},
            },
        }), shell, input_state=lambda args: ToolInputState(cwd=cwd[0]))
        first, _ = self.make_agent([
            tools(call("launched", arguments={"command": command, "timeout": 1})), done(),
        ])
        first.run("Run local test", execution_id="same")
        checkpoint = json.loads(json.dumps(first.export_execution_state()))
        self.assertEqual(list(checkpoint["state"]["pending_processes"].values()), [4321])
        self.assertFalse(checkpoint["state"]["recent"][0]["state_known"])
        resumed, _ = self.make_agent([
            tools(call("blocked", arguments={"command": command, "timeout": 99})),
            tools(call("query", arguments={"command": query})), done(),
        ], messages=first.messages)
        resumed.restore_execution_state(checkpoint)
        self.assertEqual(resumed.run(None).stop_reason, "end_turn")
        self.assertEqual([item[0] for item in self.executed], [command, query])
        results = self.results(resumed)
        self.assertIn("4321", results["blocked"]["content"])
        self.assertIn("未执行", results["blocked"]["content"])
        self.assertTrue(results["blocked"]["is_error"])
        self.assertNotIn("is_error", results["query"])
        self.assertEqual(resumed._loop_guard.state.pending_processes, checkpoint["state"]["pending_processes"])
        self.assertEqual(resumed._loop_guard.budget.state.tool_calls, 3)

        # The key includes cwd; the same command in another workspace is distinct.
        cwd[0] = str(self.root / "another-workspace")
        different, _ = self.make_agent([tools(call("other-cwd")), done()], messages=first.messages)
        different.restore_execution_state(checkpoint)
        different.run(None)
        self.assertEqual([item[0] for item in self.executed], [command, query, command])
        self.assertFalse(self.events_of("tool.blocked"))

    def test_stopped_checkpoint_stays_stopped_but_new_prompt_starts_fresh_scope(self):
        self.register_test()
        first, _ = self.make_agent(self.train() + [tools(call(f"blocked-{i}")) for i in range(3)])
        first.run("Test", execution_id="same")
        resumed, client = self.make_agent([tools(call("new-test")), done()], messages=first.messages)
        resumed.restore_execution_state(json.loads(json.dumps(first.export_execution_state())))
        result = resumed.run(None)
        self.assertEqual(result.stop_reason, "loop_detected:blocked_recovery_exhausted")
        self.assertEqual(client.calls, [])
        result = resumed.run("Start a new independent task")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertNotEqual(resumed._loop_guard.state.scope_id, "same")
        self.assertEqual(resumed._loop_guard.state.blocked_attempts, 0)
        self.assertEqual(resumed._loop_guard.budget.state.model_calls, 2)
        self.assertEqual(resumed._loop_guard.budget.state.tool_calls, 1)
        self.assertEqual(len(self.executed), 4)

    def test_independent_agent_does_not_inherit_another_agents_block(self):
        self.register_test()
        first, _ = self.make_agent(self.train() + [done()])
        first.run("Test", execution_id="first")
        second, _ = self.make_agent([tools(call("independent")), done()])
        second.run("Test", execution_id="second")
        self.assertEqual(len(self.executed), 4)
        self.assertEqual(second._loop_guard.state.blocked_attempts, 0)
        self.assertEqual(second._loop_guard.budget.state.tool_calls, 1)
        self.assertEqual(first._loop_guard.budget.state.tool_calls, 3)

    def test_history_projection_without_failure_text_preserves_active_feedback(self):
        self.register_test()
        first, _ = self.make_agent(self.train() + [done()])
        first.run("Test", execution_id="same")
        checkpoint = first.export_execution_state()
        # Simulate a compacted checkpoint containing no failure details. The
        # guard must restore from execution facts, never reconstruct from text.
        projected = [{"role": "user", "content": "Earlier work was summarized; continue the repair."}]
        resumed, client = self.make_agent([tools(call("blocked")), done()], messages=projected)
        resumed.context.state.summary_revision += 1
        resumed.restore_execution_state(checkpoint)
        resumed.run(None)
        self.assertEqual(len(self.executed), 3)
        self.assertIn("相同输入状态", client.calls[0]["system"])
        self.assertIn("未执行", self.results(resumed)["blocked"]["content"])
        self.assertEqual(resumed._loop_guard.budget.state.tool_calls, 4)

    def test_real_compaction_between_warning_and_next_response_preserves_guard(self):
        self.register_test(output=lambda: ToolOutput(
            "FAILED test_example::test_value\n" + "diagnostic detail " * 400,
            status="error", outcome="diagnostic", deterministic=True, result_signature="assertion",
        ))
        agent, client = self.make_agent(self.train() + [tools(call("blocked")), done()])
        summary_client = ScriptedClient([done("Earlier investigation is summarized. Continue the repair.")])
        captured = []

        def compact_once(boundary):
            if len(agent._loop_guard.state.recent) != 3 or captured:
                return
            before = agent.export_execution_state()
            config = agent.context.config
            # Automatic context management stays off; explicitly exercise one
            # real compaction using a finite fake summary client.
            config.mode = "model"
            config.summarization_model = "fake-summary"
            config.recency_rounds = 1
            config.recency_messages = 2
            config.min_fold_messages = 2
            try:
                agent.context.force_compact(
                    agent.messages, client=BudgetedClient(summary_client, agent._loop_guard.budget),
                    event_emitter=agent.event_emitter,
                )
            finally:
                config.mode = "off"
            captured.append((before, agent.export_execution_state()))

        agent.boundary_callback = compact_once
        agent.run("Repair test")
        self.assertEqual(agent.context.state.summary_revision, 1)
        self.assertEqual(agent.context.last_compaction["status"], "written")
        self.assertEqual(len(summary_client.calls), 1)
        before, after = captured[0]
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(after["budget"]["model_calls"], before["budget"]["model_calls"] + 1)
        self.assertIn("context_summary", repr(client.calls[3]["messages"]))
        self.assertIn("相同输入状态", client.calls[3]["system"])
        self.assertEqual(len(self.executed), 3)
        self.assertIn("未执行", self.results(agent)["blocked"]["content"])

    def test_pause_before_warning_delivery_restores_feedback_and_budget(self):
        self.register_test()
        first, first_client = self.make_agent(self.train())

        def pause(boundary):
            if len(first._loop_guard.state.recent) == 3:
                first.request_yield("checkpoint")

        first.boundary_callback = pause
        waiting = first.run_until_yield("Diagnose test")
        self.assertEqual(waiting.stop_reason, "waiting:checkpoint")
        self.assertTrue(waiting.yielded)
        self.assertEqual(len(first_client.calls), 3)
        snapshot = json.loads(json.dumps(first.export_execution_state()))
        self.assertTrue(all(issue["seen_at"] is None for issue in snapshot["state"]["issues"].values()))
        resumed, client = self.make_agent([tools(call("blocked")), done()], messages=first.messages)
        resumed.restore_execution_state(snapshot)
        resumed.run(None)
        self.assertEqual(resumed._loop_guard.state.scope_id, snapshot["state"]["scope_id"])
        self.assertEqual(resumed._loop_guard.budget.state.model_calls, 5)
        self.assertIn("相同输入状态", client.calls[0]["system"])
        self.assertEqual(len(self.executed), 3)
        self.assertIn("未执行", self.results(resumed)["blocked"]["content"])

    def test_observation_and_issue_windows_are_bounded(self):
        self.register_test()
        agent, _ = self.make_agent([
            tools(call(f"test-{i}", arguments={"command": f"pytest case_{i}.py"})) for i in range(20)
        ] + [done()])
        agent.run("Inspect distinct failures")
        snapshot = agent.export_execution_state()
        self.assertEqual(len(snapshot["state"]["recent"]), 12)
        self.assertEqual(len(snapshot["state"]["issues"]), 12)
        self.assertEqual(snapshot["budget"]["tool_calls"], 20)
        self.assertFalse(self.events_of("agent.loop_warning"))

    def test_tool_budget_stops_mid_batch_and_pairs_unexecuted_results(self):
        self.register_read()
        agent, client = self.make_agent([
            tools(*(call(f"read-{i}", "read_file", {"file_path": "implementation.py"}) for i in range(4))),
        ], max_tool_calls=2)
        result = agent.run("Read code")
        self.assertEqual(result.stop_reason, "budget_exceeded:tool_calls")
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(len(client.calls), 1)
        results = self.results(agent)
        self.assertEqual(len(results), 4)
        for identifier in ("read-2", "read-3"):
            self.assertTrue(results[identifier]["is_error"])
            self.assertIn("未执行", results[identifier]["content"])

    def test_model_budget_limits_unknown_usage_and_programmatic_finalization(self):
        self.register_read()
        agent, client = self.make_agent([
            tools(call(f"read-{i}", "read_file", {"file_path": "implementation.py"})) for i in range(2)
        ], max_model_calls=2)
        result = agent.run("Read code")
        self.assertEqual(result.stop_reason, "budget_exceeded:model_calls")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(agent._loop_guard.budget.state.unknown_usage_calls, 2)
        self.assertIn("用量未知", result.final_text)
        self.assertFalse(self.events_of("agent.completed"))

    def test_reported_tokens_enforce_budget_before_next_tool_starts(self):
        self.register_read()
        response = tools(call("not-started", "read_file", {"file_path": "implementation.py"}))
        response.usage = TokenUsage(input_tokens=70, output_tokens=10, cache_read_input_tokens=30)
        agent, client = self.make_agent([response], max_total_tokens=100)
        result = agent.run("Read code")
        self.assertEqual(result.stop_reason, "budget_exceeded:tokens")
        self.assertEqual(agent._loop_guard.budget.state.total_tokens, 110)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.executed, [])
        self.assertFalse(self.events_of("tool.started"))
        self.results(agent)  # History stays protocol-valid even if response admission stops.

    def test_safe_transient_retry_is_bounded_and_charged_once_per_attempt(self):
        self.register_test(output=lambda: ToolOutput(
            "Error: temporarily unavailable", status="error", outcome="transient",
            retryable=True, retry_safe=True,
        ))
        agent, client = self.make_agent([tools(call("transient")), done()])
        agent.run("Diagnose transient service failure")
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, 3)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(self.events_of("tool.retry_scheduled")), 2)
        self.assertEqual(len(self.results(agent)), 1)
        self.assertEqual(agent._loop_guard.state.issues, {})

    def test_transient_without_replay_safety_is_not_retried(self):
        self.register_test(output=lambda: ToolOutput(
            "Error: response lost after write", status="error", outcome="transient",
            retryable=True, retry_safe=False,
        ))
        agent, _ = self.make_agent([tools(call("uncertain")), done()])
        agent.run("Verify uncertain write")
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(agent._loop_guard.budget.state.tool_calls, 1)
        self.assertFalse(self.events_of("tool.retry_scheduled"))


if __name__ == "__main__":
    unittest.main()
