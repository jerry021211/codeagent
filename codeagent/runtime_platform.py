"""Operating-system and command-shell detection for the agent runtime."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable


WhichCommand = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class RuntimePlatform:
    """The host operating system and the shell used to execute commands."""

    operating_system: str
    shell_name: str
    shell_executable: str
    shell_arguments: tuple[str, ...]
    command_style: str

    def command_argv(self, command: str) -> list[str]:
        """Build an explicit subprocess argv for one shell command."""

        return [self.shell_executable, *self.shell_arguments, command]

    def prompt_reminder(self) -> str:
        """Describe the detected platform in terms useful to the model."""

        return (
            f"当前操作系统：{self.operating_system}。"
            f"命令 Shell：{self.shell_name} ({self.shell_executable})。"
            f"{self.command_style}"
        )


def detect_runtime_platform(
    *,
    os_name: str | None = None,
    sys_platform: str | None = None,
    which: WhichCommand = shutil.which,
) -> RuntimePlatform:
    """Detect the host and choose a native command shell.

    Optional arguments make all platform branches deterministic in tests.
    """

    detected_os_name = os.name if os_name is None else os_name
    detected_sys_platform = sys.platform if sys_platform is None else sys_platform

    if detected_os_name == "nt" or detected_sys_platform.startswith("win"):
        for candidate, display_name in (
            ("pwsh", "PowerShell"),
            ("powershell.exe", "Windows PowerShell"),
            ("powershell", "Windows PowerShell"),
        ):
            executable = which(candidate)
            if executable:
                return RuntimePlatform(
                    operating_system="Windows",
                    shell_name=display_name,
                    shell_executable=executable,
                    shell_arguments=(
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                    ),
                    command_style=(
                        "使用 PowerShell 命令、分隔符和 Windows 路径。"
                        "先确认所需程序可用，再使用仅适用于 POSIX 的命令语法。"
                    ),
                )

        return RuntimePlatform(
            operating_system="Windows",
            shell_name="Command Prompt",
            shell_executable=os.environ.get("COMSPEC", "cmd.exe"),
            shell_arguments=("/d", "/s", "/c"),
            command_style=(
                "使用 cmd.exe 命令、分隔符和 Windows 路径，不使用 PowerShell 或仅适用于 POSIX 的语法。"
            ),
        )

    operating_system = {
        "darwin": "macOS",
        "linux": "Linux",
    }.get(detected_sys_platform, "Unix")
    bash = which("bash")
    if bash:
        return RuntimePlatform(
            operating_system=operating_system,
            shell_name="Bash",
            shell_executable=bash,
            shell_arguments=("--noprofile", "--norc", "-c"),
            command_style=(
                "使用 Bash/POSIX 命令、分隔符和路径，不使用 PowerShell 命令或 cmd.exe 内置命令。"
            ),
        )

    shell = which("sh") or "/bin/sh"
    return RuntimePlatform(
        operating_system=operating_system,
        shell_name="POSIX sh",
        shell_executable=shell,
        shell_arguments=("-c",),
        command_style=(
            "使用可移植的 POSIX sh 命令、分隔符和路径，不使用 Bash 专属语法、PowerShell 或 cmd.exe 内置命令。"
        ),
    )


@lru_cache(maxsize=1)
def current_runtime_platform() -> RuntimePlatform:
    """Return the process-wide platform detection result."""

    return detect_runtime_platform()
