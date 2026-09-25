from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import ContextConfig, ContextManager, EnvironmentConfig, MemoryConfig, ModelResponse, PromptMode
from codeagent.cli import create_default_subagent_environment, main, print_run_result
from codeagent.events import EventEmitter, ExecutionContext, TokenTotals
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import ToolUse
from codeagent.permissions import CliPermissionBroker, PermissionPolicy, WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.execution import RunBudget
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def done():
    return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done"}])


class LoopGuardConfigTests(unittest.TestCase):
    def test_environment_values_reach_agent_config(self):
        settings = {
            "MODEL_ID": "fake",
            "CONTEXT_COMPACT_MODE": "off",
            "CODEAGENT_LOOP_WINDOW": "16",
            "CODEAGENT_LOOP_REPEAT_FAILURE_LIMIT": "4",
            "CODEAGENT_LOOP_PARAMETER_ERROR_LIMIT": "3",
            "CODEAGENT_LOOP_BLOCKED_ATTEMPT_LIMIT": "5",
            "CODEAGENT_LOOP_EMPTY_RESPONSE_LIMIT": "3",
            "CODEAGENT_LOOP_TOOL_MAX_RETRIES": "0",
            "CODEAGENT_LOOP_RETRY_DELAY_SECONDS": "0.5",
            "CODEAGENT_RUN_MAX_MODEL_CALLS": "90",
            "CODEAGENT_RUN_MAX_TOOL_CALLS": "250",
            "CODEAGENT_RUN_MAX_TOTAL_TOKENS": "0",
            "CODEAGENT_RUN_MAX_ACTIVE_SECONDS": "600",
        }
        with patch.dict(os.environ, settings, clear=True), patch("codeagent.config._load_dotenv"):
            env = EnvironmentConfig.from_env()
        self.assertEqual(env.loop_guard_config, LoopGuardConfig(
            window_size=16, repeat_failure_limit=4, parameter_error_limit=3,
            blocked_attempt_limit=5, empty_response_limit=3, tool_max_retries=0,
            retry_delay_seconds=0.5, max_model_calls=90, max_tool_calls=250,
            max_total_tokens=0, max_active_seconds=600,
        ))
        self.assertIs(env.to_agent_config().loop_guard, env.loop_guard_config)

    def test_environment_defaults_match_rule_defaults(self):
        with patch.dict(os.environ, {"MODEL_ID": "fake", "CONTEXT_COMPACT_MODE": "off"}, clear=True):
            with patch("codeagent.config._load_dotenv"):
                env = EnvironmentConfig.from_env()
        self.assertEqual(env.loop_guard_config, LoopGuardConfig())

    def test_invalid_environment_budget_is_rejected(self):
        for name, value in (
            ("CODEAGENT_LOOP_WINDOW", "0"),
            ("CODEAGENT_LOOP_TOOL_MAX_RETRIES", "-1"),
            ("CODEAGENT_RUN_MAX_MODEL_CALLS", "0"),
            ("CODEAGENT_RUN_MAX_TOTAL_TOKENS", "-1"),
            ("CODEAGENT_RUN_MAX_ACTIVE_SECONDS", "0"),
        ):
            with self.subTest(name=name):
                with patch.dict(os.environ, {
                    "MODEL_ID": "fake", "CONTEXT_COMPACT_MODE": "off", name: value,
                }, clear=True), patch("codeagent.config._load_dotenv"):
                    with self.assertRaises(ValueError):
                        EnvironmentConfig.from_env()


class LoopGuardCliTests(unittest.TestCase):
    def test_cli_and_subagent_approval_waits_do_not_consume_active_time(self):
        now = [0.0]
        clock = lambda: now[0]
        budget = RunBudget(LoopGuardConfig(max_active_seconds=10), clock=clock)
        activity = ExecutionActivity(CancellationToken(), clock=clock)
        activity.execution_budget = budget
        prompts = []

        def approve(name, arguments, reason):
            prompts.append((name, arguments, reason))
            now[0] += 1000
            return True

        broker = CliPermissionBroker(prompt=approve, execution_activity=activity)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = EnvironmentConfig(model_id="fake", context_config=ContextConfig(mode="off"))
            _, hooks, _ = create_default_subagent_environment(
                root, None, None, env, root, permission_broker=broker,
            )
            tool = ToolUse("approval", "bash", {"command": "rm example.txt"})
            policy = PermissionPolicy(workspace=root, broker=broker)
            with budget.running(), contextlib.redirect_stdout(io.StringIO()):
                now[0] += 2
                self.assertTrue(policy.check(tool.name, tool.input).allowed)
                self.assertIsNone(hooks.trigger("PreToolUse", tool))
                self.assertEqual(budget.snapshot()["active_seconds"], 2)
                now[0] += 3
                budget.check()
        self.assertEqual(len(prompts), 2)
        self.assertEqual(budget.snapshot()["active_seconds"], 5)

    def test_streamed_failure_prints_programmatic_summary(self):
        result = SimpleNamespace(final_text="Stopped: existing edits preserved", stop_reason="loop_detected:repeat")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_run_result(result, stream=True)
        self.assertIn(result.final_text, output.getvalue())

    def test_one_shot_returns_nonzero_for_execution_failure(self):
        for stream in (False, True):
            for reason in ("loop_detected:repeat", "budget_exceeded:model_calls", "end_turn"):
                with self.subTest(stream=stream, reason=reason), tempfile.TemporaryDirectory() as directory:
                    env = EnvironmentConfig(
                        model_id="fake", stream=stream, data_dir=Path(directory),
                        enable_skills=False, memory_config=MemoryConfig(enabled=False),
                        context_config=ContextConfig(mode="off"),
                    )
                    result = SimpleNamespace(stop_reason=reason, final_text="final facts")
                    output = io.StringIO()
                    with patch("codeagent.cli.EnvironmentConfig.from_env", return_value=env), \
                         patch.object(EnvironmentConfig, "create_anthropic_client"), \
                         patch("codeagent.cli.Agent") as agent_type, \
                         patch("codeagent.cli.McpRouter"), \
                         contextlib.redirect_stdout(output):
                        agent_type.return_value.run.return_value = result
                        exit_code = main(["inspect"])
                    self.assertEqual(exit_code, 0 if reason == "end_turn" else 1)
                    if reason != "end_turn" or not stream:
                        self.assertIn("final facts", output.getvalue())


class LoopGuardWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repository = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.repository.close)
        self.conversation = self.repository.create_conversation(title="Guard", workspace=str(self.root))

    def factory(self, **guard_options):
        env = EnvironmentConfig(
            model_id="fake", enable_skills=False, data_dir=self.root / "data",
            memory_config=MemoryConfig(enabled=False), context_config=ContextConfig(mode="off"),
            loop_guard_config=LoopGuardConfig(**guard_options),
        )
        factory = WebAgentFactory(env, self.root, self.repository)
        self.addCleanup(factory.close)
        return factory

    def wait_finished(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = self.repository.get_run(run_id)
            if run.status in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.01)
        self.fail(f"Run {run_id} did not finish")

    def test_real_factory_scheduler_persist_guard_and_empty_response_failure(self):
        client = ScriptedClient(ModelResponse(stop_reason="end_turn", content=[]), ModelResponse(stop_reason="end_turn", content=[]))
        factory = self.factory()
        scheduler = RunScheduler(self.repository, factory)
        with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
            try:
                submitted = scheduler.submit(self.conversation.id, "inspect")
                run = self.wait_finished(submitted.id)
            finally:
                scheduler.stop()
        self.assertEqual(run.status, "failed")
        self.assertTrue(run.metadata["stop_reason"].startswith("loop_detected:"))
        self.assertEqual(len(client.calls), 2)
        checkpoint = self.repository.get_checkpoint_for_run(run.id)
        self.assertEqual(checkpoint.metadata["execution_guard"]["version"], 1)
        events = [event.type for event in self.repository.list_events(run.id)]
        self.assertIn("run.failed", events)
        self.assertNotIn("run.completed", events)
        messages = self.repository.list_messages(self.conversation.id)
        self.assertEqual(messages[-1].role, "assistant")
        self.assertEqual(messages[-1].metadata["status"], "failed")

    def test_checkpoint_restores_budget_for_same_run_but_new_run_is_fresh(self):
        client = ScriptedClient(done(), done())
        factory = self.factory(max_model_calls=1)
        scheduler = RunScheduler(self.repository, factory)
        with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
            try:
                submitted = scheduler.submit(self.conversation.id, "inspect")
                run = self.wait_finished(submitted.id)
            finally:
                scheduler.stop()
            self.assertEqual(run.status, "completed")
            checkpoint = self.repository.get_checkpoint_for_run(run.id)
            for run_id, expected_calls, failed in ((run.id, 1, True), ("fresh-run", 2, False)):
                with self.subTest(run_id=run_id):
                    restored = factory.create(
                        event_emitter=EventEmitter(context=ExecutionContext(
                            conversation_id=self.conversation.id, run_id=run_id,
                        )),
                        cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker(),
                        checkpoint=checkpoint,
                    )
                    result = restored.run(None if failed else "new request")
                    self.assertEqual(result.stop_reason.startswith("budget_exceeded:"), failed)
                    self.assertEqual(len(client.calls), expected_calls)

    def test_factory_enables_only_normal_and_discuss_roots(self):
        factory = self.factory()
        with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=ScriptedClient()):
            for mode in (PromptMode.NORMAL, PromptMode.DISCUSS, PromptMode.TEAM_PLANNER):
                with self.subTest(mode=mode):
                    agent = factory.create(
                        event_emitter=EventEmitter(context=ExecutionContext(
                            conversation_id=self.conversation.id, run_id="mode-run",
                        )),
                        cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker(),
                        root_prompt_mode=mode,
                    )
                    self.assertEqual(agent.config.loop_guard is None, mode is PromptMode.TEAM_PLANNER)

    def test_scheduler_classifies_failures_and_accepts_agents_without_export(self):
        for reason in (
            "loop_detected:blocked_attempts", "budget_exceeded:model_calls",
            "recovery_failed:request", "max_iterations", "runtime_contract:invalid",
        ):
            with self.subTest(reason=reason):
                result = SimpleNamespace(final_text="stopped", stop_reason=reason, usage=TokenTotals())
                agent = SimpleNamespace(messages=[], context=ContextManager(), run=lambda _prompt: result)
                factory = SimpleNamespace(create=lambda **_kwargs: agent)
                scheduler = RunScheduler(self.repository, factory)
                try:
                    submitted = scheduler.submit(self.conversation.id, "inspect")
                    run = self.wait_finished(submitted.id)
                finally:
                    scheduler.stop()
                self.assertEqual(run.status, "failed")
                checkpoint = self.repository.get_checkpoint_for_run(run.id)
                self.assertNotIn("execution_guard", checkpoint.metadata)


if __name__ == "__main__":
    unittest.main()
