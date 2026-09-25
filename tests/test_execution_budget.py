"""Budgets exercised through the real loop, recovery and tool dispatch."""
from __future__ import annotations

from copy import deepcopy
import threading
import unittest

from codeagent import Agent, AgentConfig, HookManager, ModelResponse, RecoveryConfig, RecoveryRuntime, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import TokenUsage
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import validate_tool_history
from codeagent.memory import MemoryConfig
from codeagent.permissions.broker import CliPermissionBroker
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.execution import ExecutionStopped, RunBudget
from codeagent.tools.base import ToolOutput


def final(text="done", usage=None):
    return ModelResponse("end_turn", [{"type": "text", "text": text}], usage=usage)


def calls(*names):
    return ModelResponse("tool_use", [{"type": "tool_use", "id": f"call-{i}", "name": name,
                                      "input": {}} for i, name in enumerate(names)])


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def fork(self, **kwargs):
        return self


class ExecutionBudgetTests(unittest.TestCase):
    def agent(self, client, *, tools=None, limits=None, hooks=None, **kwargs):
        return Agent(client=client, tools=tools or ToolRegistry(),
                     config=AgentConfig(model="fake", loop_guard=limits or LoopGuardConfig()),
                     context=ContextManager(config=ContextConfig(mode="off")), hooks=hooks or HookManager(),
                     recovery_runtime=RecoveryRuntime(RecoveryConfig(sleep_enabled=False)), **kwargs)

    def test_tool_budget_stops_mid_batch_and_preserves_pairing(self):
        executed = []
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: executed.append(1) or "saved")
        client = Client(calls("probe", "probe", "probe"), final())
        agent = self.agent(client, tools=tools, limits=LoopGuardConfig(max_tool_calls=2))
        result = agent.run("start")
        self.assertEqual(result.stop_reason, "budget_exceeded:tool_calls")
        self.assertEqual(len(executed), 2)
        self.assertEqual(len(client.calls), 1)
        results = agent.messages[-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["call-0", "call-1", "call-2"])
        self.assertEqual(results[0]["content"], "saved")
        self.assertIn("未执行", results[-1]["content"])
        validate_tool_history(agent.messages)

    def test_provider_retry_is_charged_and_cannot_retry_budget_stop(self):
        client = Client(RuntimeError("temporary"), RuntimeError("temporary"), final())
        agent = self.agent(client, limits=LoopGuardConfig(max_model_calls=2))
        result = agent.run("start")
        self.assertEqual(result.stop_reason, "budget_exceeded:model_calls")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(agent.export_execution_state()["budget"]["unknown_usage_calls"], 2)

    def test_safe_transient_retry_is_bounded_and_charged(self):
        executed = []
        tools = ToolRegistry()
        def transient():
            executed.append(1)
            return ToolOutput("Error: service unavailable", status="error", outcome="transient",
                              retryable=True, retry_safe=True)
        tools.register_handler(ToolDefinition("probe", "", {}), transient)
        agent = self.agent(Client(calls("probe"), final()), tools=tools,
                           limits=LoopGuardConfig(retry_delay_seconds=0))
        self.assertEqual(agent.run("start").final_text, "done")
        self.assertEqual(len(executed), 3)
        self.assertEqual(agent.export_execution_state()["budget"]["tool_calls"], 3)

    def test_retry_cannot_exceed_tool_budget(self):
        executed = []
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: executed.append(1) or ToolOutput(
            "Error: temporary", status="error", outcome="transient", retryable=True, retry_safe=True))
        agent = self.agent(Client(calls("probe")), tools=tools,
                           limits=LoopGuardConfig(max_tool_calls=2, retry_delay_seconds=0))
        self.assertEqual(agent.run("start").stop_reason, "budget_exceeded:tool_calls")
        self.assertEqual(len(executed), 2)
        validate_tool_history(agent.messages)

    def test_diagnostic_and_unsafe_transient_are_not_automatically_retried(self):
        for output in (
            ToolOutput("FAILED test_case", status="error", outcome="diagnostic", deterministic=True),
            ToolOutput("Error: unknown write", status="error", outcome="transient", retryable=True),
        ):
            with self.subTest(outcome=output.outcome):
                executed = []
                tools = ToolRegistry()
                tools.register_handler(ToolDefinition("probe", "", {}), lambda: executed.append(1) or output)
                agent = self.agent(Client(calls("probe"), final()), tools=tools)
                agent.run("start")
                self.assertEqual(len(executed), 1)

    def test_reported_total_tokens_include_cache_and_no_extra_finalization_call(self):
        usage = TokenUsage(input_tokens=3, cache_read_input_tokens=4, output_tokens=4)
        client = Client(final(usage=usage), final("must not be called"))
        agent = self.agent(client, limits=LoopGuardConfig(max_total_tokens=10))
        result = agent.run("start")
        self.assertEqual(result.stop_reason, "budget_exceeded:tokens")
        self.assertIn("11", result.final_text)
        self.assertEqual(len(client.calls), 1)

    def test_missing_usage_is_unknown_and_count_limit_still_ends(self):
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: "ok")
        agent = self.agent(Client(calls("probe"), final()), tools=tools,
                           limits=LoopGuardConfig(max_model_calls=1))
        result = agent.run("start")
        self.assertEqual(result.stop_reason, "budget_exceeded:model_calls")
        self.assertIn("用量未知", result.final_text)

    def test_unlimited_tokens_still_account_usage_and_enforce_call_limit(self):
        usage = TokenUsage(input_tokens=400_000, cache_read_input_tokens=100_000, output_tokens=4)
        agent = self.agent(Client(final(usage=usage)), limits=LoopGuardConfig(max_total_tokens=0))
        self.assertEqual(agent.run("start").final_text, "done")
        self.assertEqual(agent.export_execution_state()["budget"]["total_tokens"], 500_004)

        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: "ok")
        response = ModelResponse("tool_use", calls("probe").content, usage=usage)
        agent = self.agent(Client(response, final()), tools=tools,
                           limits=LoopGuardConfig(max_total_tokens=0, max_model_calls=1))
        self.assertEqual(agent.run("start").stop_reason, "budget_exceeded:model_calls")

    def test_side_client_and_forks_share_root_budget(self):
        agent = self.agent(Client(final(), final(), final()), limits=LoopGuardConfig(max_model_calls=2))
        side = agent._side_query_client("context_summary")
        request = dict(model="fake", system="", messages=[], tools=[], max_tokens=10)
        side.create_message(**request)
        side.fork(call_kind="memory").create_message(**request)
        with self.assertRaises(ExecutionStopped):
            side.create_message(**request)
        self.assertEqual(len(agent.client.calls), 2)

    def test_last_memory_call_cannot_exceed_budget_and_report_success(self):
        class Memory:
            config = MemoryConfig(selection_mode="simple")

            def select_context(self, *args, **kwargs):
                return ""

            def after_turn(self, messages, *, client, model, max_tokens):
                client.create_message(model=model, system="", messages=[], tools=[], max_tokens=max_tokens)

        client = Client(final(usage=TokenUsage(output_tokens=1)), final(usage=TokenUsage(output_tokens=10)))
        agent = self.agent(client, memory_manager=Memory(), limits=LoopGuardConfig(max_total_tokens=5))
        self.assertEqual(agent.run("start").stop_reason, "budget_exceeded:tokens")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(agent.export_execution_state()["budget"]["total_tokens"], 11)

    def test_provider_error_is_not_replaced_by_post_request_budget_check(self):
        now = [0.0]
        budget = RunBudget(LoopGuardConfig(max_active_seconds=1), clock=lambda: now[0])
        class FailingClient:
            def create_message(self, **kwargs):
                now[0] = 2
                raise OSError("connection failed")
        with budget.running():
            with self.assertRaisesRegex(OSError, "connection failed"):
                budget.invoke(FailingClient())
        self.assertEqual(budget.snapshot()["unknown_usage_calls"], 1)

    def test_subagent_cannot_escape_root_tool_budget(self):
        executed = []
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: executed.append(1) or "ok")
        child = ModelResponse("tool_use", [{"type": "tool_use", "id": "spawn", "name": "subagent",
                                           "input": {"description": "investigate"}}])
        agent = self.agent(Client(child, calls("probe")), tools=tools,
                           limits=LoopGuardConfig(max_tool_calls=1))
        result = agent.run("delegate")
        self.assertEqual(result.stop_reason, "budget_exceeded:tool_calls")
        self.assertEqual(executed, [])
        self.assertEqual(agent.export_execution_state()["budget"]["model_calls"], 2)
        validate_tool_history(agent.messages)

    def test_time_budget_preserves_returned_tool_facts(self):
        now = [0.0]
        tools = ToolRegistry()
        def slow():
            now[0] += 6
            return ToolOutput("saved", changed_files=("a.py",))
        tools.register_handler(ToolDefinition("probe", "", {}), slow)
        agent = self.agent(Client(calls("probe"), final()), tools=tools,
                           limits=LoopGuardConfig(max_active_seconds=5))
        agent._loop_guard.budget.clock = lambda: now[0]
        result = agent.run("start")
        self.assertEqual(result.stop_reason, "budget_exceeded:active_time")
        self.assertEqual(agent.messages[-1]["content"][0]["content"], "saved")
        self.assertIn("a.py", result.final_text)
        self.assertEqual(len(agent.client.calls), 1)

    def test_cancel_has_priority_and_does_not_force_finalization(self):
        token = CancellationToken()
        token.cancel()
        agent = self.agent(Client(final()), cancellation=token)
        with self.assertRaises(CancelledError):
            agent.run("start")
        self.assertEqual(agent.client.calls, [])

    def test_denied_calls_consume_budget_but_not_failure_records(self):
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {}), lambda: self.fail("not allowed"))
        hooks = HookManager()
        hooks.register("PreToolUse", lambda _: "Permission denied")
        agent = self.agent(Client(calls("probe", "probe", "probe")), tools=tools, hooks=hooks,
                           limits=LoopGuardConfig(max_tool_calls=2))
        self.assertEqual(agent.run("start").stop_reason, "budget_exceeded:tool_calls")
        self.assertEqual(agent.export_execution_state()["state"]["recent"], [])


