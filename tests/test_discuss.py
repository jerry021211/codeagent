from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from codeagent import Agent, AgentConfig, EnvironmentConfig, HookManager, ModelResponse, PromptMode, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import MemoryConfig
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.web.factory import WebAgentFactory, serialize_runtime_state, _restore_runtime_state
from codeagent.web.storage import SQLiteRepository
from codeagent.messages import ToolUse
from codeagent.permissions.discuss import is_safe_discuss_command
from codeagent.tools import ReadFileTool, WriteFileTool


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return next(self.responses)


def end_turn():
    return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done"}])


class DiscussPolicyTests(unittest.TestCase):
    def test_read_only_commands(self):
        for command in (
            "cat README.md", "  ls -la", "rg -n pattern codeagent", "rg --files",
            "Get-Content -Raw README.md", 'Get-Content -LiteralPath "C:\\My Project\\README.md"',
            "Get-ChildItem -Name", "Select-String -Path README.md -Pattern code",
            "git status --short", "git branch", "git branch --show-current",
            "git diff --no-ext-diff --no-textconv HEAD", "git ls-files", "python --version",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_safe_discuss_command(command))

    def test_writes_and_shell_bypasses(self):
        for command in (
            "", "  ", None, 42, "rm file", "mkdir dir", "npm install",
            "git branch new", "git branch -D main", "git remote add origin url",
            "git config x y", "git push", "git status; python evil.py",
            "git status\npython evil.py", "git diff --output=out.txt",
            "git diff", "git show HEAD", "git diff # --no-ext-diff --no-textconv",
            "git diff -- --no-ext-diff --no-textconv",
            "git log --no-ext-diff --no-textconv --output=out.txt",
            "git status > out.txt", "cat file | python evil.py", "cat $(python evil.py)",
            "cat `python evil.py`", "Get-Content (Remove-Item file)",
            "Get-Content x -OutVariable result", "Get-Content @args", "cat %EVIL%",
            "rg --pre=python pattern .", r"rg \--pre=python pattern .",
            "rg --pre python pattern .", "rg --hostname-bin evil pattern .",
            "find . -exec python evil.py", 'awk "BEGIN {system(cmd)}"',
            "sed -n 'w output' file", "sort -o output input", "curl -o out https://example.com",
            "curl -X POST https://example.com", "python -c print(1)", "env python evil.py",
            'Get-Content "unclosed', "Get-Content ./file`nRemove-Item x",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_safe_discuss_command(command))


