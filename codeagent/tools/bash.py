"""Shell command execution tool."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from codeagent.tools.base import ToolDefinition

_cwd: str | None = None

_DANGEROUS_PATTERNS = [
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    (r"\brm\s+(-\w*)?-rf\s", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
    (r"\bwget\b.*\|\s*(sudo\s+)?bash", "pipe wget to bash"),
]


@dataclass(frozen=True, slots=True)
class BashTool:
    """Execute shell commands with basic safety checks and output truncation."""

    definition: ToolDefinition = ToolDefinition(
        name="bash",
        description=(
            "Execute a shell command. Returns stdout, stderr, and exit code. "
            "Use this for running tests, installing packages, git operations, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120)",
                },
            },
            "required": ["command"],
        },
    )

    def run(self, command: str, timeout: int = 120) -> str:
        global _cwd

        warning = _check_dangerous(command)
        if warning:
            return (
                f"Blocked: {warning}\n"
                f"Command: {command}\n"
                "If intentional, modify the command to be more specific."
            )

        cwd = _cwd or os.getcwd()

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            if proc.returncode == 0:
                _update_cwd(command, cwd)

            output = proc.stdout
            if proc.stderr:
                output += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"
            if len(output) > 15_000:
                output = (
                    output[:6000]
                    + f"\n\n... truncated ({len(output)} chars total) ...\n\n"
                    + output[-3000:]
                )
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except Exception as exc:
            return f"Error running command: {exc}"


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def _update_cwd(command: str, current_cwd: str) -> None:
    global _cwd

    parts = command.split("&&")
    for part in parts:
        part = part.strip()
        if not part.startswith("cd "):
            continue
        target = part[3:].strip().strip("'\"")
        if not target:
            continue
        new_dir = os.path.normpath(os.path.join(current_cwd, os.path.expanduser(target)))
        if os.path.isdir(new_dir):
            _cwd = new_dir
