from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent import (
    Agent,
    AgentConfig,
    HookManager,
    ModelResponse,
    PermissionPolicy,
    ToolDefinition,
    ToolRegistry,
)
from codeagent.tools import (
    TodoStore,
    create_task_reminder_hook,
    create_todo_reminder_hook,
)


class OneToolCallClient:
    def __init__(self, tool_name: str, tool_input: dict, final_text: str = "done") -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.final_text = final_text

    def create_message(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": self.tool_name,
                        "input": self.tool_input,
                    }
                ],
            )
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": self.final_text}],
        )


class PermissionPolicyTests(unittest.TestCase):
    def test_mcp_tools_require_approval(self) -> None:
        requests = []
        policy = PermissionPolicy(
            ask=lambda tool_name, tool_input, reason: requests.append(
                (tool_name, tool_input, reason)
            )
            or False,
        )

        decision = policy.check("mcp__github__search", {"query": "mcp"})

        self.assertFalse(decision.allowed)
        self.assertEqual(requests[0][2], "External MCP tool call")

    def test_blocks_hard_denied_bash_command(self) -> None:
        policy = PermissionPolicy()

        decision = policy.check("bash", {"command": "sudo reboot"})

        self.assertFalse(decision.allowed)
        self.assertIn("sudo", decision.reason)

    def test_blocks_windows_hard_denied_bash_command(self) -> None:
        policy = PermissionPolicy()

        decision = policy.check("bash", {"command": "format C:"})

        self.assertFalse(decision.allowed)
        self.assertIn("format", decision.reason)

    def test_prompts_for_windows_destructive_commands(self) -> None:
        policy = PermissionPolicy(
            ask=lambda tool_name, tool_input, reason: False,
        )

        decision = policy.check("bash", {"command": "del permission_allowed.txt"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "Permission denied by user")

    def test_prompts_for_powershell_destructive_commands_case_insensitively(self) -> None:
        policy = PermissionPolicy(
            ask=lambda tool_name, tool_input, reason: False,
        )

        decision = policy.check(
            "bash",
            {"command": "Remove-Item permission_allowed.txt"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "Permission denied by user")

    def test_denies_workspace_escape_without_offering_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prompts = 0

            def ask(tool_name, tool_input, reason):
                nonlocal prompts
                prompts += 1
                return True

            policy = PermissionPolicy(
                workspace=Path(temp_dir),
                ask=ask,
            )

            decision = policy.check("write_file", {"file_path": "../outside.txt"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "Writing outside workspace")
        self.assertEqual(prompts, 0)

    def test_allows_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            policy = PermissionPolicy(
                workspace=Path(temp_dir),
                ask=lambda tool_name, tool_input, reason: False,
            )

            decision = policy.check("write_file", {"file_path": "inside.txt"})

        self.assertTrue(decision.allowed)


class HookedAgentTests(unittest.TestCase):
    def test_pre_tool_hook_can_block_execution(self) -> None:
        tools = ToolRegistry()
        executed = False

        def fail_if_executed() -> str:
            nonlocal executed
            executed = True
            return "executed"

        tools.register_handler(
            ToolDefinition(
                name="danger",
                description="Danger tool.",
                input_schema={"type": "object", "properties": {}},
            ),
            fail_if_executed,
        )
        hooks = HookManager()
        hooks.register("PreToolUse", lambda tool_use: "blocked by hook")
        agent = Agent(
            client=OneToolCallClient("danger", {}),
            tools=tools,
            config=AgentConfig(model="fake-model"),
            hooks=hooks,
        )

        result = agent.run("run danger")

        self.assertFalse(executed)
        self.assertEqual(agent.messages[2]["content"][0]["content"], "blocked by hook")
        self.assertEqual(result.final_text, "done")

    def test_lifecycle_hooks_fire(self) -> None:
        events: list[str] = []
        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: "ok",
        )
        hooks = HookManager()
        hooks.register("UserPromptSubmit", lambda prompt: events.append("user") or None)
        hooks.register("PreToolUse", lambda tool_use: events.append("pre") or None)
        hooks.register("PostToolUse", lambda tool_use, output: events.append("post") or None)
        hooks.register("Stop", lambda messages: events.append("stop") or None)
        agent = Agent(
            client=OneToolCallClient("echo", {}),
            tools=tools,
            config=AgentConfig(model="fake-model"),
            hooks=hooks,
        )

        agent.run("say hello")

        self.assertEqual(events, ["user", "pre", "post", "stop"])

    def test_todo_reminder_hook_nudges_stale_plan(self) -> None:
        store = TodoStore()
        hook = create_todo_reminder_hook(store, interval=1)
        messages = [
            {"role": "user", "content": "do work"},
            {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]},
        ]

        reminder = hook(messages)

        self.assertIsNone(reminder)
        store.replace([{"content": "修改并验证", "status": "in_progress"}])
        self.assertIsNone(hook(messages))
        self.assertIn("todo_write", hook(messages))

    def test_todo_reminder_resets_after_todo_update(self) -> None:
        store = TodoStore()
        hook = create_todo_reminder_hook(store, interval=1)
        messages = [
            {"role": "user", "content": "do work"},
            {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]},
        ]

        store.replace([{"content": "Plan the change", "status": "in_progress"}])

        self.assertIsNone(hook(messages))

    def test_task_reminder_nudges_stale_persistent_progress(self) -> None:
        hook = create_task_reminder_hook(
            lambda: '[{"id":"1","subject":"Review storage","status":"in_progress"}]',
            interval=2,
        )
        messages = [{"role": "user", "content": "review tasks"}]

        self.assertIsNone(hook(messages))
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "read_file", "input": {}}
                ],
            }
        )
        self.assertIsNone(hook(messages))
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "grep", "input": {}}
                ],
            }
        )

        reminder = hook(messages)

        self.assertIsNotNone(reminder)
        self.assertIn("Review storage", reminder)
        self.assertIn("连续 2 次模型调用未更新", reminder)

    def test_task_reminder_resets_after_task_update(self) -> None:
        hook = create_task_reminder_hook(
            lambda: '[{"id":"1","status":"in_progress"}]',
            interval=1,
        )
        messages = [{"role": "user", "content": "review tasks"}]
        self.assertIsNone(hook(messages))
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "TaskUpdate", "input": {}}
                ],
            }
        )

        self.assertIsNone(hook(messages))


if __name__ == "__main__":
    unittest.main()