class DiscussAgentTests(unittest.TestCase):
    def make_agent(self, *, hooks=None, client=None, tools=None, mode=PromptMode.DISCUSS):
        return Agent(
            client=client or ScriptedClient(end_turn()), tools=tools or ToolRegistry(),
            config=AgentConfig(model="fake"), hooks=hooks or HookManager(), prompt_mode=mode,
        )

    def test_real_write_is_blocked_and_switching_restores_it(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "example.txt"
            target.write_text("before", encoding="utf-8")
            tools = ToolRegistry()
            tools.register(WriteFileTool())
            tools.register(ReadFileTool())
            agent = self.make_agent(tools=tools)
            write = ToolUse("w", "write_file", {"file_path": str(target), "content": "after"})
            result = agent._execute_tools([write])
            self.assertIn("Discuss mode", result[0]["content"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            read = agent._execute_tools([ToolUse("r", "read_file", {"file_path": str(target)})])
            self.assertIn("before", read[0]["content"])
            agent.set_discuss_mode(False)
            agent._execute_tools([write])
            self.assertEqual(target.read_text(encoding="utf-8"), "after")

    def test_guard_runs_before_approval_and_blocks_opaque_tools(self):
        approval = Mock(return_value=None)
        hooks = HookManager()
        hooks.register("PreToolUse", approval)
        handler = Mock(return_value="executed")
        tools = ToolRegistry()
        names = ["mcp__test__read", "subagent", "remember", "TaskCreate", "TaskUpdate", "todo_write", "custom_write", "edit_file"]
        for name in names:
            if name == "todo_write":
                continue
            tools.register_handler(ToolDefinition(name, name, {"type": "object"}), handler)
        agent = self.make_agent(hooks=hooks, tools=tools)
        for name in names:
            result = agent._execute_tools([ToolUse(name, name, {})])
            self.assertIn("Discuss mode", result[0]["content"])
        handler.assert_not_called()
        approval.assert_not_called()

    def test_prompt_suppresses_execution_planning_and_memory_writes(self):
        tools = ToolRegistry()
        for name in ["todo_write", "remember"]:
            tools.register_handler(ToolDefinition(name, name, {"type": "object"}), lambda: "unused")
        hooks = HookManager()
        hooks.register("BeforeModelCall", lambda messages: "CREATE A TODO NOW")
        client = ScriptedClient(end_turn(), end_turn())
        agent = self.make_agent(tools=tools, client=client, hooks=hooks)
        agent.run("discuss the architecture")
        call = client.calls[0]
        self.assertIn("[DISCUSS MODE · 只读讨论]", call["system"])
        self.assertNotIn("CREATE A TODO NOW", repr(call["messages"]))
        fragments = {item.id for item in agent._assemble_prompt(tools.schemas()).trace}
        self.assertFalse(fragments & {"base.execution", "tools.todo", "tools.subagent", "memory.write"})
        agent.set_discuss_mode(False)
        agent.run("implement")
        self.assertNotIn("[DISCUSS MODE · 只读讨论]", client.calls[1]["system"])
        self.assertIn("CREATE A TODO NOW", repr(client.calls[1]["messages"]))

    def test_model_requested_write_is_intercepted_in_actual_loop(self):
        handler = Mock(return_value="written")
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("write_file", "write", {"type": "object"}), handler)
        client = ScriptedClient(ModelResponse(stop_reason="tool_use", content=[
            {"type": "tool_use", "id": "w", "name": "write_file", "input": {}}
        ]), end_turn())
        self.make_agent(tools=tools, client=client).run("please write anyway")
        handler.assert_not_called()
        self.assertIn("Discuss mode", repr(client.calls[1]["messages"]))
        self.assertEqual(client.calls[0]["system"], client.calls[1]["system"])

    def test_repeated_switches_override_old_mode_claims_without_rewriting_history(self):
        refusal = ModelResponse(stop_reason="end_turn", content=[{
            "type": "text", "text": "我处于 Discuss 模式，请先切换到 Code 模式。",
        }])
        client = ScriptedClient(refusal, end_turn(), end_turn(), end_turn())
        agent = self.make_agent(client=client)
        agent.run("讨论实现")
        history = deepcopy(agent.messages)
        agent.set_discuss_mode(False)
        agent.run("现在实现")
        self.assertEqual(client.calls[1]["messages"][:len(history)], history)
        transition = client.calls[1]["messages"][len(history)]
        self.assertEqual(transition["role"], "user")
        self.assertIn("[运行时模式更新]", transition["content"])
        self.assertIn("本轮当前执行模式：Code · 编码", transition["content"])
        self.assertIn("Discuss 只读限制已解除", transition["content"])
        self.assertIn("我处于 Discuss 模式", repr(client.calls[1]["messages"]))
        self.assertIn("当前执行模式：Code · 编码", client.calls[1]["system"])
        self.assertIn("历史消息、工具拒绝或摘要中的模式只描述当时状态", client.calls[1]["system"])
        self.assertNotIn("[DISCUSS MODE · 只读讨论]", client.calls[1]["system"])
        agent.set_discuss_mode(True)
        agent.run("继续讨论")
        self.assertIn("Discuss 只读限制已启用", client.calls[2]["messages"][-2]["content"])
        self.assertIn("当前执行模式：Discuss · 只读讨论", client.calls[2]["system"])
        agent.set_discuss_mode(False)
        agent.run("继续实现")
        self.assertEqual(client.calls[1]["system"], client.calls[3]["system"])

    def test_legacy_checkpoint_announces_current_mode_once_and_persists_it(self):
        history = [
            {"role": "user", "content": "可以编写代码吗"},
            {"role": "assistant", "content": "不能，我处于 Discuss 模式。"},
        ]
        state = _restore_runtime_state({"user_goal": "可以编写代码吗"})
        self.assertIsNone(state.last_prompt_mode)
        client = ScriptedClient(end_turn(), end_turn())
        agent = Agent(
            client=client, tools=ToolRegistry(), config=AgentConfig(model="fake"),
            messages=deepcopy(history), prompt_mode=PromptMode.NORMAL,
            context=ContextManager(config=ContextConfig(mode="off"), state=state),
        )
        agent.run("你现在可以编写代码吗")
        self.assertEqual(client.calls[0]["messages"][:len(history)], history)
        self.assertIn("本轮当前执行模式：Code · 编码", client.calls[0]["messages"][-2]["content"])
        self.assertEqual(client.calls[0]["messages"][-1]["content"], "你现在可以编写代码吗")
        persisted = serialize_runtime_state(agent.context.state)
        self.assertEqual(persisted["last_prompt_mode"], "normal")
        restored = Agent(
            client=client, tools=ToolRegistry(), config=AgentConfig(model="fake"),
            messages=deepcopy(agent.messages), prompt_mode=PromptMode.NORMAL,
            context=ContextManager(config=ContextConfig(mode="off"), state=_restore_runtime_state(persisted)),
        )
        restored.run("继续")
        updates = [m for m in client.calls[1]["messages"] if isinstance(m["content"], str) and m["content"].startswith("[运行时模式更新]")]
        self.assertEqual(len(updates), 1)

    def test_mode_update_survives_selected_memory_without_changing_user_prompt(self):
        client = ScriptedClient(end_turn())
        agent = self.make_agent(client=client, mode=PromptMode.NORMAL)
        agent.messages = [{"role": "user", "content": "之前讨论"}, {"role": "assistant", "content": "Discuss"}]
        agent.context.state.last_prompt_mode = "discuss"
        with patch.object(Agent, "_selected_memory_context", return_value="用户偏好中文"):
            agent.run("请实现功能")
        sent = client.calls[0]["messages"]
        self.assertIn("本轮当前执行模式：Code · 编码", sent[-2]["content"])
        self.assertIn("用户偏好中文", sent[-1]["content"][0]["text"])
        self.assertEqual(sent[-1]["content"][-1]["text"], "请实现功能")

    def test_fresh_or_team_runs_do_not_get_ordinary_mode_transitions(self):
        for mode in (PromptMode.NORMAL, PromptMode.DISCUSS, PromptMode.SUBAGENT):
            with self.subTest(mode=mode):
                client = ScriptedClient(end_turn())
                agent = self.make_agent(client=client, mode=mode)
                agent.run("检查")
                self.assertNotIn("[运行时模式更新]", repr(client.calls[0]["messages"]))
        agent = self.make_agent(mode=PromptMode.TEAM_PLANNER)
        agent.messages = [{"role": "user", "content": "之前的任务"}, {"role": "assistant", "content": "done"}]
        agent.run("继续规划")
        self.assertNotIn("[运行时模式更新]", repr(agent.client.calls[0]["messages"]))

    def test_shared_hooks_do_not_leak_mode_to_other_agents(self):
        hooks = HookManager()
        discuss = self.make_agent(hooks=hooks)
        normal = self.make_agent(hooks=hooks, mode=PromptMode.NORMAL)
        normal.tools.register_handler(ToolDefinition("write_file", "", {"type": "object"}), lambda: "ok")
        call = ToolUse("id", "write_file", {})
        self.assertIsNotNone(discuss.hooks.trigger("PreToolUse", call))
        self.assertIsNone(normal.hooks.trigger("PreToolUse", call))
        self.assertIsNone(hooks.trigger("PreToolUse", call))

    def test_memory_maintenance_is_skipped(self):
        agent = self.make_agent()
        manager = Mock()
        agent.memory_manager = manager
        agent._after_turn_memory()
        manager.after_turn.assert_not_called()

    def test_team_role_cannot_be_unlocked_by_toggle(self):
        agent = self.make_agent(mode=PromptMode.TEAM_PLANNER)
        with self.assertRaises(ValueError):
            agent.set_discuss_mode(False)

    def test_web_factory_restores_history_with_current_mode_and_read_only_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = SQLiteRepository(root / "state.db", recover_incomplete=False)
            conversation = repository.create_conversation(title="Discuss", workspace=str(root))
            env = EnvironmentConfig(
                model_id="fake", data_dir=root / "data", enable_skills=False,
                context_config=ContextConfig(mode="off"),
                memory_config=MemoryConfig(selection_mode="simple", auto_extract=True),
            )
            client = ScriptedClient(
                ModelResponse(stop_reason="end_turn", content=[{
                    "type": "text", "text": "当前处于 Discuss 模式，无法修改文件。",
                }]),
                ModelResponse(stop_reason="tool_use", content=[{
                    "type": "tool_use", "id": "restored-write", "name": "write_file",
                    "input": {"file_path": "restored.txt", "content": "ok"},
                }]),
                end_turn(),
            )
            emitter = EventEmitter(context=ExecutionContext(conversation_id=conversation.id, run_id="test-run"))
            factory = WebAgentFactory(env, root, repository)
            try:
                with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
                    agent = factory.create(
                        event_emitter=emitter, cancellation=CancellationToken(),
                        permission_broker=WaitingPermissionBroker(), root_prompt_mode=PromptMode.DISCUSS,
                    )
                    self.assertTrue(agent.discuss_mode)
                    self.assertIn("Discuss mode", agent._execute_tools([
                        ToolUse("w", "write_file", {"file_path": "should-not-exist", "content": "x"})
                    ])[0]["content"])
                    self.assertFalse((root / "should-not-exist").exists())
                    memory = agent.memory_manager
                    with patch.object(type(memory), "after_turn") as maintenance:
                        agent.run("inspect only")
                        maintenance.assert_not_called()
                    checkpoint = SimpleNamespace(
                        messages=agent.messages, context=serialize_runtime_state(agent.context.state)
                    )
                    normal = factory.create(
                        event_emitter=emitter, cancellation=CancellationToken(),
                        permission_broker=WaitingPermissionBroker(), checkpoint=checkpoint,
                    )
                    self.assertFalse(normal.discuss_mode)
                    self.assertEqual(normal.messages, agent.messages)
                    with patch.object(type(normal.memory_manager), "after_turn"):
                        normal.run("已切回 Code，创建 restored.txt")
                    self.assertIn("当前执行模式：Code · 编码", client.calls[1]["system"])
                    self.assertIn("本轮当前执行模式：Code · 编码", client.calls[1]["messages"][-2]["content"])
                    self.assertNotIn("[DISCUSS MODE · 只读讨论]", client.calls[1]["system"])
                    self.assertIn("当前处于 Discuss 模式，无法修改文件", repr(client.calls[1]["messages"]))
                    self.assertEqual((root / "restored.txt").read_text(encoding="utf-8"), "ok")
            finally:
                factory.close()
                repository.close()


if __name__ == "__main__":
    unittest.main()
