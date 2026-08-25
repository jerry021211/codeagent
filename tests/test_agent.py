from __future__ import annotations

import unittest
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codeagent import (
    Agent,
    AgentConfig,
    CallbackEventSink,
    EnvironmentConfig,
    EventEmitter,
    HookManager,
    ModelResponse,
    RecoveryConfig,
    RecoveryRuntime,
    ToolDefinition,
    ToolRegistry,
)
from codeagent.context import ContextManager, RuntimeState
from codeagent.tools import SearchMemoryTool, TodoStore, TodoWriteTool
from codeagent.memory import MemoryConfig, MemoryManager, MemoryStore


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
        self.fork_calls = []

    def create_message(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def fork(self, **kwargs):
        self.fork_calls.append(dict(kwargs))
        return self


class AgentTests(unittest.TestCase):
    def test_legacy_conversation_gets_tool_change_notice(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )
            ]
        )
        state = RuntimeState()
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model"),
            context=ContextManager(state=state),
            messages=[
                {"role": "user", "content": "Use Context7"},
                {"role": "assistant", "content": "That tool is unavailable"},
            ],
        )

        agent.run("Try again")

        self.assertIn(
            "registered tool set has changed since the previous turn",
            client.calls[0]["system"],
        )
        self.assertTrue(state.tool_schema_hash)

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
            config=AgentConfig(model="fake-model"),
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

    def test_subagent_tool_runs_with_fresh_messages_and_returns_summary(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_parent",
                            "name": "subagent",
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
            config=AgentConfig(model="fake-model"),
        )

        result = agent.run("delegate this")

        self.assertEqual(result.final_text, "parent final")
        self.assertEqual(agent.subagent_max_iterations, 30)
        subagent_fork = next(
            call for call in client.fork_calls if call.get("call_kind") == "subagent"
        )
        self.assertTrue(subagent_fork["stream"])
        self.assertEqual(
            client.calls[1]["messages"][0],
            {"role": "user", "content": "inspect the project"},
        )
        self.assertEqual(len(client.calls[1]["messages"]), 1)
        self.assertIn("Current workspace:", client.calls[1]["system"])
        self.assertIn("## Outcome", client.calls[1]["system"])
        subagent_tool_names = {tool["name"] for tool in client.calls[1]["tools"]}
        self.assertIn("echo", subagent_tool_names)
        self.assertNotIn("subagent", subagent_tool_names)
        self.assertEqual(
            agent.messages[2]["content"][0],
            {
                "type": "tool_result",
                "tool_use_id": "toolu_parent",
                "content": "subagent conclusion",
            },
        )

    def test_subagent_failure_is_reported_once_and_not_completed(self) -> None:
        error = ValueError(
            "Streaming is required for operations that may take longer than 10 minutes."
        )
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_parent",
                            "name": "subagent",
                            "input": {"description": "implement the backend"},
                        }
                    ],
                ),
                error,
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "parent handled failure"}],
                ),
            ]
        )
        events = []
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(model="fake-model"),
            recovery_runtime=RecoveryRuntime(
                RecoveryConfig(max_retries=10, sleep_enabled=False)
            ),
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )

        result = agent.run("delegate this")

        self.assertEqual(result.final_text, "parent handled failure")
        self.assertEqual(len(client.calls), 3)
        tool_result = agent.messages[2]["content"][0]["content"]
        self.assertTrue(tool_result.startswith("Error: Subagent failed"))
        child_events = [
            event.type for event in events if event.parent_agent_id == "agent_root"
        ]
        self.assertIn("subagent.failed", child_events)
        self.assertNotIn("subagent.completed", child_events)

    def test_subagent_logs_enter_and_exit_markers(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="tool_use",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_parent",
                            "name": "subagent",
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
            config=AgentConfig(model="fake-model"),
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

    def test_subagent_guidance_is_added_when_subagents_are_enabled(self) -> None:
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
            config=AgentConfig(model="fake-model"),
        )

        agent.run("do work")

        self.assertIn("interactive coding agent", client.system_prompt)
        self.assertIn("Use the subagent tool", client.system_prompt)

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
            config=AgentConfig(model="fake-model"),
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
            config=AgentConfig(model="fake-model"),
        )

        agent.run("do work")

        self.assertIn("interactive coding agent", client.system_prompt)
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
            config=AgentConfig(model="fake-model"),
            skill_catalog="Available skills:\n- python-refactor: Refactor Python.",
        )

        agent.run("do work")

        self.assertIn("Available skills:", client.system_prompt)
        self.assertIn("python-refactor", client.system_prompt)
        self.assertIn("Use load_skill(name)", client.system_prompt)

    def test_agent_adds_memory_catalog_when_available(self) -> None:
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
        store = MemoryStore(".memory-test")
        tools = ToolRegistry()
        tools.register(SearchMemoryTool(store=store))
        agent = Agent(
            client=client,
            tools=tools,
            config=AgentConfig(model="fake-model"),
            memory_catalog=(
                "Available memories:\n"
                "- Project Style [project]: Explain call chains first."
            ),
        )

        agent.run("do work")

        self.assertIn("Available memories:", client.system_prompt)
        self.assertIn("Project Style", client.system_prompt)
        self.assertIn("Use long-term memory selectively", client.system_prompt)

    def test_agent_injects_llm_selected_memory_context(self) -> None:
        class SelectionClient:
            def __init__(self) -> None:
                self.calls = []

            def fork(self, **kwargs):
                return self

            def create_message(self, **kwargs):
                self.calls.append(deepcopy(kwargs))
                if len(self.calls) == 1:
                    return ModelResponse(
                        stop_reason="end_turn",
                        content=[
                            {
                                "type": "text",
                                "text": '{"selected_memories":["project-style.md"]}',
                            }
                        ],
                    )
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))
            store.remember(
                name="Project Style",
                memory_type="project",
                description="Explain call chains first.",
                content="For this project, explain the call chain first.",
            )
            manager = MemoryManager(store, MemoryConfig(selection_mode="llm"))
            client = SelectionClient()
            agent = Agent(
                client=client,
                tools=ToolRegistry(),
                config=AgentConfig(model="deepseek-v4-pro"),
                memory_manager=manager,
                memory_catalog=manager.catalog_prompt(),
            )

            agent.run("Explain agent.py")

            self.assertIn("selected_memories", client.calls[0]["messages"][0]["content"])
            turn_content = client.calls[1]["messages"][0]["content"]
            self.assertIn("Selected long-term memories", turn_content[0]["text"])
            self.assertIn("explain the call chain first", turn_content[0]["text"])
            self.assertEqual(turn_content[1]["text"], "Explain agent.py")
            self.assertNotIn("Selected long-term memories", client.calls[1]["system"])
            self.assertNotIn("Available memories:", client.calls[1]["system"])

    def test_memory_selection_runs_once_during_tool_loop(self) -> None:
        class SelectionClient:
            def __init__(self) -> None:
                self.calls = []

            def fork(self, **kwargs):
                return self

            def create_message(self, **kwargs):
                self.calls.append(deepcopy(kwargs))
                if len(self.calls) == 1:
                    return ModelResponse(
                        stop_reason="end_turn",
                        content=[
                            {
                                "type": "text",
                                "text": '{"selected_memories":["project-style.md"]}',
                            }
                        ],
                    )
                if len(self.calls) == 2:
                    return ModelResponse(
                        stop_reason="tool_use",
                        content=[
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "echo",
                                "input": {},
                            }
                        ],
                    )
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))
            store.remember(
                name="Project Style",
                memory_type="project",
                description="Explain call chains first.",
                content="For this project, explain the call chain first.",
            )
            tools = ToolRegistry()
            tools.register_handler(
                ToolDefinition(
                    name="echo",
                    description="Echo.",
                    input_schema={"type": "object", "properties": {}},
                ),
                lambda: "ok",
            )
            client = SelectionClient()
            agent = Agent(
                client=client,
                tools=tools,
                config=AgentConfig(model="deepseek-v4-pro"),
                memory_manager=MemoryManager(
                    store,
                    MemoryConfig(selection_mode="llm"),
                ),
            )

            result = agent.run("Inspect the project")

            self.assertEqual(result.final_text, "done")
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(
                client.calls[2]["messages"][: len(client.calls[1]["messages"])],
                client.calls[1]["messages"],
            )

    def test_agent_recovers_from_max_tokens_by_retrying_with_more_tokens(self) -> None:
        client = SequenceClient(
            [
                ModelResponse(
                    stop_reason="max_tokens",
                    content=[{"type": "text", "text": "partial"}],
                ),
                ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                ),
            ]
        )
        agent = Agent(
            client=client,
            tools=ToolRegistry(),
            config=AgentConfig(
                model="fake-model",
                max_tokens=100,
            ),
            recovery_runtime=RecoveryRuntime(
                RecoveryConfig(escalated_max_tokens=1000, sleep_enabled=False)
            ),
        )

        result = agent.run("write a long answer")

        self.assertEqual(result.final_text, "done")
        self.assertEqual(client.calls[0]["max_tokens"], 100)
        self.assertEqual(client.calls[1]["max_tokens"], 1000)
        self.assertNotIn({"role": "assistant", "content": [{"type": "text", "text": "partial"}]}, agent.messages)

    def test_environment_config_reads_model_settings(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MODEL_ID": "test-model",
                "API_KEY": "test-key",
                "BASE_URL": "https://example.test",
                "MAX_TOKENS": "1234",
                "MAX_ITERATIONS": "7",
                "ENABLE_SKILLS": "false",
                "SKILLS_DIR": "project-skills",
                "CONTEXT_COMPACT_MODE": "model",
                "SUMMARIZATION_MODEL_ID": "summary-model",
                "SUMMARIZATION_API_KEY": "summary-key",
                "CONTEXT_TOOL_RESULT_BUDGET_CHARS": "111",
                "ENABLE_MEMORY": "true",
                "MEMORY_DIR": "project-memory",
                "MEMORY_SELECTION_MODE": "llm",
                "MEMORY_SESSION_BUDGET_CHARS": "60000",
                "MEMORY_AUTO_EXTRACT": "true",
                "MEMORY_ALLOW_SUBAGENT_WRITE": "true",
                "RECOVERY_ENABLED": "true",
                "RECOVERY_MAX_RETRIES": "4",
                "RECOVERY_ESCALATED_MAX_TOKENS": "9000",
                "FALLBACK_MODEL_ID": "fallback-model",
                "RECOVERY_TRACE": "true",
            },
            clear=True,
        ):
            env = EnvironmentConfig.from_env()

        self.assertEqual(env.model_id, "test-model")
        self.assertEqual(env.api_key, "test-key")
        self.assertEqual(env.base_url, "https://example.test")
        self.assertEqual(env.to_agent_config().max_tokens, 1234)
        self.assertEqual(env.to_agent_config().max_iterations, 7)
        self.assertFalse(env.enable_skills)
        self.assertEqual([str(path) for path in env.skill_roots], ["project-skills"])
        self.assertEqual(env.context_config.mode, "model")
        self.assertEqual(env.context_config.summarization_model, "summary-model")
        self.assertEqual(env.context_config.summarization_api_key, "summary-key")
        self.assertEqual(env.context_config.tool_result_budget_chars, 111)
        self.assertTrue(env.memory_config.enabled)
        self.assertEqual(str(env.memory_config.memory_dir), "project-memory")
        self.assertEqual(env.memory_config.selection_mode, "llm")
        self.assertEqual(env.memory_config.session_budget_chars, 60000)
        self.assertTrue(env.memory_config.auto_extract)
        self.assertTrue(env.memory_config.allow_subagent_write)
        self.assertTrue(env.recovery_config.enabled)
        self.assertEqual(env.recovery_config.max_retries, 4)
        self.assertEqual(env.recovery_config.escalated_max_tokens, 9000)
        self.assertEqual(env.recovery_config.fallback_model, "fallback-model")
        self.assertTrue(env.recovery_config.trace)


if __name__ == "__main__":
    unittest.main()
