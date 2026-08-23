from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent.prompts import PromptMode, PromptRuntime
from codeagent.runtime_platform import RuntimePlatform, detect_runtime_platform
from codeagent.tools import BashTool


class RuntimePlatformTests(unittest.TestCase):
    def test_detects_windows_and_prefers_powershell(self) -> None:
        executables = {
            "powershell.exe": r"C:\Windows\System32\WindowsPowerShell\powershell.exe"
        }

        detected = detect_runtime_platform(
            os_name="nt",
            sys_platform="win32",
            which=executables.get,
        )

        self.assertEqual(detected.operating_system, "Windows")
        self.assertEqual(detected.shell_name, "Windows PowerShell")
        self.assertEqual(detected.shell_arguments[-1], "-Command")
        self.assertIn("PowerShell", detected.prompt_reminder())

    def test_detects_linux_and_prefers_bash(self) -> None:
        detected = detect_runtime_platform(
            os_name="posix",
            sys_platform="linux",
            which=lambda command: "/usr/bin/bash" if command == "bash" else None,
        )

        self.assertEqual(detected.operating_system, "Linux")
        self.assertEqual(detected.shell_name, "Bash")
        self.assertEqual(
            detected.command_argv("pwd"),
            ["/usr/bin/bash", "--noprofile", "--norc", "-c", "pwd"],
        )

    def test_bash_tool_invokes_the_detected_shell_explicitly(self) -> None:
        detected = RuntimePlatform(
            operating_system="Test OS",
            shell_name="Test Shell",
            shell_executable="test-shell",
            shell_arguments=("--command",),
            command_style="Use test syntax.",
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )

        with patch("codeagent.tools.bash.subprocess.run", return_value=completed) as run:
            tool = BashTool(runtime_platform=detected)
            result = tool.run("show-version")

        self.assertEqual(result, "ok")
        self.assertIn("Test OS", tool.definition.description)
        self.assertIn("Test Shell", tool.definition.description)
        run.assert_called_once_with(
            ["test-shell", "--command", "show-version"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path.cwd().resolve()),
            env=run.call_args.kwargs["env"],
        )

    def test_bash_tool_disables_tracing_in_child_processes_by_default(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_TRACING": "true",
                "LANGCHAIN_TRACING_V2": "true",
            },
            clear=True,
        ):
            with patch(
                "codeagent.tools.bash.subprocess.run", return_value=completed
            ) as run:
                BashTool().run("show-version")

            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["LANGSMITH_TRACING"], "false")
            self.assertEqual(child_env["LANGCHAIN_TRACING_V2"], "false")
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "true")
            self.assertEqual(os.environ["LANGCHAIN_TRACING_V2"], "true")

    def test_bash_tool_can_explicitly_propagate_tracing(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_TRACING": "true",
                "LANGCHAIN_TRACING_V2": "true",
                "CODEAGENT_TRACE_SUBPROCESSES": "true",
            },
            clear=True,
        ):
            with patch(
                "codeagent.tools.bash.subprocess.run", return_value=completed
            ) as run:
                BashTool().run("show-version")

            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["LANGSMITH_TRACING"], "true")
            self.assertEqual(child_env["LANGCHAIN_TRACING_V2"], "true")

    def test_prompt_includes_detected_platform_and_command_style(self) -> None:
        detected = RuntimePlatform(
            operating_system="Windows",
            shell_name="PowerShell",
            shell_executable="pwsh.exe",
            shell_arguments=("-Command",),
            command_style="Use PowerShell syntax, not POSIX syntax.",
        )
        runtime = PromptRuntime(workspace=Path.cwd(), runtime_platform=detected)

        result = runtime.assemble(
            mode=PromptMode.NORMAL,
            tool_schemas=[{"name": "bash"}],
        )
        system_prompt = result.system_prompt

        self.assertIn("Current operating system: Windows", system_prompt)
        self.assertIn("Command shell: PowerShell", system_prompt)
        self.assertIn("not POSIX syntax", system_prompt)


if __name__ == "__main__":
    unittest.main()
