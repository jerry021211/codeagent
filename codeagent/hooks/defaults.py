"""Default hooks used by the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from codeagent.hooks.manager import HookManager
from codeagent.messages import ToolUse
from codeagent.permissions import PermissionPolicy
from codeagent.tools import (
    TodoStore,
    create_todo_final_status_hook,
    create_todo_reminder_hook,
)

Logger = Callable[[str], None]


def create_default_hooks(
    *,
    permission_policy: PermissionPolicy | None = None,
    workspace: Path | None = None,
    log: Logger = print,
    large_output_limit: int = 100_000,
    todo_store: TodoStore | None = None,
    todo_reminder_interval: int = 3,
) -> HookManager:
    hooks = HookManager()
    policy = permission_policy or PermissionPolicy(workspace=workspace or Path.cwd())
    store = todo_store or TodoStore()

    hooks.register("UserPromptSubmit", _user_prompt_log(workspace or Path.cwd(), log))
    hooks.register(
        "BeforeModelCall",
        create_todo_reminder_hook(store, interval=todo_reminder_interval),
    )
    hooks.register("PreToolUse", _permission_hook(policy))
    hooks.register("PreToolUse", _tool_log(log))
    hooks.register("PostToolUse", _large_output_hook(large_output_limit, log))
    hooks.register("Stop", create_todo_final_status_hook(store, log))
    hooks.register("Stop", _summary_hook(log))
    return hooks


def _user_prompt_log(workspace: Path, log: Logger):
    def hook(prompt: str) -> None:
        log(f"[hook] UserPromptSubmit: working in {workspace}")
        return None

    return hook


def _permission_hook(policy: PermissionPolicy):
    def hook(tool_use: ToolUse) -> str | None:
        decision = policy.check(tool_use.name, tool_use.input)
        if decision.allowed:
            return None
        return decision.reason or "Permission denied."

    return hook


def _tool_log(log: Logger):
    def hook(tool_use: ToolUse) -> None:
        args_preview = str(list(tool_use.input.values())[:2])[:80]
        log(f"[hook] PreToolUse: {tool_use.name}({args_preview})")
        return None

    return hook


def _large_output_hook(limit: int, log: Logger):
    def hook(tool_use: ToolUse, output: Any) -> None:
        output_len = len(str(output))
        if output_len > limit:
            log(f"[hook] PostToolUse: large output from {tool_use.name}: {output_len} chars")
        return None

    return hook


def _summary_hook(log: Logger):
    def hook(messages: list[dict[str, Any]]) -> None:
        tool_count = 0
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            tool_count += sum(
                1
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
        log(f"[hook] Stop: session used {tool_count} tool calls")
        return None

    return hook
