"""Behavioral contracts through the production Agent/context request boundary."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, ModelResponse, RecoveryConfig, RecoveryRuntime, ToolDefinition, ToolRegistry
from codeagent.context import ContextCompactionError, ContextConfig, ContextManager, RuntimeState
from codeagent.context.budget import RequestBudgetError
from codeagent.context.projection import TOOL_VIEW_MARKER
from codeagent.events import TokenUsage
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.prompts import PromptAssemblyResult


def _text(value="done", *, usage=None):
    return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": value}], usage=usage)


def _tool(index, *, name="echo", usage=None):
    return ModelResponse(stop_reason="tool_use", content=[{
        "type": "tool_use", "id": f"call_{index}", "name": name,
        "input": {"value": str(index)},
    }], usage=usage)


def _rounds(count, *, parallel=False):
    messages = [{"role": "user", "content": "Keep exact task /project/current.py and do not repeat completed work."}]
    for index in range(count):
        ids = [f"round_{index}_a", f"round_{index}_b"] if parallel else [f"round_{index}_a"]
        messages.extend([
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": call_id, "name": "echo", "input": {"value": f"step {index}"},
            } for call_id in ids]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id, "content": f"evidence for {call_id}",
            } for call_id in ids]},
        ])
    return messages


def _dialogue(count):
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": f"dialogue-{index}"} for index in range(count)]


class ScriptedContextClient:
    def __init__(self, responses=(), summaries=("Summary: exact task remains active; older completed steps verified.",)):
        self.responses = list(responses)
        self.summaries = list(summaries)
        self.calls = []

    @property
    def main_calls(self):
        return [call for call in self.calls if call["model"] != "summary"]

    @property
    def summary_calls(self):
        return [call for call in self.calls if call["model"] == "summary"]

    def fork(self, **kwargs):
        return self

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if kwargs["model"] == "summary":
            value = self.summaries.pop(0)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, ModelResponse):
                return value
            return _text(value, usage=TokenUsage(input_tokens=9000, output_tokens=100, call_kind="context_summary"))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manager(self, **settings):
        config = ContextConfig(**{
            "compact_threshold_chars": 1000, "summarization_model": "summary", "transcript_dir": self.root / "transcripts",
            "tool_output_dir": self.root / "outputs", **settings,
        })
        return ContextManager(config=config)

    def agent(self, client, *, manager=None, messages=None, tools=None):
        registry = tools or ToolRegistry()
        if "echo" not in registry:
            registry.register_handler(ToolDefinition("echo", "Echo", {"type": "object"}), lambda value: value)
        return Agent(
            client=client, config=AgentConfig(model="main", max_tokens=1000, max_iterations=30),
            tools=registry, context=manager or self.manager(), messages=messages or [],
            allow_subagents=False,
            recovery_runtime=RecoveryRuntime(RecoveryConfig(sleep_enabled=False, max_retries=0)),
        )

    def test_production_pressure_trigger_keeps_canonical_and_sends_summary_recent_rounds(self):
        history = _rounds(10, parallel=True)
        original = deepcopy(history)
        manager = self.manager()
        manager.begin_turn(0)
        client = ScriptedContextClient([_text()])
        agent = self.agent(client, manager=manager, messages=history)

        result = agent.run()

        self.assertEqual(result.final_text, "done")
        self.assertEqual(agent.messages[:len(original)], original)
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(manager.state.compacted_message_count, 17)
        projected = client.main_calls[0]["messages"]
        self.assertIn("<context_summary", projected[0]["content"])
        self.assertIn(original[0], projected)
        self.assertEqual(projected[-4:], original[-4:])
        self.assertNotIn("evidence for round_0_a", str(projected))
        for call in client.main_calls:
            validate_tool_history(call["messages"])
        validate_tool_history(agent.messages)

    def test_current_user_steering_survives_folded_execution_rounds(self):
        history = _rounds(10)
        steering = {"role": "user", "content": "Correction: keep exact filename 新模块.py and preserve the public API."}
        history.insert(7, steering)
        manager = self.manager()
        manager.begin_turn(0)
        original = deepcopy(history)
        client = ScriptedContextClient([_text()])
        agent = self.agent(client, manager=manager, messages=history)
        agent.run()
        self.assertIn(steering, client.main_calls[0]["messages"])
        self.assertEqual(agent.messages[:len(original)], original)

    def test_two_user_turns_preserve_unsummarized_tool_evidence(self):
        client = ScriptedContextClient([_tool(1), _text("first answer"), _text("second answer")])
        agent = self.agent(client)
        agent.run("first request")
        first_turn = deepcopy(agent.messages)
        agent.run("second request")
        request = client.main_calls[-1]["messages"]
        self.assertIn("first request", str(request))
        self.assertIn("first answer", str(request))
        self.assertIn("second request", str(request))
        self.assertIn("tool_use", str(request))
        self.assertIn("tool_result", str(request))
        self.assertEqual(agent.messages[:len(first_turn)], first_turn)
        validate_tool_history(agent.messages)

    def test_real_tool_loop_clears_only_sent_old_outputs(self):
        tools = ToolRegistry()
        outputs = {str(index): f"result-{index}:" + str(index) * 3000 for index in range(3)}
        tools.register_handler(
            ToolDefinition("grep", "Search", {"type": "object"}), lambda value: outputs[value],
        )
        client = ScriptedContextClient([_tool(index, name="grep") for index in range(3)] + [_text()])
        manager = self.manager(mode="off", compact_threshold_chars=1, tool_clear_min_chars=2000)
        agent = self.agent(client, manager=manager, tools=tools)
        agent.run("perform the three searches")
        sent = client.main_calls[-1]["messages"]
        self.assertIn(TOOL_VIEW_MARKER, str(sent))
        self.assertNotIn(outputs["0"], str(sent))
        self.assertIn(outputs["1"], str(sent))
        self.assertIn(outputs["2"], str(sent))
        for output in outputs.values():
            self.assertIn(output, str(agent.messages))
        for call in client.main_calls:
            validate_tool_history(call["messages"])
        validate_tool_history(agent.messages)

    def test_next_turn_omits_empty_old_assistant_responses_without_mutating_history(self):
        for content in ("", "  ", [], [{"type": "text", "text": ""}]):
            with self.subTest(content=content):
                history = [
                    {"role": "user", "content": "previous task"},
                    {"role": "assistant", "content": content},
                ]
                original = deepcopy(history)
                client = ScriptedContextClient([_text()])
                agent = self.agent(client, messages=history)
                agent.run("continue the task")
                self.assertEqual(agent.messages[:2], original)
                request = client.main_calls[0]["messages"]
                self.assertFalse(any(message["role"] == "assistant" for message in request))
                self.assertIn(original[0], request)
                self.assertIn({"role": "user", "content": "continue the task"}, request)

    def test_28_dialogue_messages_fold_16_and_retain_12(self):
        history = [{**m, "content": m["content"] * 50} for m in _dialogue(28)]
        original = deepcopy(history)
        manager = self.manager()
        manager.begin_turn(28)
        client = ScriptedContextClient()
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertEqual(manager.state.compacted_message_count, 16)
        self.assertEqual(projected[1:], history[16:])
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(history, original)

    def test_small_history_no_summary_call(self):
        manager = self.manager()
        history = _rounds(3)
        manager.begin_turn(0)
        client = ScriptedContextClient()
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertEqual(projected, history)
        self.assertEqual(client.summary_calls, [])
        self.assertEqual(manager.state.summary_revision, 0)

    def test_checkpoint_state_reconstruction_reproduces_request_view(self):
        history = _rounds(10, parallel=True)
        manager = self.manager()
        manager.begin_turn(0)
        manager.prepare_before_model_call(history, client=ScriptedContextClient())
        expected = manager.project_messages(history)
        restored = ContextManager(config=manager.config, state=RuntimeState(**asdict(manager.state)))
        restored_history = deepcopy(history)
        self.assertEqual(restored.project_messages(restored_history), expected)
        self.assertEqual(restored.project_messages(restored_history), expected)
        self.assertEqual(restored_history, history)
        resumed_client = ScriptedContextClient([_text()])
        self.agent(resumed_client, manager=restored, messages=restored_history).run()
        self.assertEqual(resumed_client.main_calls[0]["messages"], expected)
        self.assertEqual(resumed_client.summary_calls, [])

    def test_second_summary_only_receives_new_interval_and_previous_summary(self):
        history = _rounds(10)
        manager = self.manager()
        manager.begin_turn(0)
        client = ScriptedContextClient(summaries=("FIRST_SUMMARY", "SECOND_SUMMARY"))
        manager.force_compact(history, client=client)
        first_cursor = manager.state.compacted_message_count
        more = _rounds(6)[1:]
        # Unique IDs ensure this is a genuine continuation, not a replay.
        more = [{**message, "content": [{**block, **(
            {"id": "later_" + block["id"]} if block["type"] == "tool_use"
            else {"tool_use_id": "later_" + block["tool_use_id"], "content": "later evidence " + block["tool_use_id"]}
        )} for block in message["content"]]} for message in more]
        history.extend(more)
        manager.force_compact(history, client=client)
        second_input = client.summary_calls[-1]["messages"][0]["content"]
        self.assertIn("FIRST_SUMMARY", second_input)
        self.assertNotIn("evidence for round_0_a", second_input)
        self.assertGreater(manager.state.compacted_message_count, first_cursor)
        self.assertEqual(manager.state.summary_revision, 2)

    def test_summary_failure_does_not_advance_existing_checkpoint(self):
        history = _rounds(10)
        manager = self.manager()
        manager.begin_turn(0)
        manager.force_compact(history, client=ScriptedContextClient(summaries=("stable summary",)))
        old_state = asdict(manager.state)
        # Removing the latest two rounds from the retained tail is not necessary:
        # a later user turn supplies enough new dialogue for another legal batch.
        history.extend(_dialogue(30))
        manager.begin_turn(len(history))
        expected = asdict(manager.state)
        client = ScriptedContextClient(summaries=(RuntimeError("summary unavailable"),))
        with self.assertRaises(ContextCompactionError):
            manager.force_compact(history, client=client)
        actual = asdict(manager.state)
        for key in ("summary_retry_after_epoch", "summary_failure_scope"):
            actual.pop(key)
            expected.pop(key)
        self.assertEqual(actual, expected)
        self.assertGreater(manager.state.summary_retry_after_epoch, 0)
        self.assertEqual(manager.state.summary_revision, old_state["summary_revision"])
        self.assertEqual(manager.last_compaction["status"], "failed")

    def test_ordinary_summary_failure_can_send_full_safe_request(self):
        history = _rounds(10)
        manager = self.manager()
        manager.begin_turn(0)
        client = ScriptedContextClient([_text()], summaries=(RuntimeError("summary unavailable"),))
        agent = self.agent(client, manager=manager, messages=history)
        result = agent.run()
        self.assertEqual(result.final_text, "done")
        self.assertEqual(manager.state.summary_revision, 0)
        self.assertEqual(client.main_calls[0]["messages"], history[:-1])

    def test_oversized_system_never_reaches_main_provider(self):
        client = ScriptedContextClient([_text()])
        agent = self.agent(client, manager=self.manager(max_request_chars=10000))
        assembly = PromptAssemblyResult(system_prompt="system " * 3000, trace=[], prompt_hash="test")
        with patch.object(Agent, "_assemble_prompt", return_value=assembly):
            result = agent.run("tiny user request")
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual(client.main_calls, [])
        self.assertEqual(client.summary_calls, [])

    def test_oversized_tool_schema_never_reaches_main_provider(self):
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("large", "description " * 3000, {"type": "object"}), lambda: "ok")
        client = ScriptedContextClient([_text()])
        agent = self.agent(client, manager=self.manager(max_request_chars=20000), tools=tools)
        result = agent.run("tiny user request")
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual(client.main_calls, [])
        self.assertEqual(client.summary_calls, [])

    def test_provider_metrics_separate_latest_peak_and_accumulated_from_summary(self):
        history = [{**m, "content": m["content"] * 50} for m in _dialogue(28)]
        first = TokenUsage(input_tokens=200, output_tokens=3, cache_creation_input_tokens=10, cache_read_input_tokens=50)
        second = TokenUsage(input_tokens=50, output_tokens=4, cache_read_input_tokens=20)
        client = ScriptedContextClient([_tool(1, usage=first), _text(usage=second)])
        manager = self.manager()
        agent = self.agent(client, manager=manager, messages=history)
        agent.run("continue actual task")
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(manager.state.latest_request_prompt_tokens, 70)
        self.assertEqual(manager.state.peak_request_prompt_tokens, 260)
        self.assertEqual(manager.state.accumulated_input_tokens, 330)
        self.assertEqual(manager.state.accumulated_output_tokens, 7)
        self.assertEqual(manager.state.cache_hit_tokens, 70)
        self.assertEqual(manager.state.cache_miss_tokens, 260)
        self.assertFalse(manager.state.latest_request_estimated)

    def test_missing_usage_does_not_replace_last_valid_measurement_with_zero(self):
        usage = TokenUsage(input_tokens=120, output_tokens=3)
        client = ScriptedContextClient([_tool(1, usage=usage), _text(usage=TokenUsage(available=False))])
        agent = self.agent(client)
        agent.run("task")
        self.assertEqual(agent.context.state.latest_request_prompt_tokens, 120)
        self.assertEqual(agent.context.state.peak_request_prompt_tokens, 120)
        self.assertEqual(agent.context.state.accumulated_input_tokens, 120)

    def test_compaction_off_provider_overflow_does_not_call_summary_model(self):
        history = _rounds(10)
        manager = self.manager(mode="off")
        manager.begin_turn(0)
        client = ScriptedContextClient([RuntimeError("prompt too long")])
        result = self.agent(client, manager=manager, messages=history).run()
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertEqual(len(client.main_calls), 1)
        self.assertEqual(client.summary_calls, [])
        self.assertEqual(manager.state.summary_revision, 0)

    def test_reactive_retry_reprojects_canonical_and_keeps_latest_rounds(self):
        history = _rounds(10, parallel=True)
        original = deepcopy(history)
        manager = self.manager(compact_threshold_chars=300000)
        manager.begin_turn(0)
        client = ScriptedContextClient([RuntimeError("prompt too long"), _text("recovered")])
        agent = self.agent(client, manager=manager, messages=history)
        result = agent.run()
        self.assertEqual(result.final_text, "recovered")
        self.assertEqual(len(client.main_calls), 2)
        self.assertEqual(len(client.summary_calls), 1)
        self.assertEqual(client.main_calls[0]["messages"], original)
        self.assertEqual(client.main_calls[1]["messages"][-4:], original[-4:])
        self.assertEqual(agent.messages[:len(original)], original)
        self.assertEqual(manager.state.summary_revision, 1)
        for call in client.main_calls:
            validate_tool_history(call["messages"])

    def test_edit_inside_covered_prefix_invalidates_stale_summary(self):
        history = _rounds(10)
        manager = self.manager()
        manager.begin_turn(0)
        manager.force_compact(history, client=ScriptedContextClient(summaries=("old task summary",)))
        history[0]["content"] = "changed task that was never included in old task summary"
        projected = manager.project_messages(history)
        self.assertEqual(manager.state.summary_text, "")
        self.assertEqual(manager.state.compacted_message_count, 0)
        self.assertEqual(projected, history)
        self.assertEqual(manager.last_compaction["reason"], "history_changed")

    def test_summary_input_budget_does_not_send_oversized_summary_or_main_request(self):
        manager = self.manager(summary_input_max_chars=1000, max_request_chars=2000)
        history = _rounds(10)
        for message in history:
            if message["role"] == "user" and isinstance(message["content"], list):
                message["content"][0]["content"] += "x" * 3000
        manager.begin_turn(0)
        client = ScriptedContextClient()
        with self.assertRaises(RequestBudgetError):
            manager.prepare_before_model_call(history, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(manager.state.summary_revision, 0)

    def test_nonfinal_summary_text_never_advances_checkpoint(self):
        for reason in ("max_tokens", "pause_turn", "refusal", "tool_use"):
            with self.subTest(stop_reason=reason):
                history = _rounds(10)
                original = deepcopy(history)
                manager = self.manager()
                manager.begin_turn(0)
                before = asdict(manager.state)
                incomplete = ModelResponse(
                    stop_reason=reason, content=[{"type": "text", "text": "nonempty but unfinished checkpoint"}],
                )
                client = ScriptedContextClient(summaries=(incomplete,))
                with self.assertRaises(ContextCompactionError):
                    manager.force_compact(history, client=client)
                after = asdict(manager.state)
                for key in ("summary_retry_after_epoch", "summary_failure_scope"):
                    after.pop(key)
                    before.pop(key)
                self.assertEqual(after, before)
                self.assertGreater(manager.state.summary_retry_after_epoch, 0)
                self.assertEqual(history, original)
                self.assertEqual(len(client.summary_calls), 1)
                self.assertEqual(manager.last_compaction["status"], "failed")

    def test_task_snapshot_failure_cools_down_without_blocking_safe_main_request(self):
        manager = self.manager()
        manager.begin_turn(0)
        snapshot_calls = []

        def failing_snapshot():
            snapshot_calls.append(True)
            raise RuntimeError("task snapshot unavailable")

        manager.task_state_provider = failing_snapshot
        history = _rounds(10)
        client = ScriptedContextClient([_text("main request continued")])
        agent = self.agent(client, manager=manager, messages=history)
        result = agent.run()
        self.assertEqual(result.final_text, "main request continued")
        self.assertEqual(manager.last_compaction, {"status": "failed", "reason": "RuntimeError"})
        self.assertEqual(manager.state.summary_revision, 0)
        self.assertEqual(len(client.main_calls), 1)
        self.assertEqual(client.summary_calls, [])
        self.assertEqual(snapshot_calls, [True])
        manager.prepare_before_model_call(agent.messages, client=client)
        self.assertEqual(manager.last_compaction["reason"], "failure_cooldown")
        self.assertEqual(snapshot_calls, [True])
        self.assertEqual(client.summary_calls, [])

    def test_edit_or_truncate_unfolded_snapshot_tail_invalidates_restored_summary(self):
        for mutation in ("edit", "truncate"):
            with self.subTest(mutation=mutation):
                history = _rounds(10)
                manager = self.manager()
                manager.begin_turn(0)
                manager.state.important_notes = ["Tail evidence informed this runtime snapshot."]
                manager.force_compact(history, client=ScriptedContextClient())
                self.assertEqual(manager.state.summary_source_count, len(history))
                restored = ContextManager(config=manager.config, state=RuntimeState(**asdict(manager.state)))
                old_prefix = deepcopy(history[:restored.state.compacted_message_count])
                if mutation == "edit":
                    history[-1]["content"][0]["content"] = "corrected tail evidence: earlier result was false"
                else:
                    del history[-2:]
                self.assertEqual(history[:restored.state.compacted_message_count], old_prefix)
                validate_tool_history(history)
                projected = restored.project_messages(history)
                self.assertEqual(restored.state.summary_text, "")
                self.assertEqual(restored.state.compacted_message_count, 0)
                self.assertEqual(restored.state.summary_source_count, 0)
                self.assertEqual(restored.state.summary_source_hash, "")
                self.assertEqual(restored.last_compaction["reason"], "history_changed")
                self.assertEqual(projected, history)

    def test_mixed_tool_result_and_user_text_is_never_a_conversation_cut(self):
        history = [{**m, "content": m["content"] * 100} for m in _dialogue(4)] + _rounds(10)
        for message in history:
            if message["role"] == "user" and isinstance(message["content"], list):
                message["content"].append({"type": "text", "text": "user correction bundled with results"})
        current_start = len(history)
        history.append({"role": "user", "content": "current user request"})
        original = deepcopy(history)
        manager = self.manager()
        manager.begin_turn(current_start)
        client = ScriptedContextClient()
        projected = manager.force_compact(history, client=client)
        self.assertEqual(manager.state.compacted_message_count, 4)
        validate_tool_history(history[:manager.state.compacted_message_count])
        validate_tool_history(history[manager.state.compacted_message_count:])
        validate_tool_history(projected)
        self.assertEqual(history, original)

    def test_mixed_tool_result_text_stays_paired_in_recent_execution_rounds(self):
        history = _rounds(10, parallel=True)
        for message in history:
            if message["role"] == "user" and isinstance(message["content"], list):
                message["content"].append({"type": "text", "text": "keep this steering too"})
        manager = self.manager()
        manager.begin_turn(0)
        projected = manager.force_compact(history, client=ScriptedContextClient())
        self.assertEqual(projected[-4:], history[-4:])
        validate_tool_history(projected)
        self.assertEqual(manager.state.compacted_message_count, 17)

    def test_legacy_failed_receipts_without_is_error_keep_failure_status(self):
        for receipt in ("Error: command failed", "Blocked: permission denied", "command stopped\n[exit code: 2]"):
            with self.subTest(receipt=receipt):
                history = [{"role": "user", "content": "old task"}, {
                    "role": "assistant", "content": [{
                        "type": "tool_use", "id": "legacy_call", "name": "bash", "input": {"command": "legacy command"},
                    }],
                }, {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "legacy_call", "content": receipt,
                }]}, {"role": "assistant", "content": "old answer"}, {"role": "user", "content": "new request"}]
                original = deepcopy(history)
                manager = self.manager()
                manager.begin_turn(4)
                projected = manager.project_messages(history)
                self.assertEqual(projected, history)
                self.assertNotIn("工具返回状态=已返回", str(projected))
                self.assertIn(receipt.splitlines()[0], str(projected))
                self.assertEqual(history, original)

    def test_projection_generations_do_not_repeat_stable_hook_reminder(self):
        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition("grep", "Search", {"type": "object"}), lambda value: f"search-{value}:" + value * 3000,
        )
        client = ScriptedContextClient([_tool(index, name="grep") for index in range(5)] + [_text()])
        manager = self.manager(mode="off", compact_threshold_chars=1, tool_clear_min_chars=2000)
        agent = self.agent(client, manager=manager, tools=tools)
        reminder = "Stable plan reminder: finish the current task."
        agent.hooks.register("BeforeModelCall", lambda messages: reminder)
        result = agent.run("run the searches")
        self.assertEqual(result.final_text, "done")
        self.assertGreater(manager.state.history_generation, 0)
        self.assertEqual(manager.state.summary_revision, 0)
        self.assertEqual(sum(reminder in str(message["content"]) for message in agent.messages), 1)
        self.assertTrue(any(TOOL_VIEW_MARKER in str(call["messages"]) for call in client.main_calls))

    def test_reused_tool_id_archives_distinct_outputs_without_overwriting(self):
        manager = self.manager(single_tool_output_max_chars=500, tool_result_budget_chars=2000)
        tool = ToolUse(id="reused_call", name="grep", input={"pattern": "example"})
        first = "original output " * 200
        second = "changed output " * 200
        first_preview = manager.finalize_tool_results([tool], [first])[0]
        first_path = Path(manager.state.tool_artifacts[-1])
        second_preview = manager.finalize_tool_results([tool], [second])[0]
        second_path = Path(manager.state.tool_artifacts[-1])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.read_text(encoding="utf-8"), first)
        self.assertEqual(second_path.read_text(encoding="utf-8"), second)
        self.assertIn(str(first_path), first_preview)
        self.assertIn(str(second_path), second_preview)
        manager.finalize_tool_results([tool], [second])
        self.assertEqual(len(manager.state.tool_artifacts), 2)
        self.assertEqual(first_path.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
