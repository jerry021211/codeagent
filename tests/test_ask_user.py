from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, ModelResponse, create_default_registry
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, ExecutionContext, RecordingEventSink
from codeagent.messages import ToolUse
from codeagent.permissions.discuss import discuss_tool_guard
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.runtime.activity import ExecutionActivity
from codeagent.tools import AskUserTool, terminal_ask_user
from codeagent.web.questions import WebUserQuestions
from codeagent.web.storage import SQLiteRepository, StorageConflictError


class AskUserTests(unittest.TestCase):
    def test_validation_and_literal_answer(self):
        received = []
        registry = create_default_registry(ask_user_fn=lambda q, opts: received.append((q, opts)) or "Error: 请先解释")
        for args in ({"question": " "}, {"question": "q", "options": "bad"}, {"question": "q", "options": [""]}):
            self.assertEqual(registry.execute("ask_user", args).status, "error")
        self.assertEqual(received, [])
        result = registry.execute("ask_user", {"question": " q ", "options": ["A", "B"]})
        self.assertEqual(result, "Error: 请先解释")
        self.assertEqual(result.status, "success")
        self.assertEqual(received, [("q", ["A", "B"])])
        self.assertIsNone(discuss_tool_guard(ToolUse("q", "ask_user", {"question": "q"})))

    def test_terminal_reprompts_and_supports_choices_and_free_text(self):
        with patch("builtins.input", side_effect=[" ", "2"]), patch("builtins.print"):
            self.assertEqual(terminal_ask_user("格式？", ["CSV", "JSON"]), "JSON")
        with patch("builtins.input", return_value="自定义格式"), patch("builtins.print"):
            self.assertEqual(terminal_ask_user("格式？", ["CSV"]), "自定义格式")
        with patch("builtins.input", side_effect=EOFError), patch("builtins.print"):
            self.assertEqual(AskUserTool(terminal_ask_user).run("q").status, "blocked")


class WebQuestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.db"
        self.repo = SQLiteRepository(self.database, recover_incomplete=False)
        conversation = self.repo.create_conversation(workspace=self.temp.name)
        self.run = self.repo.create_run(conversation.id)
        self.repo.start_run(self.run.id)
        self.token = CancellationToken()
        self.emitter = EventEmitter(RecordingEventSink(self.repo), context=ExecutionContext(
            conversation_id=conversation.id, run_id=self.run.id,
        ))
        self.questions = WebUserQuestions(self.repo, self.emitter, self.token)

    def tearDown(self):
        self.token.cancel()
        self.repo.close()
        self.temp.cleanup()

    def wait_for_question(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            questions = self.repo.list_user_questions(self.run.id)
            if questions:
                return questions[-1]
            self.token.wait(0.01)
        self.fail("Question did not become visible")

    def test_agent_blocks_then_receives_answer_before_next_model_call(self):
        calls = []
        class Client:
            def create_message(self, **kwargs):
                calls.append(deepcopy(kwargs))
                if len(calls) == 1:
                    return ModelResponse(stop_reason="tool_use", content=[{
                        "type": "tool_use", "id": "q1", "name": "ask_user",
                        "input": {"question": "需要什么格式？", "options": ["CSV", "JSON"]},
                    }])
                return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "继续使用 JSON"}])
        clock = [0.0]
        activity = ExecutionActivity(self.token, clock=lambda: clock[0])
        agent = Agent(
            client=Client(), tools=create_default_registry(ask_user_fn=self.questions.ask),
            config=AgentConfig(model="fake"), cancellation=self.token, allow_subagents=False,
            context=ContextManager(config=ContextConfig(mode="off")),
        )
        agent.set_execution_activity(activity)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(agent.run, "导出数据")
            try:
                question = self.wait_for_question()
                self.assertFalse(future.done())
                self.assertEqual(len(calls), 1)
                clock[0] = 3600
                self.assertIsNone(activity.timeout_reason(heartbeat_timeout=120))
                self.repo.answer_user_question(self.run.id, question["id"], "JSON")
                result = future.result(timeout=3)
                self.assertEqual(result.final_text, "继续使用 JSON")
                tool_result = calls[1]["messages"][-1]["content"][0]
                self.assertEqual(tool_result["tool_use_id"], "q1")
                self.assertEqual(tool_result["content"], "JSON")
                self.assertEqual(self.repo.list_user_questions(self.run.id)[0]["status"], "answered")
            finally:
                self.token.cancel()

    def test_cancel_releases_wait_and_rejects_late_answer(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.questions.ask, "继续吗？", [])
            try:
                question = self.wait_for_question()
                self.repo.request_run_cancel(self.run.id)
                self.token.cancel()
                with self.assertRaises(CancelledError):
                    future.result(timeout=3)
                with self.assertRaises(StorageConflictError):
                    self.repo.answer_user_question(self.run.id, question["id"], "继续")
                self.assertEqual(self.repo.get_user_question(self.run.id, question["id"])["status"], "cancelled")
            finally:
                self.token.cancel()

    def test_refresh_restart_and_duplicate_answer(self):
        question = self.repo.create_user_question(self.run.id, "格式？", ["CSV"])
        with SQLiteRepository(self.database, recover_incomplete=False) as other:
            self.assertEqual(other.list_user_questions(self.run.id)[0], question)
        answered = self.repo.answer_user_question(self.run.id, question["id"], "CSV")
        self.assertEqual(self.repo.answer_user_question(self.run.id, question["id"], "CSV"), answered)
        with self.assertRaises(StorageConflictError):
            self.repo.answer_user_question(self.run.id, question["id"], "JSON")
        pending = self.repo.create_user_question(self.run.id, "文件名？", [])
        self.repo.mark_incomplete_runs_interrupted()
        self.assertEqual(self.repo.get_user_question(self.run.id, pending["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_user_question(self.run.id, question["id"])["answer"], "CSV")

    def test_concurrent_answers_accept_exactly_one(self):
        question = self.repo.create_user_question(self.run.id, "格式？", [])
        ready = threading.Barrier(2)
        def answer(value):
            ready.wait(timeout=3)
            try:
                self.repo.answer_user_question(self.run.id, question["id"], value)
                return "accepted"
            except StorageConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(list(executor.map(answer, ["CSV", "JSON"])), ["accepted", "conflict"])
