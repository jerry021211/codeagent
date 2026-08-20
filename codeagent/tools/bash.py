"""Shell command execution tool."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codeagent.runtime_platform import RuntimePlatform, current_runtime_platform
from codeagent.tools.base import ToolDefinition
from codeagent.tools.workspace import WorkspaceGuard, WorkspaceViolationError

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


@dataclass(slots=True)
class BashTool:
    """Execute commands in the detected native shell with basic safety checks."""

    runtime_platform: RuntimePlatform = field(default_factory=current_runtime_platform)
    workspace_guard: WorkspaceGuard | None = None
    definition: ToolDefinition = field(init=False)
    _cwd: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.definition = ToolDefinition(
            name="bash",
            description=(
                f"Execute a command on {self.runtime_platform.operating_system} using "
                f"{self.runtime_platform.shell_name}. Returns stdout, stderr, and exit "
                "code. Use syntax appropriate for that shell."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            f"The {self.runtime_platform.shell_name} command to run"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120)",
                    },
                },
                "required": ["command"],
            },
        )
        self._cwd = (
            self.workspace_guard.root
            if self.workspace_guard is not None
            else Path.cwd().resolve()
        )

    @property
    def cwd(self) -> Path:
        """Return this tool instance's current working directory."""

        return self._cwd or Path.cwd().resolve()

    def run(self, command: str, timeout: int = 120) -> str:
        warning = _check_dangerous(command)
        if warning:
            return (
                f"Blocked: {warning}\n"
                f"Command: {command}\n"
                "If intentional, modify the command to be more specific."
            )

        cwd = self.cwd

        if self.workspace_guard is not None:
            try:
                self._validate_directory_changes(command, cwd)
            except WorkspaceViolationError as exc:
                return f"Blocked: {exc}"

        try:
            proc = subprocess.run(
                self.runtime_platform.command_argv(command),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
            )

            if proc.returncode == 0:
                self._update_cwd(command, cwd)

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

    def _validate_directory_changes(self, command: str, current_cwd: Path) -> None:
        """Reject explicit ``cd`` targets outside a guarded workspace pre-run."""

        assert self.workspace_guard is not None
        for target in _cd_targets(command):
            current_cwd = self.workspace_guard.resolve(target, base=current_cwd)

    def _update_cwd(self, command: str, current_cwd: Path) -> None:
        """Apply successful shell ``cd`` operations to this instance only."""

        for target in _cd_targets(command):
            if self.workspace_guard is not None:
                new_dir = self.workspace_guard.resolve(target, base=current_cwd)
            else:
                expanded = Path(os.path.expanduser(target))
                new_dir = (
                    expanded.resolve()
                    if expanded.is_absolute()
                    else (current_cwd / expanded).resolve()
                )
            if new_dir.is_dir():
                self._cwd = new_dir
                current_cwd = new_dir


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def _cd_targets(command: str) -> list[str]:
    """Extract simple persistent ``cd`` operations from a compound command."""

    targets: list[str] = []
    parts = re.split(r"(?:&&|;)", command)
    for part in parts:
        part = part.strip()
        match = re.match(r"^(?:cd|chdir)\s+(?:/d\s+)?(.+?)\s*$", part, re.IGNORECASE)
        if match is None:
            continue
        target = match.group(1).strip().strip("'\"")
        if not target:
            continue
        targets.append(target)
    return targets
