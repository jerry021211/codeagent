"""Permission checks for tool execution."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


AskUser = Callable[[str, dict[str, Any], str], bool]


def ask_user(tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
    print()
    print(f"Permission required: {reason}")
    print(f"Tool: {tool_name}({tool_input})")
    choice = input("Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


@dataclass(slots=True)
class PermissionPolicy:
    """Three-gate permission policy inspired by the reference s03 example."""

    workspace: Path = field(default_factory=Path.cwd)
    ask: AskUser = ask_user

    hard_deny_patterns: tuple[str, ...] = (
        # POSIX shell: destructive system operations.
        "rm -rf /",
        "rm -rf ~",
        "rm -rf $home",
        "sudo",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        "> /dev/sda",
        "chmod 777 /",
        "chown -r ",
        # Windows cmd / PowerShell: destructive system operations.
        "format ",
        "diskpart",
        "bcdedit",
        "reg delete",
        "del /s /q c:\\",
        "erase /s /q c:\\",
        "rmdir /s /q c:\\",
        "rd /s /q c:\\",
        "remove-item -recurse -force c:\\",
        "remove-item -r -force c:\\",
    )
    destructive_command_patterns: tuple[str, ...] = (
        # POSIX shell.
        "rm ",
        "rmdir ",
        "mv ",
        "> /etc/",
        "chmod ",
        "chown ",
        "chmod 777",
        # Windows cmd.
        "del ",
        "erase ",
        "rmdir ",
        "rd ",
        "move ",
        "ren ",
        "rename ",
        # PowerShell.
        "remove-item",
        "move-item",
        "rename-item",
        "set-acl",
        "clear-content",
        # Cross-platform data destruction utilities.
        "truncate ",
        "shred ",
        "wipefs ",
    )
    write_tools: tuple[str, ...] = ("write_file", "edit_file")

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        if tool_name == "bash":
            command = str(tool_input.get("command", ""))
            reason = self._hard_deny_reason(command)
            if reason:
                return PermissionDecision(False, reason)

            reason = self._destructive_command_reason(command)
            if reason and not self.ask(tool_name, tool_input, reason):
                return PermissionDecision(False, "Permission denied by user")

        if tool_name in self.write_tools:
            reason = self._workspace_write_reason(tool_input)
            if reason and not self.ask(tool_name, tool_input, reason):
                return PermissionDecision(False, "Permission denied by user")

        return PermissionDecision(True)

    def _hard_deny_reason(self, command: str) -> str:
        normalized = command.casefold()
        for pattern in self.hard_deny_patterns:
            if pattern.casefold() in normalized:
                return f"Blocked: '{pattern}' is on the deny list"
        return ""

    def _destructive_command_reason(self, command: str) -> str:
        normalized = command.casefold()
        for pattern in self.destructive_command_patterns:
            if pattern.casefold() in normalized:
                return "Potentially destructive command"
        return ""

    def _workspace_write_reason(self, tool_input: dict[str, Any]) -> str:
        raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not raw_path:
            return ""

        target = Path(os.path.expanduser(str(raw_path)))
        if not target.is_absolute():
            target = self.workspace / target
        try:
            target.resolve().relative_to(self.workspace.resolve())
        except ValueError:
            return "Writing outside workspace"
        return ""
