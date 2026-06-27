"""Agent loop implementation.

The loop follows the harness pattern from the reference repository:
call the model, execute requested tools, append tool results, repeat.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextManager
from codeagent.hooks import HookManager
from codeagent.memory import MemoryManager
from codeagent.messages import Message, ToolUse, extract_text, normalize_tool_uses
from codeagent.tracing import trace_run
from codeagent.tools import (
    COMPACT_TOOL_NAME,
    LOAD_MEMORY_TOOL_NAME,
    REMEMBER_TOOL_NAME,
    SEARCH_MEMORY_TOOL_NAME,
    TASK_TOOL_NAME,
    CompactTool,
    TaskTool,
    ToolRegistry,
)

SubagentEnvironment = (
    tuple[ToolRegistry, HookManager]
    | tuple[ToolRegistry, HookManager, ContextManager]
)

TODO_SYSTEM_GUIDANCE = (
    "When a task has multiple steps, requires code changes, or may take more "
    "than one tool call, call todo_write before using read_file, bash, "
    "write_file, or edit_file. Keep the todo list concise and update it as "
    "work moves through pending, in_progress, and completed. Keep at most one "
    "todo in_progress. The todo_write tool is for planning only and does not "
    "perform work."
)

TASK_SYSTEM_GUIDANCE = (
    "Use the task tool for focused subtasks that need their own investigation "
    "or multiple tool calls. The task tool starts a subagent with a fresh "
    "message list and returns only the subagent's final conclusion, so do not "
    "expect its intermediate reasoning or tool history to remain in your "
    "conversation."
)

SUBAGENT_SYSTEM_PROMPT = (
    "You are a focused coding subagent. Complete only the delegated task. "
    "Use your available tools as needed, then return a concise final "
    "conclusion for the parent agent. Include important files changed, tests "
    "run, or blockers when relevant. Do not delegate further."
)

SKILL_SYSTEM_GUIDANCE = (
    "Use load_skill(name) only when the user's task matches a listed skill. "
    "Do not load every skill. Load the most relevant skill before applying "
    "its specialized workflow."
)

MEMORY_SYSTEM_GUIDANCE = (
    "Use long-term memory selectively. Search or load memories when the task "
    "may depend on prior user preferences, project conventions, or reusable "
    "decisions. Use remember only for stable facts that should help future "
    "turns; do not store secrets, temporary task status, or large code blocks."
)

@dataclass(slots=True)
class AgentConfig:
    """Runtime settings for one agent instance."""

    model: str
    system_prompt: str
    max_tokens: int = 8000
    max_iterations: int = 50


@dataclass(slots=True)
class AgentResult:
    """Summary of a completed agent run."""

    messages: list[Message]
    final_text: str
    stop_reason: str
    iterations: int


@dataclass(slots=True)
class Agent:
    """A minimal coding-agent harness.

    The model owns reasoning and decides whether to call tools. The harness
    only provides the operational environment and feeds results back.
    """

    client: AnthropicModelClient
    tools: ToolRegistry
    config: AgentConfig
    hooks: HookManager = field(default_factory=HookManager)
    context: ContextManager = field(default_factory=ContextManager)
    memory_manager: MemoryManager | None = None
    messages: list[Message] = field(default_factory=list)
    allow_subagents: bool = True
    subagent_max_iterations: int = 30
    subagent_registry_factory: Callable[[], ToolRegistry] | None = None
    subagent_environment_factory: Callable[[], SubagentEnvironment] | None = None
    subagent_log: Callable[[str], None] | None = None
    skill_catalog: str = ""
    memory_catalog: str = ""
    _compact_requested: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.allow_subagents and TASK_TOOL_NAME not in self.tools:
            self.tools.register(TaskTool(spawn_fn=self._spawn_subagent))
        if COMPACT_TOOL_NAME not in self.tools:
            self.tools.register(CompactTool(compact_fn=self._request_manual_compact))

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})

    def run(self, prompt: str | None = None) -> AgentResult:
        """Run until the model stops requesting tools or the iteration limit hits."""

        if prompt is not None:
            self.hooks.trigger("UserPromptSubmit", prompt) #打印日志
            self.context.record_user_prompt(prompt)   
            self.add_user_message(prompt)

        with trace_run(
            "agent.run",
            run_type="chain",
            inputs={"prompt": prompt, "message_count": len(self.messages)},
            metadata={
                "model": self.config.model,
                "max_iterations": self.config.max_iterations,
                "max_tokens": self.config.max_tokens,
            },
        ) as run_trace:
            iterations = 0
            last_stop_reason = "not_started"

            while iterations < self.config.max_iterations:
                iterations += 1
                self.messages = self.context.prepare_before_model_call(
                    self.messages,
                    client=self.client,
                    model=self.config.model,
                    max_tokens=self.config.max_tokens,
                )
                reminder = self.hooks.trigger("BeforeModelCall", self.messages)
                if reminder:
                    self.add_user_message(str(reminder)) #如果有加入提醒该做todolist了

                tool_schemas = self.tools.schemas()
                try:
                    selected_memory_context = self._selected_memory_context()
                    response = self.client.create_message(
                        model=self.config.model,
                        system=self._system_prompt(
                            tool_schemas,
                            selected_memory_context=selected_memory_context,
                        ),
                        messages=self.messages,
                        tools=tool_schemas,
                        max_tokens=self.config.max_tokens,
                    )
                    self.context.reset_reactive_retries()
                except Exception as exc:
                    compacted = None
                    if _is_prompt_too_long(exc):
                        compacted = self.context.reactive_compact(
                            self.messages,
                            client=self.client,
                            model=self.config.model,
                            max_tokens=self.config.max_tokens,
                        )
                    if compacted is None:
                        raise
                    self.messages = compacted
                    continue
                last_stop_reason = response.stop_reason
                self.messages.append({"role": "assistant", "content": response.content})
                tool_uses = normalize_tool_uses(response.content)
                if response.stop_reason != "tool_use" or not tool_uses:
                    force_continue = self.hooks.trigger("Stop", self.messages)
                    if force_continue:
                        self.add_user_message(force_continue)
                        continue
                    result = AgentResult(
                        messages=self.messages,
                        final_text=extract_text(response.content),
                        stop_reason=response.stop_reason,
                        iterations=iterations,
                    )
                    self._after_turn_memory()
                    run_trace.end(
                        outputs={
                            "final_text": result.final_text,
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "message_count": len(result.messages),
                        }
                    )
                    return result

                self.messages.append(
                    {"role": "user", "content": self._execute_tools(tool_uses)}
                )
                if self._compact_requested:
                    self._compact_requested = False
                    self.messages = self.context.force_compact(
                        self.messages,
                        client=self.client,
                        model=self.config.model,
                        max_tokens=self.config.max_tokens,
                        reason="manual compact",
                    )

            result = AgentResult(
                messages=self.messages,
                final_text="",
                stop_reason=f"max_iterations:{last_stop_reason}",
                iterations=iterations,
            )
            self._after_turn_memory()
            run_trace.end(
                outputs={
                    "final_text": result.final_text,
                    "stop_reason": result.stop_reason,
                    "iterations": result.iterations,
                    "message_count": len(result.messages),
                }
            )
            return result

    def _execute_tools(self, tool_uses: list[ToolUse]) -> list[dict[str, Any]]:
        with trace_run(
            "agent.execute_tools",
            run_type="chain",
            inputs={
                "tools": [
                    {"id": tool_use.id, "name": tool_use.name, "input": tool_use.input}
                    for tool_use in tool_uses
                ]
            },
        ) as tools_trace:
            results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                blocked = self.hooks.trigger("PreToolUse", tool_use)
                if blocked:
                    output = str(blocked)
                else:
                    output = self.tools.execute(tool_use.name, tool_use.input)
                    output = self.context.compact_tool_output(tool_use, output)
                    self.context.record_tool_result(tool_use, output)
                    self.hooks.trigger("PostToolUse", tool_use, output)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": output,
                    }
                )
            tools_trace.end(outputs={"results": results})
            return results

    def _request_manual_compact(self) -> str:
        self._compact_requested = True
        return "[Compacted. History will be summarized before the next model call.]"

    def _spawn_subagent(self, description: str) -> str:
        task_description = str(description or "").strip()
        if not task_description:
            return "Error: task description is required."

        self._log_subagent_marker(f"[subagent enter] {task_description}")
        try:
            with trace_run(
                "agent.subagent",
                run_type="chain",
                inputs={"description": task_description},
                metadata={
                    "model": self.config.model,
                    "max_iterations": self.subagent_max_iterations,
                },
            ) as subagent_trace:
                sub_tools, sub_hooks, sub_context = self._subagent_environment()
                subagent = Agent(
                    client=self._subagent_client(),
                    tools=sub_tools,
                    config=AgentConfig(
                        model=self.config.model,
                        system_prompt=SUBAGENT_SYSTEM_PROMPT,
                        max_tokens=self.config.max_tokens,
                        max_iterations=self.subagent_max_iterations,
                    ),
                    hooks=sub_hooks,
                    context=sub_context,
                    allow_subagents=False,
                    subagent_log=self.subagent_log,
                    skill_catalog=self.skill_catalog,
                    memory_catalog=self.memory_catalog,
                )
                result = subagent.run(task_description)
                if result.final_text:
                    output = result.final_text
                else:
                    output = (
                        "Subagent stopped without a final conclusion "
                        f"({result.stop_reason}, {result.iterations} iterations)."
                    )
                subagent_trace.end(
                    outputs={
                        "result": output,
                        "stop_reason": result.stop_reason,
                        "iterations": result.iterations,
                    }
                )
                return output
        finally:
            self._log_subagent_marker("[subagent exit] returned to parent agent")

    def _log_subagent_marker(self, message: str) -> None:
        if self.subagent_log is not None:
            self.subagent_log(message)

    def _subagent_client(self) -> Any:
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            return fork(stream=False, on_text=None)
        return self.client

    def _subagent_tools(self) -> ToolRegistry:
        if self.subagent_registry_factory is not None:
            registry = self.subagent_registry_factory()
            if TASK_TOOL_NAME in registry:
                return registry.copy_without({TASK_TOOL_NAME})
            return registry
        return self.tools.copy_without({TASK_TOOL_NAME})

    def _subagent_environment(self) -> tuple[ToolRegistry, HookManager, ContextManager]:
        if self.subagent_environment_factory is not None:
            environment = self.subagent_environment_factory()
            registry, hooks = environment[0], environment[1]
            context = (
                environment[2]
                if len(environment) > 2
                else ContextManager(config=self.context.config)
            )
            if TASK_TOOL_NAME in registry:
                registry = registry.copy_without({TASK_TOOL_NAME})
            return registry, hooks, context
        return self._subagent_tools(), self.hooks, ContextManager(config=self.context.config)

    def _system_prompt(
        self,
        tool_schemas: list[dict[str, Any]],
        *,
        selected_memory_context: str = "",
    ) -> str:
        system_prompt = self.config.system_prompt
        has_todo_write = any(
            schema.get("name") == "todo_write" for schema in tool_schemas
        )
        if has_todo_write and "todo_write" not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{TODO_SYSTEM_GUIDANCE}"

        has_task = any(
            schema.get("name") == TASK_TOOL_NAME for schema in tool_schemas
        )
        if has_task and "task tool" not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{TASK_SYSTEM_GUIDANCE}"

        if self.skill_catalog and "Available skills:" not in system_prompt:
            system_prompt = (
                f"{system_prompt}\n\n{self.skill_catalog}\n\n{SKILL_SYSTEM_GUIDANCE}"
            )

        has_memory_tools = any(
            schema.get("name")
            in {SEARCH_MEMORY_TOOL_NAME, LOAD_MEMORY_TOOL_NAME, REMEMBER_TOOL_NAME}
            for schema in tool_schemas
        )
        if selected_memory_context:
            system_prompt = f"{system_prompt}\n\n{selected_memory_context}"
        elif (
            self.memory_catalog
            and "Available memories:" not in system_prompt
            and (
                self.memory_manager is None
                or self.memory_manager.config.selection_mode != "llm"
            )
        ):
            system_prompt = f"{system_prompt}\n\n{self.memory_catalog}"
        if has_memory_tools and "long-term memory" not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{MEMORY_SYSTEM_GUIDANCE}"
        return system_prompt

    def _selected_memory_context(self) -> str:
        if self.memory_manager is None:
            return ""
        try:
            return self.memory_manager.select_context(
                self.messages,
                client=self._memory_client(),
                model=self.config.model,
                max_tokens=self.config.max_tokens,
            )
        except Exception:
            return ""

    def _memory_client(self) -> Any:
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            return fork(stream=False, on_text=None)
        return self.client

    def _after_turn_memory(self) -> None:
        if self.memory_manager is None:
            return
        try:
            self.memory_manager.after_turn(
                self.messages,
                client=self.client,
                model=self.config.model,
                max_tokens=self.config.max_tokens,
            )
        except Exception:
            return


def _is_prompt_too_long(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(
        marker in text
        for marker in (
            "prompt_too_long",
            "prompt too long",
            "context length",
            "maximum context",
            "too many tokens",
            "413",
        )
    )
