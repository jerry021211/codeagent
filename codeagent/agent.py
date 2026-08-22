"""Agent loop implementation.

The loop follows the harness pattern from the reference repository:
call the model, execute requested tools, append tool results, repeat.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextManager, HistoryObserver
from codeagent.events import EventEmitter, TokenTotals, UsageTracker
from codeagent.hooks import HookManager
from codeagent.memory import MemoryManager
from codeagent.messages import Message, ToolUse, extract_text, normalize_tool_uses
from codeagent.prompts import PromptAssemblyResult, PromptMode, PromptRuntime
from codeagent.planning import PlanningBackend
from codeagent.recovery import RecoveryRuntime
from codeagent.runtime import CancellationToken
from codeagent.tracing import trace_run
from codeagent.tools import (
    COMPACT_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
    CompactTool,
    SubagentTool,
    ToolRegistry,
)

SubagentEnvironment = (
    tuple[ToolRegistry, HookManager]
    | tuple[ToolRegistry, HookManager, ContextManager]
)

@dataclass(slots=True)
class AgentConfig:
    """Runtime settings for one agent instance."""

    model: str
    system_prompt: str
    max_tokens: int = 8000
    max_iterations: int = 50
    planning_backend: PlanningBackend = PlanningBackend.TODO


@dataclass(slots=True)
class AgentResult:
    """Summary of a completed agent run."""

    messages: list[Message]
    final_text: str
    stop_reason: str
    iterations: int
    usage: TokenTotals = field(default_factory=TokenTotals)


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
    prompt_runtime: PromptRuntime | None = None
    prompt_log: Callable[[str], None] | None = None
    recovery_runtime: RecoveryRuntime | None = None
    event_emitter: EventEmitter | None = None
    usage_tracker: UsageTracker | None = None
    cancellation: CancellationToken | None = None
    messages: list[Message] = field(default_factory=list)
    allow_subagents: bool = True
    subagent_max_iterations: int = 30
    subagent_registry_factory: Callable[[], ToolRegistry] | None = None
    subagent_environment_factory: Callable[[], SubagentEnvironment] | None = None
    subagent_log: Callable[[str], None] | None = None
    skill_catalog: str = ""
    memory_catalog: str = ""
    history_observer: HistoryObserver = field(default_factory=HistoryObserver)
    _compact_requested: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        task_tools = {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}
        if "todo_write" in self.tools and any(name in self.tools for name in task_tools):
            raise ValueError("TodoWrite and Task tools cannot be registered together")
        client_emitter = getattr(self.client, "event_emitter", None)
        if self.event_emitter is None:
            self.event_emitter = client_emitter or EventEmitter()
        elif hasattr(self.client, "event_emitter"):
            self.client.event_emitter = self.event_emitter

        client_tracker = getattr(self.client, "usage_tracker", None)
        if self.usage_tracker is None:
            self.usage_tracker = client_tracker or UsageTracker()
        if hasattr(self.client, "usage_tracker"):
            self.client.usage_tracker = self.usage_tracker
        if self.prompt_runtime is None:
            self.prompt_runtime = PromptRuntime(workspace=Path.cwd())
        if self.recovery_runtime is None:
            self.recovery_runtime = RecoveryRuntime()
        if self.allow_subagents and SUBAGENT_TOOL_NAME not in self.tools:
            self.tools.register(SubagentTool(spawn_fn=self._spawn_subagent))
        if COMPACT_TOOL_NAME not in self.tools:
            self.tools.register(CompactTool(compact_fn=self._request_manual_compact))
        if self.messages:
            self.history_observer.restore(
                generation=self.context.state.history_generation,
                last_sent=self.messages,
            )

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})

    def run(self, prompt: str | None = None) -> AgentResult:
        """Run until the model stops requesting tools or the iteration limit hits."""

        self._check_cancelled()
        assert self.event_emitter is not None
        assert self.usage_tracker is not None
        usage_before = self.usage_tracker.snapshot()
        self.event_emitter.emit(
            "agent.started",
            {
                "model": self.config.model,
                "max_iterations": self.config.max_iterations,
                "max_tokens": self.config.max_tokens,
                "is_subagent": not self.allow_subagents,
            },
        )
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
            assert self.recovery_runtime is not None
            recovery_state = self.recovery_runtime.create_state(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
            )#现在已经有了一个state了，后续多处会对此进行修改
            if prompt is not None:
                selected_memory_context = self._selected_memory_context(
                    model=recovery_state.current_model,
                    max_tokens=recovery_state.current_max_tokens,
                )
                if selected_memory_context:
                    assert self.prompt_runtime is not None
                    self.messages[-1] = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": self.prompt_runtime.memory_turn_context(
                                    selected_memory_context
                                ),
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }

            while iterations < self.config.max_iterations:
                self._check_cancelled()
                iterations += 1
                self.messages = self.context.prepare_before_model_call(
                    self.messages,
                    client=self._context_client(),
                    event_emitter=self.event_emitter,
                )
                reminder = self.hooks.trigger("BeforeModelCall", self.messages)
                if reminder:
                    self.add_user_message(str(reminder)) #如果有加入提醒该做todolist了

                tool_schemas = self.tools.schemas()
                prompt_assembly = self._assemble_prompt(
                    tool_schemas,
                    model=recovery_state.current_model,
                ) #组装system prompt
                self._log_prompt_assembly(prompt_assembly) #system prompt加入log
                history_observation = self.history_observer.observe(
                    self.messages,
                    generation=self.context.state.history_generation,
                    generation_reason=self.context.consume_generation_reason(),
                )
                self.context.state.history_generation = history_observation.generation
                history_payload = history_observation.to_event_payload()
                if history_observation.rewritten:
                    self.event_emitter.emit(
                        "history.rewritten",
                        history_payload,
                        iteration=iterations,
                    )
                self.event_emitter.emit(
                    "prompt.assembled",
                    {
                        "prompt_hash": prompt_assembly.prompt_hash,
                        "chars": len(prompt_assembly.system_prompt),
                        "assembly_reused": prompt_assembly.cache_hit,
                        **history_payload,
                        "fragments": [
                            {
                                "id": item.id,
                                "section": item.section,
                                "source": item.source,
                                "chars": item.chars,
                                "clipped": item.clipped,
                            }
                            for item in prompt_assembly.trace
                        ],
                    },
                    iteration=iterations,
                )
                compact_for_retry = self._compact_for_recovery_retry
                call_result = self.recovery_runtime.call_model(
                    lambda model, max_tokens, messages: self.client.create_message(
                        model=model,
                        system=prompt_assembly.system_prompt,
                        messages=messages,
                        tools=tool_schemas,
                        max_tokens=max_tokens,
                    ),
                    state=recovery_state,
                    messages=self.messages,
                    compact_fn=compact_for_retry,
                    event_emitter=self.event_emitter,
                    cancellation=self.cancellation,
                )
                self._check_cancelled()
                if call_result.messages is not None:
                    self.messages = call_result.messages
                if call_result.failed or call_result.response is None:
                    self._after_turn_memory()
                    result = self._make_result(
                        final_text=call_result.error,
                        stop_reason=f"recovery_failed:{call_result.reason}",
                        iterations=iterations,
                        usage_before=usage_before,
                    )
                    self._emit_agent_terminal("agent.failed", result)
                    run_trace.end(
                        outputs={
                            "final_text": result.final_text,
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "message_count": len(result.messages),
                        },
                        error=result.final_text or result.stop_reason,
                    )
                    return result

                self.context.reset_reactive_retries()
                response_recovery = self.recovery_runtime.handle_response(
                    call_result.response,
                    state=recovery_state,
                    messages=self.messages,
                    event_emitter=self.event_emitter,
                    cancellation=self.cancellation,
                )
                if response_recovery.messages is not None:
                    self.messages = response_recovery.messages
                if response_recovery.failed:
                    self._after_turn_memory()
                    result = self._make_result(
                        final_text=response_recovery.error,
                        stop_reason=f"recovery_failed:{response_recovery.reason}",
                        iterations=iterations,
                        usage_before=usage_before,
                    )
                    self._emit_agent_terminal("agent.failed", result)
                    run_trace.end(
                        outputs={
                            "final_text": result.final_text,
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "message_count": len(result.messages),
                        },
                        error=result.final_text or result.stop_reason,
                    )
                    return result
                if response_recovery.retry:
                    continue
                response = response_recovery.response or call_result.response
                last_stop_reason = response.stop_reason
                self.messages.append({"role": "assistant", "content": response.content})
                tool_uses = normalize_tool_uses(response.content)
                if response.stop_reason != "tool_use" or not tool_uses:
                    force_continue = self.hooks.trigger("Stop", self.messages)
                    if force_continue:
                        self.add_user_message(force_continue)
                        continue
                    self._after_turn_memory()
                    result = self._make_result(
                        final_text=extract_text(response.content),
                        stop_reason=response.stop_reason,
                        iterations=iterations,
                        usage_before=usage_before,
                    )
                    self._emit_agent_terminal("agent.completed", result)
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
                        client=self._context_client(),
                        reason="manual_compact",
                        event_emitter=self.event_emitter,
                    )

            self._after_turn_memory()
            result = self._make_result(
                final_text="",
                stop_reason=f"max_iterations:{last_stop_reason}",
                iterations=iterations,
                usage_before=usage_before,
            )
            self._emit_agent_terminal("agent.failed", result)
            run_trace.end(
                outputs={
                    "final_text": result.final_text,
                    "stop_reason": result.stop_reason,
                    "iterations": result.iterations,
                    "message_count": len(result.messages),
                },
                error=result.stop_reason,
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
            raw_outputs: list[str] = []
            executions: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                self.event_emitter.emit(
                    "tool.requested",
                    {
                        "tool_use_id": tool_use.id,
                        "name": tool_use.name,
                        "input": _public_tool_input(tool_use.name, tool_use.input),
                    },
                )
            for tool_use in tool_uses:
                self._check_cancelled()
                started_at = time.monotonic()
                blocked = self.hooks.trigger("PreToolUse", tool_use)
                if blocked:
                    output = str(blocked)
                    self.event_emitter.emit(
                        "tool.blocked",
                        {
                            "tool_use_id": tool_use.id,
                            "name": tool_use.name,
                            "input": _public_tool_input(tool_use.name, tool_use.input),
                            "reason": output,
                            "duration_ms": round(
                                (time.monotonic() - started_at) * 1000
                            ),
                        },
                    )
                else:
                    self.event_emitter.emit(
                        "tool.started",
                        {
                            "tool_use_id": tool_use.id,
                            "name": tool_use.name,
                            "input": _public_tool_input(tool_use.name, tool_use.input),
                        },
                    )
                    output = self.tools.execute(tool_use.name, tool_use.input)
                    self.context.record_tool_result(tool_use, output)
                    self.hooks.trigger("PostToolUse", tool_use, output)
                    failed = output.startswith(("Error:", "Unknown tool:"))
                self._check_cancelled()
                raw_outputs.append(output)
                executions.append(
                    {
                        "blocked": bool(blocked),
                        "failed": False if blocked else failed,
                        "duration_ms": round((time.monotonic() - started_at) * 1000),
                    }
                )

            finalized_outputs = self.context.finalize_tool_results(
                tool_uses, raw_outputs
            )
            results: list[dict[str, Any]] = []
            for tool_use, output, execution in zip(
                tool_uses, finalized_outputs, executions
            ):
                if not execution["blocked"]:
                    self.event_emitter.emit(
                        "tool.failed" if execution["failed"] else "tool.completed",
                        {
                            "tool_use_id": tool_use.id,
                            "name": tool_use.name,
                            "input": _public_tool_input(tool_use.name, tool_use.input),
                            "output": _public_tool_output(output),
                            "duration_ms": execution["duration_ms"],
                        },
                    )
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
            return "Error: subagent description is required."

        subagent_id = f"agent_{uuid4().hex}"
        child_emitter = self.event_emitter.child(agent_id=subagent_id)
        self._log_subagent_marker(f"[subagent enter] {task_description}")
        child_emitter.emit("subagent.started", {"description": task_description})
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
                    client=self._subagent_client(child_emitter),
                    tools=sub_tools,
                    config=AgentConfig(
                        model=self.config.model,
                        system_prompt=self.config.system_prompt,
                        max_tokens=self.config.max_tokens,
                        max_iterations=self.subagent_max_iterations,
                    ),
                    hooks=sub_hooks,
                    context=sub_context,
                    memory_manager=None,
                    prompt_runtime=self.prompt_runtime,
                    prompt_log=self.prompt_log,
                    recovery_runtime=self.recovery_runtime,
                    event_emitter=child_emitter,
                    usage_tracker=self.usage_tracker,
                    cancellation=self.cancellation,
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
                child_emitter.emit(
                    "subagent.completed",
                    {
                        "description": task_description,
                        "stop_reason": result.stop_reason,
                        "iterations": result.iterations,
                        "result": output,
                    },
                )
                return output
        except Exception as exc:
            child_emitter.emit(
                "subagent.failed",
                {
                    "description": task_description,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        finally:
            self._log_subagent_marker("[subagent exit] returned to parent agent")

    def _log_subagent_marker(self, message: str) -> None:
        if self.subagent_log is not None:
            self.subagent_log(message)

    def _subagent_client(self, emitter: EventEmitter) -> Any:
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            return fork(
                stream=False,
                on_text=None,
                event_emitter=emitter,
                usage_tracker=self.usage_tracker,
                call_kind="subagent",
            )
        return self.client

    def _subagent_tools(self) -> ToolRegistry:
        if self.subagent_registry_factory is not None:
            registry = self.subagent_registry_factory()
            if SUBAGENT_TOOL_NAME in registry:
                return registry.copy_without({SUBAGENT_TOOL_NAME})
            return registry
        return self.tools.copy_without({SUBAGENT_TOOL_NAME})

    def _subagent_environment(self) -> tuple[ToolRegistry, HookManager, ContextManager]:
        if self.subagent_environment_factory is not None:
            environment = self.subagent_environment_factory()
            registry, hooks = environment[0], environment[1]
            context = (
                environment[2]
                if len(environment) > 2
                else ContextManager(config=self.context.config)
            )
            if SUBAGENT_TOOL_NAME in registry:
                registry = registry.copy_without({SUBAGENT_TOOL_NAME})
            return registry, hooks, context
        return self._subagent_tools(), self.hooks, ContextManager(config=self.context.config)

    def _system_prompt(
        self,
        tool_schemas: list[dict[str, Any]],
        *,
        selected_memory_context: str = "",
    ) -> str:
        return self._assemble_prompt(
            tool_schemas,
            selected_memory_context=selected_memory_context,
        ).system_prompt

    def _assemble_prompt(
        self,
        tool_schemas: list[dict[str, Any]],
        *,
        selected_memory_context: str = "",
        model: str | None = None,
    ) -> PromptAssemblyResult:
        memory_catalog = self.memory_catalog
        if (
            self.memory_manager is not None
            and self.memory_manager.config.selection_mode == "llm"
            and not selected_memory_context
        ):
            memory_catalog = ""

        assert self.prompt_runtime is not None
        return self.prompt_runtime.assemble(
            mode=self._prompt_mode(),
            base_system_prompt=self.config.system_prompt,
            model=model or self.config.model,
            tool_schemas=tool_schemas,
            selected_memory_context=selected_memory_context,
            memory_catalog=memory_catalog,
            skill_catalog=self.skill_catalog,
        )

    def _prompt_mode(self) -> PromptMode:
        return PromptMode.NORMAL if self.allow_subagents else PromptMode.SUBAGENT

    def _log_prompt_assembly(self, assembly: PromptAssemblyResult) -> None:
        if self.prompt_log is None:
            return
        fragments = ", ".join(item.id for item in assembly.trace)
        cache = " assembly_reused" if assembly.cache_hit else ""
        self.prompt_log(
            f"[prompt] hash={assembly.prompt_hash} chars={len(assembly.system_prompt)} "
            f"fragments={len(assembly.trace)}{cache}: {fragments}"
        )

    def _compact_for_recovery_retry(
        self,
        messages: list[Message],
    ) -> list[Message] | None:
        compacted = self.context.reactive_compact(
            messages,
            client=self._context_client(),
            event_emitter=self.event_emitter,
        )
        if compacted is None:
            return None
        return compacted

    def _selected_memory_context(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:  #调用memory的manager去用llm选择memory
        if self.memory_manager is None:
            return ""
        try:
            return self.memory_manager.select_context(
                self.messages,
                client=self._memory_client(),
                model=model or self.config.model,
                max_tokens=max_tokens or self.config.max_tokens,
                event_emitter=self.event_emitter,
                cancellation=self.cancellation,
            )
        except Exception:
            self._check_cancelled()
            return ""

    def _memory_client(self) -> Any: #注入memory的client
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            return fork(
                stream=False,
                on_text=None,
                event_emitter=self.event_emitter,
                usage_tracker=self.usage_tracker,
                call_kind="memory_select",
            )
        return self.client

    def _context_client(self) -> Any:
        if self.context.config.summarization_api_key:
            return AnthropicModelClient(
                api_key=self.context.config.summarization_api_key,
                base_url=self.client.base_url,
                stream=False,
                on_text=None,
                event_emitter=self.event_emitter,
                usage_tracker=self.usage_tracker,
                call_kind="context_summary",
            )
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            return fork(
                stream=False,
                on_text=None,
                event_emitter=self.event_emitter,
                usage_tracker=self.usage_tracker,
                call_kind="context_summary",
            )
        return self.client

    def _after_turn_memory(self) -> None:
        if self.memory_manager is None:
            return
        try:
            memory_client = self.client
            fork = getattr(self.client, "fork", None)
            if callable(fork):
                memory_client = fork(
                    stream=False,
                    on_text=None,
                    event_emitter=self.event_emitter,
                    usage_tracker=self.usage_tracker,
                    call_kind="memory_maintenance",
                )
            self.memory_manager.after_turn(
                self.messages,
                client=memory_client,
                model=self.config.model,
                max_tokens=self.config.max_tokens,
            )
        except Exception:
            self._check_cancelled()
            return

    def _make_result(
        self,
        *,
        final_text: str,
        stop_reason: str,
        iterations: int,
        usage_before: TokenTotals,
    ) -> AgentResult:
        assert self.usage_tracker is not None
        return AgentResult(
            messages=self.messages,
            final_text=final_text,
            stop_reason=stop_reason,
            iterations=iterations,
            usage=self.usage_tracker.snapshot().delta(usage_before),
        )

    def _emit_agent_terminal(self, event_type: str, result: AgentResult) -> None:
        assert self.event_emitter is not None
        self.event_emitter.emit(
            event_type,
            {
                "stop_reason": result.stop_reason,
                "iterations": result.iterations,
                "message_count": len(result.messages),
                "final_text_chars": len(result.final_text),
                "usage": result.usage.to_dict(),
            },
        )

    def _check_cancelled(self) -> None:
        """Honor cooperative cancellation at safe execution boundaries."""

        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()

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


def _public_tool_input(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return bounded action metadata without exposing file bodies or patches."""

    hidden = {"content", "old_string", "new_string"}
    public: dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in hidden:
            public[f"{key}_chars"] = len(str(value))
            continue
        if key == "todos":
            public[key] = value
            continue
        public[key] = _clip_event_value(value)
    return public


def _public_tool_output(output: str) -> dict[str, Any]:
    """Expose a short preview; full tool output remains only in Agent context."""

    limit = 2_000
    return {
        "chars": len(output),
        "preview": output if len(output) <= limit else output[:limit] + "\n[truncated]",
        "truncated": len(output) > limit,
    }


def _clip_event_value(value: Any, limit: int = 2_000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value
