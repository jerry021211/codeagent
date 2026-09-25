"""Local coding-agent rules, implemented through the existing synchronous hooks."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from codeagent.events.redaction import redact_payload
from codeagent.hooks.manager import HookDecision, HookManager
from codeagent.messages import ToolUse, _field, extract_text
from codeagent.runtime.execution import ExecutionStopped, RunBudget
from codeagent.tools.base import ToolOutput


@dataclass(frozen=True, slots=True)
class LoopGuardConfig:
    window_size: int = 12
    repeat_failure_limit: int = 3
    parameter_error_limit: int = 2
    blocked_attempt_limit: int = 3
    empty_response_limit: int = 2
    max_model_calls: int = 80
    max_tool_calls: int = 200
    max_total_tokens: int = 300_000
    max_active_seconds: float = 1800.0
    tool_max_retries: int = 2
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        for name in ("window_size", "repeat_failure_limit", "parameter_error_limit",
                     "blocked_attempt_limit", "empty_response_limit", "max_model_calls", "max_tool_calls"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_total_tokens", "tool_max_retries"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("max_active_seconds", "retry_delay_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name == "max_active_seconds" and not value):
                raise ValueError(f"Invalid {name}")
        if self.repeat_failure_limit > self.window_size:
            raise ValueError("repeat_failure_limit must fit in window_size")


@dataclass(slots=True)
class GuardState:
    scope_id: str = ""
    rounds: int = 0
    response_seq: int = 0
    empty_responses: int = 0
    blocked_attempts: int = 0
    stop_reason: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    pending_processes: dict[str, int] = field(default_factory=dict)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _diagnostic(output: ToolOutput) -> str:
    # Keep a small diagnostic, never file bodies or whole command output.
    lines = [line for line in str(output).splitlines()
             if any(word in line for word in ("Error", "error", "FAILED", "failed", "错误", "超时", "exit code"))]
    return str(redact_payload(" | ".join(lines[:2])[:180])) or f"{output.outcome or output.status}, exit={output.exit_code}"


class LoopGuard:
    def __init__(self, config: LoopGuardConfig, *, tools: Callable[[], Any],
                 emit: Callable[..., Any], budget: RunBudget | None = None,
                 max_iterations: int = 50) -> None:
        self.config = config
        self.tools = tools
        self.emit = emit
        self.budget = budget or RunBudget(config)
        self.owns_budget = budget is None
        self.max_iterations = max_iterations
        self.state = GuardState(scope_id=uuid4().hex)
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def install(self, hooks: HookManager) -> None:
        hooks.register("BeforeModelCall", self.before_model, first=True)
        hooks.register("AfterModelCall", self.after_model, first=True)
        # Existing permission hooks decide first. A denied call never reaches us.
        hooks.register("PreToolUse", self.before_tool)
        hooks.register("PostToolUse", self.after_tool, first=True)

    def begin(self, scope_id: str) -> None:
        with self._lock:
            if scope_id != self.state.scope_id:
                self.state = GuardState(scope_id=scope_id)
                self._pending.clear()
                if self.owns_budget:
                    self.budget.reset()
            elif self.state.stop_reason.startswith("max_iterations:") and self.state.rounds < self.max_iterations:
                # An explicit SDK limit increase authorizes more rounds, not a counter reset.
                self.state.stop_reason = ""

    def before_model(self, messages: Any) -> None:
        self.budget.check()
        with self._lock:
            if self.state.stop_reason:
                raise ExecutionStopped(self.state.stop_reason)
            if self.state.rounds >= self.max_iterations:
                raise ExecutionStopped("budget_exceeded:iterations")
            self.state.rounds += 1

    def feedback(self) -> str:
        with self._lock:
            messages = [issue["message"] for issue in self.state.issues.values() if issue.get("warned")]
            if self.state.empty_responses:
                messages.append("上一轮未返回有效正文或工具调用。请给出答复或有效工具请求；再次为空将停止本次执行。")
            return "\n".join(messages[-self.config.window_size:])

    def feedback_sent(self) -> None:
        with self._lock:
            for issue in self.state.issues.values():
                if issue.get("warned") and issue.get("seen_at") is None:
                    issue["seen_at"] = self.state.response_seq + 1

    def after_model(self, response: Any) -> HookDecision | None:
        with self._lock:
            self.state.response_seq += 1
            # Before recovery, a truncated tool input may still be raw JSON text.
            # Inspect presence here; parsing and protocol repair belong to recovery.
            blocks = response.content if isinstance(response.content, list) else [response.content]
            has_tool = any(_field(block, "type") == "tool_use" for block in blocks)
            if has_tool or extract_text(response.content).strip():
                self.state.empty_responses = 0
                return None
            self.state.empty_responses += 1
            if self.state.empty_responses >= self.config.empty_response_limit:
                self.state.stop_reason = "loop_detected:empty_response"
                return HookDecision("finalize", reason=self.state.stop_reason)
            return HookDecision("retry", "请返回有效正文或工具调用。")

    def _issue(self, key: str, **values: Any) -> dict[str, Any]:
        if key not in self.state.issues:
            if len(self.state.issues) >= self.config.window_size:
                del self.state.issues[next(iter(self.state.issues))]
            self.state.issues[key] = {"count": 0, "warned": False, "seen_at": None, **values}
        return self.state.issues[key]

    def _warn(self, issue: dict[str, Any], message: str) -> str:
        if issue["warned"]:
            return ""
        issue.update(warned=True, message=message)
        self.emit("agent.loop_warning", {"message": message, "count": issue["count"], "hard": issue["hard"]})
        return "\n\n运行时纠正：" + message

    def _blocked(self, issue: dict[str, Any]) -> HookDecision:
        self.state.blocked_attempts += 1
        reason = "loop_detected:blocked_recovery_exhausted"
        message = ("本次调用未执行：" + issue["message"]
                   + " 请改正参数、读取相关实现或核实输入变化后再验证；其他诊断和修复工具仍可用。")
        if self.state.blocked_attempts >= self.config.blocked_attempt_limit:
            self.state.stop_reason = reason
            message += " 重复请求已阻断操作的次数达到上限，将结束本次执行。"
        return HookDecision("block", message, reason=self.state.stop_reason)

    def before_tool(self, tool: ToolUse) -> HookDecision | None:
        with self._lock:
            self.budget.check()
            if self.state.stop_reason:
                raise ExecutionStopped(self.state.stop_reason)
            registry = self.tools()
            invalid = registry.parameter_error(tool.name, tool.input)
            if invalid is not None:
                # Semantic invalid-edit categories cannot be bypassed by changing an unrelated field.
                signature = invalid.result_signature or _digest(tool.input)
                key = _digest(["parameter", tool.name, signature])
                issue = self._issue(key, action=key, input_state="", hard=True, name=tool.name)
                if issue["warned"] and issue["seen_at"] is not None:
                    return self._blocked(issue)
                issue["count"] += 1
                notice = ""
                if issue["count"] >= self.config.parameter_error_limit:
                    notice = self._warn(issue, f"{tool.name} 的同类无效参数已出现 {issue['count']} 次：{_diagnostic(invalid)} 请按工具 schema 修正参数。")
                return HookDecision("respond", str(invalid) + notice, outcome="parameter_error")

            inputs = registry.input_state(tool.name, tool.input)
            action = _digest([tool.name, tool.input, inputs.cwd])
            process_key = _digest([tool.name, tool.input.get("command"), inputs.cwd])
            if process_key in self.state.pending_processes:
                return self._blocked({"message": f"该命令的进程 {self.state.pending_processes[process_key]} 尚未确认结束，"
                                      "请先查询进程并处理本任务的未完成进程。"})
            for issue in self.state.issues.values():
                same_state = inputs.known and issue.get("input_state") == inputs.fingerprint
                if (issue.get("action") == action and issue["hard"]
                        and (same_state or issue.get("kind") == "parameter")
                        and issue["warned"] and issue["seen_at"] is not None):
                    return self._blocked(issue)
            self._pending[tool.id] = {
                "name": tool.name, "action": action, "input_state": inputs.fingerprint,
                "state_known": inputs.known, "cwd": inputs.cwd, "started": time.monotonic(),
                "process_key": process_key,
                "operation": f"{tool.name} [{action[:12]}]",
            }
            return None

    def after_tool(self, tool: ToolUse, output: ToolOutput) -> None:
        with self._lock:
            observation = self._pending.pop(tool.id, None)
            if observation is None or output.status == "blocked":
                return None
            output.guard_feedback = ""
            observation["duration_seconds"] = max(0.0, time.monotonic() - observation.pop("started"))
            observation.update(status=output.status, outcome=output.outcome or ("success" if output.status == "success" else "unknown"),
                               signature=output.result_signature or _digest([output.status, output.exit_code, str(output)]),
                               exit_code=output.exit_code, diagnostic=_diagnostic(output) if output.status != "success" else "",
                               changed_files=list(output.changed_files), process_id=output.process_id,
                               process_running=output.process_running)
            if output.state_known and output.input_state != observation["input_state"]:
                observation["state_known"] = False
            observation["known_failure"] = bool(observation["state_known"] and output.deterministic
                                                 and output.outcome == "diagnostic" and output.status == "error")
            self.state.recent.append(observation)
            self.state.recent[:] = self.state.recent[-self.config.window_size:]
            for path in output.changed_files:
                if path not in self.state.changed_files:
                    self.state.changed_files.append(path)
            self.state.changed_files[:] = self.state.changed_files[-50:]
            if tool.name == "bash" and output.process_running and output.process_id is not None:
                self.state.pending_processes[observation["process_key"]] = output.process_id
                if len(self.state.pending_processes) >= self.config.window_size:
                    self.state.stop_reason = "loop_detected:unresolved_processes"
            if output.status == "success":
                # Only contradicting evidence about this exact operation/state clears its issue.
                for key, issue in list(self.state.issues.items()):
                    if issue.get("action") == observation["action"] and issue.get("input_state") == observation["input_state"]:
                        del self.state.issues[key]
                return None
            if output.outcome == "parameter_error":
                key = _digest(["parameter", observation["action"], observation["signature"]])
                issue = self._issue(key, action=observation["action"], input_state="", hard=True,
                                    kind="parameter", name=tool.name)
                issue["count"] += 1
                if issue["count"] >= self.config.parameter_error_limit:
                    output.guard_feedback = self._warn(issue, f"{tool.name} 的同一无效参数已出现 {issue['count']} 次："
                                                       f"{observation['diagnostic']} 请修改参数后再执行。")
                return None
            if output.outcome in {"unknown_result", "running", "transient", "timeout"}:
                return None
            key = _digest([observation["action"], observation["input_state"], observation["signature"]])
            matching = [item for item in self.state.recent if item["status"] == "error" and
                        (item["action"], item["input_state"], item["signature"]) ==
                        (observation["action"], observation["input_state"], observation["signature"])]
            trusted_count = sum(bool(item.get("known_failure")) for item in matching)
            hard = observation["known_failure"] and trusted_count >= self.config.repeat_failure_limit
            issue = self._issue(key, action=observation["action"], input_state=observation["input_state"], hard=hard, name=tool.name)
            if issue["hard"] != hard:
                # A soft notice is not permission to block on newly acquired evidence.
                issue.update(hard=hard, warned=False, seen_at=None)
            issue["count"] = trusted_count if hard else len(matching)
            if issue["count"] >= self.config.repeat_failure_limit:
                certainty = "相同输入状态下" if hard else "输入状态或错误确定性未确认，仅提醒："
                notice = self._warn(issue, f"{certainty}{observation['operation']} 已重复得到相同失败 {issue['count']} 次："
                                    f"{observation['diagnostic']} 请读取相关实现、改变诊断方式或修改相关输入后再验证。")
                # Agent appends this once to the matching tool result; metadata remains raw.
                output.guard_feedback = notice
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"version": 1, "state": asdict(self.state), "budget": self.budget.snapshot()}

    def restore(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        if payload["version"] != 1:
            raise ValueError("Unsupported execution guard checkpoint version")
        with self._lock:
            self.state = GuardState(**payload["state"])
            self.state.recent[:] = self.state.recent[-self.config.window_size:]
            self.state.issues = dict(list(self.state.issues.items())[-self.config.window_size:])
            self.state.changed_files[:] = self.state.changed_files[-50:]
            self.state.pending_processes = dict(list(self.state.pending_processes.items())[-self.config.window_size:])
            self._pending.clear()
            if self.owns_budget:
                self.budget.restore(payload["budget"])

    def finish(self, reason: str) -> str:
        with self._lock:
            self.state.stop_reason = reason
            budget = self.budget.snapshot()
            lines = [f"本次执行已停止，未标记为完成。原因：{reason}。"]
            if self.state.changed_files:
                lines.append("已记录的文件改动（保留，未回滚）：" + "、".join(self.state.changed_files))
            else:
                lines.append("没有已确认的文件改动记录；命令或外部工具可能产生了未追踪的改动。")
            diagnostics = [f"{item['operation']}：{item['diagnostic'] or ('exit=' + str(item['exit_code']))}"
                           for item in self.state.recent if item["diagnostic"] or item["name"] == "bash"]
            if diagnostics:
                lines.append("近期实际执行的诊断：\n" + "\n".join(diagnostics[-5:]))
            lines.append(f"已用模型请求 {budget['model_calls']} 次，工具尝试 {budget['tool_calls']} 次，"
                         f"活跃执行 {budget['active_seconds']:.1f} 秒，已报告总 Token {budget['total_tokens']}。")
            if budget["unknown_usage_calls"]:
                lines.append(f"另有 {budget['unknown_usage_calls']} 次请求用量未知，未按零消耗认定。")
            lines.append("未完成项：原任务尚未确认完成；请依据上述诊断核实结果，再决定后续修复。")
            return "\n\n".join(lines)
