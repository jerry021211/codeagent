from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from codeagent import Agent, AgentConfig, ModelResponse, RecoveryConfig, RecoveryRuntime, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.messages import validate_tool_history
from codeagent.runtime import CancellationToken
from codeagent.runtime.cancellation import CancelledError
from codeagent.web.storage import SQLiteRepository


def truncated(identifier="partial", *, content="incomplete file content", extra=None):
    return ModelResponse("max_tokens", [
        {"type": "text", "text": f"received text evidence: {identifier}"},
        {"type": "tool_use", "id": identifier, "name": "write_file", "input": {"file_path": "result.txt", "content": content}},
        *(extra or []),
    ])


def complete():
    return ModelResponse("end_turn", [{"type": "text", "text": "recovered safely"}])


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fork(self, **kwargs):
        return self

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        value = self.responses.pop(0)
        if callable(value):
            value = value()
        if isinstance(value, BaseException):
            raise value
        return value


class ContextRecoveryProtocolTests(unittest.TestCase):
    def setUp(self):
        self.executions = []

    def agent(self, responses, *, config=None, cancellation=None, messages=None, state=None):
        client = Client(responses)
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("write_file", "write", {"type": "object"}), lambda **kwargs: self.executions.append(kwargs) or "written")
        return Agent(
            client=client, tools=tools, config=AgentConfig(model="main", max_tokens=1000, max_iterations=20),
            context=ContextManager(config=ContextConfig(mode="off"), state=state),
            messages=messages or [], cancellation=cancellation,
            recovery_runtime=RecoveryRuntime(config or RecoveryConfig(sleep_enabled=False, max_retries=0)),
            allow_subagents=False,
        )

    def assert_valid_unexecuted(self, agent, ids):
        self.assertEqual(self.executions, [])
        validate_tool_history(agent.messages)
        results = {block["tool_use_id"]: block for message in agent.messages if isinstance(message["content"], list)
                   for block in message["content"] if block.get("type") == "tool_result"}
        self.assertEqual(set(results), set(ids))
        for result in results.values():
            self.assertTrue(result["is_error"])
            self.assertIn("未执行", result["content"])
            self.assertIn("max_tokens", result["content"])
            self.assertIn("未调用此工具", result["content"])
        for call in agent.client.calls:
            validate_tool_history(call["messages"])

    def test_truncated_tool_response_escalates_with_received_text_and_no_execution(self):
        partial = truncated()
        original = deepcopy(partial.content)
        agent = self.agent([partial, complete()])
        result = agent.run("write the file")
        self.assertEqual(result.final_text, "recovered safely")
        self.assertEqual(agent.client.calls[1]["max_tokens"], 64000)
        self.assertIn("received text evidence: partial", str(agent.client.calls[1]["messages"]))
        self.assertEqual(partial.content, original)
        self.assert_valid_unexecuted(agent, ["partial"])

    def test_repeated_truncation_keeps_every_pair_and_parallel_call_without_execution(self):
        extra = [{"type": "tool_use", "id": "parallel", "name": "write_file", "input": {"file_path": "second.txt", "content": "partial"}}]
        agent = self.agent([truncated("first"), truncated("second", extra=extra), complete()])
        result = agent.run("write files")
        self.assertEqual(result.final_text, "recovered safely")
        self.assertEqual(len(agent.client.calls), 3)
        self.assert_valid_unexecuted(agent, ["first", "second", "parallel"])

    def test_exhausted_recovery_fails_without_dispatching_partial_tool(self):
        cfg = RecoveryConfig(escalated_max_tokens=1000, max_continuations=0, sleep_enabled=False)
        agent = self.agent([truncated()], config=cfg)
        result = agent.run("write file")
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assertIn("均未执行", result.final_text)
        self.assertEqual(len(agent.client.calls), 1)
        self.assert_valid_unexecuted(agent, ["partial"])

    def test_exhaustion_after_multiple_partial_responses_remains_checkpointable(self):
        cfg = RecoveryConfig(escalated_max_tokens=1000, max_continuations=2, sleep_enabled=False)
        agent = self.agent([truncated("one"), truncated("two"), truncated("three")], config=cfg)
        result = agent.run("write file")
        self.assertTrue(result.stop_reason.startswith("recovery_failed"))
        self.assert_valid_unexecuted(agent, ["one", "two", "three"])
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "state.db")
            try:
                conversation = repo.create_conversation(workspace=temp)
                run = repo.create_run(conversation.id)
                repo.finish_run_with_checkpoint(
                    run.id, status="failed", messages=agent.messages, todos=[], context=asdict(agent.context.state),
                )
                saved = repo.get_latest_checkpoint(conversation.id)
                self.assertEqual(saved.messages, agent.messages)
                restored = self.agent([complete()], messages=saved.messages, state=RuntimeState(**saved.context))
                resumed = restored.run("continue with complete instructions")
                self.assertEqual(resumed.final_text, "recovered safely")
                validate_tool_history(restored.messages)
                self.assertEqual(self.executions, [])
            finally:
                repo.close()

    def test_malformed_partial_ids_or_arguments_are_saved_as_inert_data(self):
        cases = [
            [{"type": "tool_use", "id": "", "name": "write_file", "input": {"content": "partial"}}],
            [{"type": "tool_use", "id": "bad_json", "name": "write_file", "input": '{"content":"unfinished'}],
            [{"type": "tool_use", "id": "duplicate", "name": "write_file", "input": {}}] * 2,
        ]
        for blocks in cases:
            with self.subTest(blocks=blocks):
                response = ModelResponse("max_tokens", [{"type": "text", "text": "preserved visible text"}, *blocks])
                agent = self.agent([response, complete()])
                result = agent.run("task")
                self.assertEqual(result.final_text, "recovered safely")
                self.assertIn("preserved visible text", str(agent.messages))
                self.assertIn("仅保存为历史数据", str(agent.messages))
                self.assert_valid_unexecuted(agent, [])

    def test_cancellation_after_partial_checkpoint_preserves_pairs_and_can_resume(self):
        token = CancellationToken()
        def cancel():
            token.cancel("cancelled during next model request")
            return CancelledError("cancelled during next model request")
        agent = self.agent([truncated(), cancel], cancellation=token)
        with self.assertRaises(CancelledError):
            agent.run("task")
        self.assert_valid_unexecuted(agent, ["partial"])
        restored = self.agent([complete()], messages=deepcopy(agent.messages), state=RuntimeState(**asdict(agent.context.state)))
        result = restored.run()
        self.assertEqual(result.final_text, "recovered safely")
        self.assert_valid_unexecuted(restored, ["partial"])

    def test_complete_resubmitted_tool_is_the_only_request_executed(self):
        full = ModelResponse("tool_use", [{
            "type": "tool_use", "id": "complete", "name": "write_file",
            "input": {"file_path": "result.txt", "content": "FULL CONTENT"},
        }])
        agent = self.agent([truncated(), full, complete()])
        result = agent.run("write file")
        self.assertEqual(result.final_text, "recovered safely")
        self.assertEqual(self.executions, [{"file_path": "result.txt", "content": "FULL CONTENT"}])
        validate_tool_history(agent.messages)
        for call in agent.client.calls:
            validate_tool_history(call["messages"])


if __name__ == "__main__":
    unittest.main()
