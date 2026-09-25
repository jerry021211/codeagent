"""Agent loop implementation.

The loop follows the harness pattern from the reference repository:
call the model, execute requested tools, append tool results, repeat.
"""

from __future__ import annotations

import time
import hashlib
import json
import math
from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextManager, HistoryObserver
from codeagent.context.budget import BoundModelClient
from codeagent.events import EventEmitter, TokenTotals, UsageTracker
from codeagent.hooks import HookDecision, HookManager
from codeagent.hooks.loop_guard import LoopGuard, LoopGuardConfig
from codeagent.memory import MemoryManager
from codeagent.messages import Message, ToolUse, extract_text, normalize_tool_uses, validate_tool_history
from codeagent.prompts import PromptAssemblyResult, PromptMode, PromptRuntime
from codeagent.planning import PlanningBackend
from codeagent.permissions import CliPermissionBroker, WaitingPermissionBroker
from codeagent.permissions.discuss import discuss_tool_guard
from codeagent.recovery import RecoveryRuntime
from codeagent.runtime import CancellationToken
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.execution import BudgetedClient, ExecutionStopped, RunBudget, is_execution_failure
from codeagent.tracing import trace_run
from codeagent.tools.base import ToolOutput, normalize_tool_output
from codeagent.tools import (
    COMPACT_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
    CompactTool,
    SubagentTool,
    ToolRegistry,
    LoadContextHistoryTool,
    LoadToolOutputTool,
    TodoStore,
    TodoWriteTool,
    tool_schema_hash,
)

SubagentEnvironment = tuple[ToolRegistry, HookManager, ContextManager]


@dataclass(slots=True)
class AgentConfig:
    """Runtime settings for one agent instance."""

    model: str
    max_tokens: int = 8000
    max_iterations: int = 50
    planning_backend: PlanningBackend = PlanningBackend.TODO
    loop_guard: LoopGuardConfig | None = field(default_factory=LoopGuardConfig)