class RunBudgetTests(unittest.TestCase):
    def test_cli_approval_wait_does_not_consume_active_budget(self):
        now = [0.0]
        clock = lambda: now[0]
        budget = RunBudget(LoopGuardConfig(max_active_seconds=10), clock=clock)
        activity = ExecutionActivity(CancellationToken(), clock=clock)
        activity.execution_budget = budget

        def approve(*args):
            now[0] += 100
            return True

        broker = CliPermissionBroker(prompt=approve, execution_activity=activity)
        with budget.running():
            now[0] = 2
            self.assertTrue(broker.request("bash", {}, "test"))
            now[0] += 3
            activity.check()
        self.assertEqual(budget.snapshot()["active_seconds"], 5)

    def test_active_intervals_exclude_human_wait_and_do_not_double_count_children(self):
        now = [0.0]
        budget = RunBudget(LoopGuardConfig(max_active_seconds=10), clock=lambda: now[0])
        with budget.running():
            now[0] = 2
            with budget.running():
                now[0] = 4
                with budget.paused():
                    now[0] = 104
                now[0] = 107
        self.assertEqual(budget.snapshot()["active_seconds"], 7)
        restored = RunBudget(budget.config, clock=lambda: now[0])
        restored.restore(budget.snapshot())
        now[0] = 1000  # Offline time is not active work.
        with restored.running():
            now[0] = 1004
            with self.assertRaises(ExecutionStopped):
                restored.check()

    def test_atomic_reservations_do_not_oversubscribe(self):
        budget = RunBudget(LoopGuardConfig(max_tool_calls=5))
        accepted = []
        def reserve():
            try:
                budget.reserve("tool")
                accepted.append(1)
            except ExecutionStopped:
                pass
        threads = [threading.Thread(target=reserve) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(accepted), 5)
        self.assertEqual(budget.snapshot()["tool_calls"], 5)


if __name__ == "__main__":
    unittest.main()
