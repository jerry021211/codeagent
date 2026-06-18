from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from codeagent import (
    Agent,
    AgentConfig,
    EnvironmentConfig,
    HookManager,
    ModelResponse,
    ToolDefinition,
    ToolRegistry,
)
from codeagent.tools import TodoStore, TodoWriteTool


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_message(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "echo",
                        "input": {"message": "hello"},
                    }
                ],
            )
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": "done"}],
        )


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.responses.pop(0)


class AgentTests(unittest.TestCase):
    def test_agent_executes_tool_and_continues(self) -> None:
        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition(
                name="echo",
                description="Echo a message.",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            ),
            lambda message: f"echo:{message}",
        )

        agent = Agent(
            client=FakeClient(),
            tools=tools,
            config=AgentConfig(model="fake-model", system_prompt="test"),
        )
        result = agent.run("say hello")

        self.assertEqual(result.final_text, "done")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(
            agent.messages[2]["content"][0],
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "echo:hello",
            },
        )

    def test_task_tool_runs_subagent_with_fresh_messages_and_returns_summary(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_parent",
                            "name": "task",
                            "input": {"description": "inspect the project"},
                        }
                    ],
                ),
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "subagent conclusion"}],
                ),
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "parent final"}],
                ),
            ]
        )
        tools = ToolRegistry()
        tools.register_handler(
            ToolDefinition(
                name="echo",
                description="Echo a message.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: "echo",
        )
        agent = Agent(
            client=client,
            tools=tools,
            config=AgentConfig(model="fake-model", system_prompt="test"),
        )

        result = agent.run("delegate this")

        self.assertEqual(result.final_text, "parent final")
        self.assertEqual(client.calls[1]["messages"], [
            {"role": "user", "content": "inspect the project"}
        ])
        subagent_tool_names = {tool["name"] for tool in client.calls[1]["tools"]}
        self.assertIn("echo", subagent_tool_names)
        self.assertNotIn("task", subagent_tool_names)
        self.assertEqual(
            agent.messages[2]["content"][0],
            {
                "type": "tool_result",
                "tool_use_id": "toolu_parent",
                "content": "subagent conclusion",
            },
        )

    def test_subagent_logs_enter_and_exit_markers(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_parent",
                            "name": "task",
                            "input": {"description": "inspect markers"},
                        }
                    ],
                ),
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "subagent conclusion"}],
                ),
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "parent final"}],
                ),
            ]
        )
        markers = []
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model", system_prompt="test"),
            subagent_log=markers.append,
        )

        agent.run("delegate this")

        self.assertEqual(
            markers,
            [
                "[subagent enter] inspect markers",
                "[subagent exit] returned to parent agent",
            ],
        )

    def test_task_guidance_is_added_when_subagents_are_enabled(self) -> None:
        class CaptureClient:
            def __init__(self) -> None:
                self.system_prompt = ""

            def create_message(self, **kwargs):
                self.system_prompt = kwargs["system"]
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        client = CaptureClient()
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model", system_prompt="base prompt"),
        )

        agent.run("do work")

        self.assertIn("base prompt", client.system_prompt)
        self.assertIn("Use the task tool", client.system_prompt)

    def test_agent_injects_before_model_call_reminders(self) -> None:
        class EndTurnClient:
            def create_message(self, **kwargs):
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        hooks = HookManager()
        hooks.register("BeforeModelCall", lambda messages: "<reminder>plan</reminder>")
        agent = Agent(
            client=EndTurnClient(),
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model", system_prompt="test"),
            hooks=hooks,
        )

        agent.run("do work")

        self.assertEqual(agent.messages[1]["content"], "<reminder>plan</reminder>")

    def test_agent_adds_todo_guidance_when_tool_is_available(self) -> None:
        class CaptureClient:
            def __init__(self) -> None:
                self.system_prompt = ""

            def create_message(self, **kwargs):
                self.system_prompt = kwargs["system"]
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        client = CaptureClient()
        tools = ToolRegistry()
        tools.register(TodoWriteTool(store=TodoStore()))
        agent = Agent(
            client=client,
            tools=tools,
            config=AgentConfig(model="fake-model", system_prompt="base prompt"),
        )

        agent.run("do work")

        self.assertIn("base prompt", client.system_prompt)
        self.assertIn("call todo_write before", client.system_prompt)

    def test_agent_adds_skill_catalog_when_available(self) -> None:
        class CaptureClient:
            def __init__(self) -> None:
                self.system_prompt = ""

            def create_message(self, **kwargs):
                self.system_prompt = kwargs["system"]
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        client = CaptureClient()
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model", system_prompt="base prompt"),
            skill_catalog="Available skills:\n- python-refactor: Refactor Python.",
        )

        agent.run("do work")

        self.assertIn("Available skills:", client.system_prompt)
        self.assertIn("python-refactor", client.system_prompt)
        self.assertIn("Use load_skill(name)", client.system_prompt)

    def test_environment_config_reads_model_settings(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MODEL_ID": "test-model",
                "API_KEY": "test-key",
                "BASE_URL": "https://example.test",
                "MAX_TOKENS": "1234",
                "MAX_ITERATIONS": "7",
                "SYSTEM_PROMPT": "custom prompt",
                "ENABLE_SKILLS": "false",
                "SKILLS_DIR": "project-skills",
                "CONTEXT_COMPACT_MODE": "model",
                "CONTEXT_MAX_MESSAGES": "9",
                "CONTEXT_TOOL_RESULT_BUDGET_CHARS": "111",
            },
            clear=True,
        ):
            env = EnvironmentConfig.from_env()

        self.assertEqual(env.model_id, "test-model")
        self.assertEqual(env.api_key, "test-key")
        self.assertEqual(env.base_url, "https://example.test")
        self.assertEqual(env.to_agent_config().max_tokens, 1234)
        self.assertEqual(env.to_agent_config().max_iterations, 7)
        self.assertEqual(env.to_agent_config().system_prompt, "custom prompt")
        self.assertFalse(env.enable_skills)
        self.assertEqual([str(path) for path in env.skill_roots], ["project-skills"])
        self.assertEqual(env.context_config.mode, "model")
        self.assertEqual(env.context_config.max_messages, 9)
        self.assertEqual(env.context_config.tool_result_budget_chars, 111)


if __name__ == "__main__":
    unittest.main()