@dataclass(slots=True)
class AgentResult:
    """Summary of a completed agent run."""

    messages: list[Message]
    final_text: str
    stop_reason: str
    iterations: int
    usage: TokenTotals = field(default_factory=TokenTotals)
    yielded: bool = False


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
    subagent_environment_factory: Callable[[], SubagentEnvironment] | None = None
    subagent_log: Callable[[str], None] | None = None
    skill_catalog: str = ""
    memory_catalog: str = ""
    history_observer: HistoryObserver = field(default_factory=HistoryObserver)
    boundary_callback: Callable[[str], None] | None = None
    execution_activity: ExecutionActivity | None = None
    permission_broker: CliPermissionBroker | WaitingPermissionBroker | None = None
    prompt_mode: PromptMode | None = None
    execution_budget: RunBudget | None = None
    _loop_guard: LoopGuard | None = field(default=None, init=False, repr=False)
    _compact_requested: bool = field(default=False, init=False)
    _tool_schema_changed: bool = field(default=False, init=False)
    _last_runtime_reminder: tuple | None = field(default=None, init=False, repr=False)
    _yield_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.context.cancellation_check = self._check_cancelled
        self.context.summary_credentials_scope = hashlib.sha256(json.dumps([
            getattr(self.client, "base_url", None),
            self.context.config.summarization_api_key or getattr(self.client, "api_key", None),
        ], default=str).encode("utf-8")).hexdigest()
        self.hooks = self.hooks.copy()
        self.hooks.register("PreToolUse", self._discuss_guard, first=True)
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
        if "load_tool_output" not in self.tools:
            self.tools.register(LoadToolOutputTool(self.context.config.tool_output_dir))
        if "load_context_history" not in self.tools:
            self.tools.register(LoadContextHistoryTool(self.context.config.transcript_dir))
        current_tool_hash = tool_schema_hash(self.tools.schemas())
        self._tool_schema_changed = bool(self.messages) and (
            self.context.state.tool_schema_hash != current_tool_hash
        )
        self.context.state.tool_schema_hash = current_tool_hash
        if self.config.loop_guard is not None and self._prompt_mode() in {
            PromptMode.NORMAL, PromptMode.DISCUSS, PromptMode.SUBAGENT,
        }:
            self._loop_guard = LoopGuard(
                self.config.loop_guard, tools=lambda: self.tools, emit=self.event_emitter.emit,
                budget=self.execution_budget, max_iterations=self.config.max_iterations,
            )
            self._loop_guard.install(self.hooks)
            if self.cancellation is None:
                self.cancellation = CancellationToken()
            self.set_execution_activity(self.execution_activity or ExecutionActivity(self.cancellation))
            self.tools.bind_runtime(self._check_execution, self._loop_guard.budget.remaining_seconds)
        if self.messages:
            self.history_observer.restore(
                generation=self.context.state.history_generation,
                last_sent=self.context.project_messages(self.messages),
            )

    def add_user_message(self, content: Any, *, source: str = "user") -> None:
        message = {"role": "user", "content": content}
        if source == "runtime":
            message["_context_source"] = "runtime"
        self.messages.append(message)

    @property
    def discuss_mode(self) -> bool:
        return self.prompt_mode is PromptMode.DISCUSS

    def set_discuss_mode(self, enabled: bool) -> None:
        """Switch an idle ordinary Agent; never use this to unlock a Team role."""
        if self.prompt_mode not in {None, PromptMode.NORMAL, PromptMode.DISCUSS}:
            raise ValueError("Discuss mode cannot replace a Team or subagent role")
        self.prompt_mode = PromptMode.DISCUSS if enabled else PromptMode.NORMAL

    def _discuss_guard(self, tool_use: ToolUse) -> str | None:
        return discuss_tool_guard(tool_use) if self.discuss_mode else None

    def set_execution_activity(self, activity: ExecutionActivity) -> None:
        """Bind one Team worker's monitor to main, retry and forked side calls."""
        self.execution_activity = activity
        if self._loop_guard is not None:
            activity.execution_budget = self._loop_guard.budget
        assert self.recovery_runtime is not None
        self.recovery_runtime.activity = activity
        if self.permission_broker is not None:
            self.permission_broker.execution_activity = activity
        if isinstance(self.client, AnthropicModelClient):
            self.client.activity = activity

    def run(self, prompt: str | None = None, *, execution_id: str | None = None) -> AgentResult:
        """Compatibility wrapper that runs a normal Agent to completion."""

        return self._guarded_run(prompt, allow_yield=False, execution_id=execution_id)

    def run_until_yield(self, prompt: str | None = None) -> AgentResult:
        """Run a Team Agent until completion or a requested safe-boundary yield."""

        return self._guarded_run(prompt, allow_yield=True)

    def export_execution_state(self) -> dict[str, Any]:
        return self._loop_guard.snapshot() if self._loop_guard is not None else {}

    def restore_execution_state(self, payload: dict[str, Any]) -> None:
        if self._loop_guard is not None:
            self._loop_guard.restore(payload)

    def _guarded_run(self, prompt: str | None, *, allow_yield: bool,
                     execution_id: str | None = None) -> AgentResult:
        guard = self._loop_guard
        if guard is None:
            return self._run(prompt, allow_yield=allow_yield)
        self._check_cancelled()
        if self.execution_activity is not None:
            self.execution_activity.cancellation = self.cancellation
        scope = execution_id or (guard.state.scope_id if prompt is None
                                 else self.event_emitter.context.run_id or uuid4().hex)
        guard.max_iterations = self.config.max_iterations
        guard.begin(scope)
        usage_before = self.usage_tracker.snapshot()
        start_rounds = guard.state.rounds
        with guard.budget.running():
            try:
                self._check_execution()
                return self._run(prompt, allow_yield=allow_yield)
            except ExecutionStopped as exc:
                self._check_cancelled()
                validate_tool_history(self.messages)
                result = self._make_result(
                    final_text=guard.finish(exc.reason), stop_reason=exc.reason,
                    iterations=guard.state.rounds - start_rounds, usage_before=usage_before,
                )
                self.event_emitter.emit(
                    "agent.budget_exceeded" if exc.reason.startswith(("budget_exceeded", "max_iterations"))
                    else "agent.loop_stopped", {"reason": exc.reason},
                )
                self._emit_agent_terminal("agent.failed", result)
                return result

    def request_yield(self, reason: str = "waiting") -> None:
        """Request a pause after the current model/tool boundary completes."""

        self._yield_reason = str(reason).strip() or "waiting"

    def _run(self, prompt: str | None, *, allow_yield: bool) -> AgentResult:
        """Run until the model stops requesting tools or the iteration limit hits."""

        self._check_cancelled()
        validate_tool_history(self.messages)
        # Team task assignments and answers arrive through durable mailbox messages,
        # with run(None). They continue the same task, not a new chat turn.
        if prompt is None and self.context.state.current_turn_start < 0:
            self.context.begin_turn(0)
        memory_start = len(self.messages)
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
            self.context.begin_turn(len(self.messages))
            self.hooks.trigger("UserPromptSubmit", prompt) #打印日志
            self._last_runtime_reminder = None
            self.context.record_user_prompt(prompt)
            # The system prompt alone does not record WHEN a mode changed.
            # Append the transition before the new request, preserving the
            # complete old history and tool pairs. Legacy checkpoints have no
            # mode marker, so announce their current mode once as well.
            current_mode = self._prompt_mode()
            if current_mode in {PromptMode.NORMAL, PromptMode.DISCUSS}:
                previous_mode = self.context.state.last_prompt_mode
                if self.messages and previous_mode != current_mode.value:
                    assert self.prompt_runtime is not None
                    self.add_user_message(self.prompt_runtime.mode_turn_context(current_mode), source="runtime")
                    self.event_emitter.emit("agent.mode.changed", {
                        "previous_mode": previous_mode,
                        "mode": current_mode.value,
                    })
                self.context.state.last_prompt_mode = current_mode.value
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
                self._check_execution()
                if self.boundary_callback is not None:
                    self.boundary_callback("before_model")
                if allow_yield and self._yield_reason is not None:
                    reason = self._yield_reason
                    self._yield_reason = None
                    result = self._make_result(
                        final_text="",
                        stop_reason=f"waiting:{reason}",
                        iterations=iterations,
                        usage_before=usage_before,
                        yielded=True,
                    )
                    self.event_emitter.emit(
                        "agent.waiting",
                        {
                            "reason": reason,
                            "iterations": iterations,
                            "message_count": len(self.messages),
                            "usage": result.usage.to_dict(),
                        },
                    )
                    run_trace.end(
                        outputs={
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "message_count": len(result.messages),
                            "yielded": True,
                        }
                    )
                    return result
                iterations += 1
                if self.discuss_mode:
                    reminder = None
                    if self._loop_guard is not None:
                        self._loop_guard.before_model(self.messages)
                else:
                    reminder = self.hooks.trigger("BeforeModelCall", self.messages)
                if isinstance(reminder, HookDecision):
                    if reminder.action == "finalize":
                        raise ExecutionStopped(reminder.reason)
                    reminder = reminder.message
                if reminder:
                    key = (self.context.state.summary_revision, self._prompt_mode(), str(reminder))
                    if key != self._last_runtime_reminder:
                        self.add_user_message("[运行时提醒：计划状态；不改变用户目标或权限]\n" + str(reminder), source="runtime")
                        self._last_runtime_reminder = key

                tool_schemas = self.tools.schemas()
                current_tool_hash = tool_schema_hash(tool_schemas)
                self._tool_schema_changed = self._tool_schema_changed or (
                    self.context.state.tool_schema_hash != current_tool_hash
                )
                prompt_assembly = self._assemble_prompt(tool_schemas) #组装system prompt
                self._log_prompt_assembly(prompt_assembly) #system prompt加入log
                compact_for_retry = self._compact_for_recovery_retry
                call_result = self.recovery_runtime.call_model(
                    lambda model, max_tokens, messages: self._create_message(
                        model=model,
                        system=prompt_assembly.system_prompt,
                        messages=messages,
                        tools=tool_schemas,
                        max_tokens=max_tokens,
                        prompt_assembly=prompt_assembly,
                        iteration=iterations,
                    ),
                    state=recovery_state,
                    messages=self.messages,
                    compact_fn=compact_for_retry,
                    event_emitter=self.event_emitter,
                    cancellation=self.cancellation,
                )
                self._tool_schema_changed = False
                self.context.state.tool_schema_hash = current_tool_hash
                self._check_cancelled()
                if call_result.messages is not None:
                    self.messages = call_result.messages
                if call_result.failed or call_result.response is None:
                    self._after_turn_memory(memory_start)
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

                response_decision = self.hooks.trigger("AfterModelCall", call_result.response)
                if isinstance(response_decision, HookDecision) and response_decision.action == "finalize":
                    raise ExecutionStopped(response_decision.reason)
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
                    self._after_turn_memory(memory_start)
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
                if isinstance(response_decision, HookDecision):
                    if response_decision.action == "retry":
                        self.add_user_message(response_decision.message, source="runtime")
                        continue
                last_stop_reason = response.stop_reason
                tool_uses = normalize_tool_uses(response.content)
                if tool_uses and (any(not item.id for item in tool_uses) or len({item.id for item in tool_uses}) != len(tool_uses)):
                    raise ValueError("模型返回了空或重复的工具调用 ID")
                self.messages.append({"role": "assistant", "content": response.content})
                if not tool_uses:
                    force_continue = self.hooks.trigger("Stop", self.messages)
                    if force_continue:
                        self.add_user_message(force_continue, source="runtime")
                        continue
                    self._after_turn_memory(memory_start)
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

                results: list[dict[str, Any]] = []
                self.messages.append({"role": "user", "content": results})
                self._execute_tools(tool_uses, results)
                if self._compact_requested:
                    self._compact_requested = False
                    self.context.force_compact(
                        self.messages,
                        client=self._context_client,
                        reason="manual_compact",
                        event_emitter=self.event_emitter,
                    )

            if self._loop_guard is not None:
                raise ExecutionStopped(f"max_iterations:{last_stop_reason}")
            self._after_turn_memory(memory_start)
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

    def _create_message(self, *, prompt_assembly: PromptAssemblyResult | None = None,
                        iteration: int | None = None, **kwargs: Any) -> Any:
        validate_tool_history(kwargs["messages"])
        if self._loop_guard is not None:
            feedback = self._loop_guard.feedback()
            if feedback:
                kwargs["system"] += "\n\n[本次执行的运行时纠正；不授予新权限]\n" + feedback
        kwargs["messages"] = self.context.prepare_before_model_call(
            kwargs["messages"], client=self._context_client, event_emitter=self.event_emitter,
            system=kwargs["system"], tools=kwargs["tools"], model=kwargs["model"],
            max_tokens=kwargs["max_tokens"],
        )
        self._check_cancelled()
        observation = self.history_observer.observe(
            kwargs["messages"], generation=self.context.state.history_generation,
            generation_reason=self.context.consume_generation_reason() or "request_projection",
        )
        self.context.state.history_generation = observation.generation
        if observation.rewritten:
            self.event_emitter.emit("history.rewritten", observation.to_event_payload(), iteration=iteration)
        payload = {
            **observation.to_event_payload(), "tool_schema_hash": tool_schema_hash(kwargs["tools"]),
            "chars": len(kwargs["system"]), "prompt_version": "single-agent-zh-v1",
        }
        if prompt_assembly is not None:
            payload.update(prompt_hash=prompt_assembly.prompt_hash, fragments=[{
                "id": item.id, "section": item.section, "source": item.source,
                "chars": item.chars, "clipped": item.clipped, "included": item.included,
                "original_chars": item.original_chars, "content_hash": item.content_hash,
                "dropped_reason": item.dropped_reason,
            } for item in prompt_assembly.trace])
        self.event_emitter.emit("prompt.assembled", payload, iteration=iteration)
        if self._loop_guard is not None:
            self._loop_guard.feedback_sent()
            response = self._loop_guard.budget.invoke(self.client, **kwargs)
        else:
            response = self.client.create_message(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None and usage.available:
            state = self.context.state
            state.latest_request_prompt_tokens = usage.prompt_input_tokens
            state.peak_request_prompt_tokens = max(state.peak_request_prompt_tokens, usage.prompt_input_tokens)
            state.accumulated_input_tokens += usage.prompt_input_tokens
            state.accumulated_output_tokens += usage.output_tokens
            state.cache_hit_tokens += usage.cache_read_input_tokens
            state.cache_miss_tokens += usage.input_tokens + usage.cache_creation_input_tokens
            state.latest_request_model = kwargs["model"]
            state.latest_request_estimated = usage.estimated
        return response

    def _execute_tools(self, tool_uses: list[ToolUse], results: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Keep every request paired even when dispatch, hooks or cancellation fail."""
        if results is None:
            results = []
        results.extend({
            "type": "tool_result", "tool_use_id": tool.id, "is_error": True,
            "content": "未执行：本轮在调用此工具前已停止。",
        } for tool in tool_uses)
        executions = [{"status": "cancelled", "emitted": False, "started": None} for _ in tool_uses]
        primary: BaseException | None = None
        try:
            for tool in tool_uses:
                self.event_emitter.emit("tool.requested", {
                    "tool_use_id": tool.id, "name": tool.name,
                    "input": _public_tool_input(tool.name, tool.input),
                })
            for tool, result, execution in zip(tool_uses, results, executions):
                self._check_execution()
                if self._loop_guard is not None:
                    self._loop_guard.budget.reserve("tool")
                execution["started"] = time.monotonic()
                blocked = self.hooks.trigger("PreToolUse", tool)
                self._check_cancelled()
                if blocked is not None:
                    if isinstance(blocked, HookDecision):
                        if blocked.action == "finalize":
                            raise ExecutionStopped(blocked.reason)
                        status = "error" if blocked.action == "respond" else "blocked"
                        output = ToolOutput(blocked.message, status=status, outcome=blocked.outcome)
                    else:
                        output = ToolOutput(str(blocked) or "Blocked: 已有 Hook 拒绝了本次调用；请改用允许的操作或说明阻塞。",
                                            status="blocked", outcome="permission")
                        status = "blocked"
                else:
                    self.event_emitter.emit("tool.started", {
                        "tool_use_id": tool.id, "name": tool.name,
                        "input": _public_tool_input(tool.name, tool.input),
                    })
                    activity = self.execution_activity
                    timeout = tool.input.get("timeout", 120)
                    if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
                        timeout = 120
                    if tool.name == SUBAGENT_TOOL_NAME and self._loop_guard is not None:
                        timeout = self._loop_guard.budget.remaining_seconds()
                    with (activity.operation(
                        "user_input" if tool.name == "ask_user" else "tool",
                        float("inf") if tool.name == "ask_user" else activity.clock() + timeout + 5,
                    ) if activity is not None else nullcontext()):
                        self._check_cancelled()
                        execution["status"] = "unknown"
                        result["content"] = "执行结果未知：调用过程中被中断，未收到完整结果。操作可能已产生副作用，请先核实实际状态，不要自动重复执行。"
                        output = self._execute_tool_with_retry(tool)
                        # Save the returned value before any optional bookkeeping.
                        result["content"] = str(output)
                        output = normalize_tool_output(output)
                        status = output.status
                        execution["status"] = status
                        if status == "success":
                            result.pop("is_error", None)
                        else:
                            result["is_error"] = True
                        execution["exit_code"] = output.exit_code
                        execution.update(outcome=output.outcome, process_id=output.process_id,
                                         process_running=output.process_running)
                        # Record returned facts before a deadline check can end this operation.
                        self.context.record_tool_result(tool, output)
                        self.hooks.trigger("PostToolUse", tool, output)
                result["content"] = str(output) + getattr(output, "guard_feedback", "")
                if status == "success":
                    result.pop("is_error", None)
                else:
                    result["is_error"] = True
                execution.update(status=status, exit_code=output.exit_code, outcome=output.outcome,
                                 process_id=output.process_id, process_running=output.process_running)
                self._emit_tool_outcome(tool, result, execution)
                self._check_cancelled()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            # No cancellation checks here: cleanup must finish before checkpointing.
            cleanup_errors: list[BaseException] = []
            try:
                outputs = self.context.finalize_tool_results(tool_uses, [item["content"] for item in results])
                if len(outputs) != len(results):
                    raise ValueError("工具结果格式化数量不匹配")
                for result, output in zip(results, outputs):
                    result["content"] = output
            except Exception as exc:
                cleanup_errors.append(exc)
                # Preserve pairing and truth even if an extension formatter fails.
                budget = max(1, self.context.config.tool_result_budget_chars // max(1, len(results)))
                limit = min(budget, self.context.config.single_tool_output_max_chars)
                for result in results:
                    content = result["content"]
                    if len(content) > limit:
                        marker = "[结果整理失败；预览截断，归档状态未知]\n"
                        result["content"] = (marker + content[:max(0, limit - len(marker))])[:limit]
            for tool, result, execution in zip(tool_uses, results, executions):
                if not execution["emitted"]:
                    try:
                        self._emit_tool_outcome(tool, result, execution)
                    except Exception as exc:
                        cleanup_errors.append(exc)
            if cleanup_errors:
                if primary is not None:
                    for exc in cleanup_errors:
                        primary.add_note(f"工具结果收尾异常：{type(exc).__name__}: {exc}")
                else:
                    raise cleanup_errors[0]
        return results

    def _execute_tool_with_retry(self, tool: ToolUse) -> ToolOutput:
        """Only an explicit safe transient result can request a bounded retry."""
        guard = self._loop_guard
        if guard is not None:
            self.tools.bind_runtime(self._check_execution, guard.budget.remaining_seconds)
        retries = guard.config.tool_max_retries if guard is not None else 0
        for attempt in range(retries + 1):
            self._check_execution()
            output = normalize_tool_output(self.tools.execute(tool.name, tool.input))
            if not (output.outcome == "transient" and output.retryable and output.retry_safe
                    and attempt < retries):
                return output
            delay = min(guard.config.retry_delay_seconds * 2 ** attempt,
                        guard.budget.remaining_seconds())
            self.event_emitter.emit("tool.retry_scheduled", {"name": tool.name, "attempt": attempt + 2,
                                                           "delay_seconds": delay})
            if self.cancellation is not None:
                self.cancellation.wait(delay)
            else:
                time.sleep(delay)
            self._check_execution()
            guard.budget.reserve("tool")
        raise AssertionError("unreachable")

    def _emit_tool_outcome(self, tool: ToolUse, result: dict[str, Any], execution: dict[str, Any]) -> None:
        status = execution["status"]
        event_type = {"success": "completed", "error": "failed", "blocked": "blocked", "unknown": "interrupted", "cancelled": "cancelled"}[status]
        execution["emitted"] = True
        started = execution["started"]
        self.event_emitter.emit(f"tool.{event_type}", {
            "tool_use_id": tool.id, "name": tool.name,
            "input": _public_tool_input(tool.name, tool.input),
            "output": _public_tool_output(result["content"]),
            "reason": result["content"] if status in {"blocked", "unknown", "cancelled"} else None,
            "duration_ms": round((time.monotonic() - started) * 1000) if started is not None else 0,
            "status": status, "exit_code": execution.get("exit_code"),
            "outcome": execution.get("outcome", ""),
            "process_id": execution.get("process_id"),
            "process_running": execution.get("process_running", False),
        })

    def _request_manual_compact(self) -> str:
        self._compact_requested = True
        return "[已请求压缩；将在下一次模型调用前生成摘要，当前尚未完成压缩。]"

    def _spawn_subagent(self, description: str) -> str:
        task_description = description.strip()
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
                subagent = self._create_subagent(child_emitter)
                result = subagent.run(task_description)
                output = result.final_text or (
                    "Subagent stopped without a final conclusion "
                    f"({result.stop_reason}, {result.iterations} iterations)."
                )
                if is_execution_failure(result.stop_reason):
                    error = f"Error: Subagent failed ({result.stop_reason}): {output}"
                    subagent_trace.end(
                        outputs={
                            "result": output,
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                        },
                        error=error,
                    )
                    child_emitter.emit(
                        "subagent.failed",
                        {
                            "description": task_description,
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "error": output,
                        },
                    )
                    return error
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

    def _create_subagent(self, emitter: EventEmitter) -> Agent:
        tools, hooks, context = self._subagent_environment()
        return Agent(
            client=self.client.fork(
                stream=True,
                on_text=None,
                event_emitter=emitter,
                usage_tracker=self.usage_tracker,
                call_kind="subagent",
            ),
            tools=tools,
            config=AgentConfig(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                max_iterations=self.subagent_max_iterations,
                loop_guard=self.config.loop_guard,
            ),
            hooks=hooks,
            context=context,
            prompt_runtime=self.prompt_runtime,
            prompt_log=self.prompt_log,
            recovery_runtime=RecoveryRuntime(self.recovery_runtime.config, log=self.recovery_runtime.log),
            execution_budget=self._loop_guard.budget if self._loop_guard is not None else None,
            permission_broker=self.permission_broker,
            event_emitter=emitter,
            usage_tracker=self.usage_tracker,
            cancellation=self.cancellation,
            allow_subagents=False,
            skill_catalog=self.skill_catalog,
            memory_catalog=self.memory_catalog,
        )

    def _subagent_environment(self) -> SubagentEnvironment:
        if self.subagent_environment_factory is not None:
            tools, hooks, context = self.subagent_environment_factory()
        else:
            identifier = uuid4().hex
            config = replace(
                self.context.config,
                transcript_dir=self.context.config.transcript_dir / "subagents" / identifier,
                tool_output_dir=self.context.config.tool_output_dir / "subagents" / identifier,
            )
            todo_store = TodoStore() if "todo_write" in self.tools else None
            tools = self.tools.copy_without({"todo_write"})
            if todo_store is not None:
                tools.register(TodoWriteTool(store=todo_store, on_change=self.subagent_log))
            # Built-in planning hooks retain counters and a store in closures.
            # Recreate them for this child while preserving permission/custom hooks.
            hooks = self.hooks.copy(exclude_owner=self._loop_guard, rebind=lambda handler: (
                handler.with_todo_store(todo_store)
                if todo_store is not None and callable(getattr(handler, "with_todo_store", None))
                else handler
            ))
            context = ContextManager(config=config, todo_store=todo_store)
        # Parent-bound callbacks and private readers must never leak into the child.
        tools = tools.copy_without({SUBAGENT_TOOL_NAME, COMPACT_TOOL_NAME,
                                    "load_tool_output", "load_context_history"})
        tools.register(LoadToolOutputTool(context.config.tool_output_dir))
        tools.register(LoadContextHistoryTool(context.config.transcript_dir))
        return tools, hooks, context

    def _assemble_prompt(
        self,
        tool_schemas: list[dict[str, Any]],
        *,
        selected_memory_context: str = "",
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
            tool_schemas=tool_schemas,
            selected_memory_context=selected_memory_context,
            memory_catalog=memory_catalog,
            skill_catalog=self.skill_catalog,
            tool_schema_changed=self._tool_schema_changed,
        )

    def _prompt_mode(self) -> PromptMode:
        if self.prompt_mode is not None:
            return self.prompt_mode
        return PromptMode.NORMAL if self.allow_subagents else PromptMode.SUBAGENT

    def _log_prompt_assembly(self, assembly: PromptAssemblyResult) -> None:
        if self.prompt_log is None:
            return
        fragments = ", ".join(item.id for item in assembly.trace)
        self.prompt_log(
            f"[prompt] hash={assembly.prompt_hash} chars={len(assembly.system_prompt)} "
            f"fragments={len(assembly.trace)}: {fragments}"
        )

    def _compact_for_recovery_retry(
        self,
        messages: list[Message],
    ) -> list[Message] | None:
        compacted = self.context.reactive_compact(
            messages,
            client=self._context_client,
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
                client=self._side_query_client("memory_select"),
                model=model or self.config.model,
                max_tokens=max_tokens or self.config.max_tokens,
                event_emitter=self.event_emitter,
                cancellation=self.cancellation,
            )
        except Exception:
            self._check_cancelled()
            return ""

    def _side_query_client(self, call_kind: str) -> Any:
        fork = getattr(self.client, "fork", None)
        if callable(fork):
            client = fork(
                stream=False,
                on_text=None,
                event_emitter=self.event_emitter,
                usage_tracker=self.usage_tracker,
                call_kind=call_kind,
            )
        else:
            client = self.client
        if self._loop_guard is not None:
            client = BudgetedClient(client, self._loop_guard.budget)
        if call_kind == "context_summary":
            return client  # The summary manager uses its independent input/window budget.
        return BoundModelClient(client, max_request_chars=self.context.config.max_request_chars,
                                window_resolver=self.context.config.window_for_model)

    def _context_client(self) -> Any:
        if self.context.config.summarization_api_key:
            client = AnthropicModelClient(
                api_key=self.context.config.summarization_api_key,
                base_url=self.client.base_url,
                stream=False,
                on_text=None,
                event_emitter=self.event_emitter,
                usage_tracker=self.usage_tracker,
                call_kind="context_summary",
                activity=self.execution_activity,
                request_timeout=self.context.config.summary_timeout_seconds,
            )
            return BudgetedClient(client, self._loop_guard.budget) if self._loop_guard is not None else client
        client = self._side_query_client("context_summary")
        raw_client = client.client if isinstance(client, BudgetedClient) else client
        if isinstance(raw_client, AnthropicModelClient):
            raw_client.request_timeout = self.context.config.summary_timeout_seconds
        return client

    def _after_turn_memory(self, start_index: int = 0) -> None:
        if self.memory_manager is None or self.discuss_mode:
            return
        try:
            memory_client = self._side_query_client("memory_maintenance")
            current_run_messages = (
                self.messages[start_index:] if start_index <= len(self.messages) else []
            )
            self.memory_manager.after_turn(
                current_run_messages,
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
        yielded: bool = False,
    ) -> AgentResult:
        assert self.usage_tracker is not None
        return AgentResult(
            messages=self.messages,
            final_text=final_text,
            stop_reason=stop_reason,
            iterations=iterations,
            usage=self.usage_tracker.snapshot().delta(usage_before),
            yielded=yielded,
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

    def _check_execution(self) -> None:
        self._check_cancelled()
        if self._loop_guard is not None:
            self._loop_guard.budget.check()
            if self._loop_guard.state.stop_reason:
                raise ExecutionStopped(self._loop_guard.state.stop_reason)

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
