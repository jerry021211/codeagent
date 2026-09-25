from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.runtime_platform import RuntimePlatform
from codeagent.tools.bash import BashTool
from codeagent.tools.registry import ToolRegistry
from codeagent.tools.workspace import WorkspaceGuard


class FakeProcess:
    def __init__(self, *, returncode=0, stdout="ok\n", stderr="", waits=()):
        self.pid = 12345
        self.args = []
        self.returncode = returncode
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.waits = iter(waits)
        self.wait_timeouts = []

    def start(self, args, **kwargs):
        self.args = args
        kwargs["stdout"].write(self.stdout_text)
        kwargs["stdout"].flush()
        kwargs["stderr"].write(self.stderr_text)
        kwargs["stderr"].flush()
        return self

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        action = next(self.waits, None)
        if action is not None:
            action()
        return self.returncode


def python_platform():
    # Exercise real process lifecycle without depending on shell quoting.
    return RuntimePlatform("Test", "Python", sys.executable, ("-c",), "Python")


class BashProcessGuardTests(unittest.TestCase):
    def test_registry_bind_runtime_propagates_stop_before_spawn(self):
        registry = ToolRegistry()
        registry.register(BashTool())
        registry.bind_runtime(lambda: None, lambda: 0)
        with patch("codeagent.tools.bash.subprocess.Popen") as start:
            with self.assertRaisesRegex(ExecutionStopped, "budget_exceeded:active_time"):
                registry.execute("bash", {"command": "pytest"})
        start.assert_not_called()

    def test_execution_stop_during_wait_cleans_up_and_propagates(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                proc = FakeProcess()
                tool = BashTool()
                registry = ToolRegistry()
                registry.register(tool)
                check = Mock(side_effect=[None, None, ExecutionStopped("budget_exceeded:tokens")])
                registry.bind_runtime(check, lambda: 10)
                with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
                    with patch.object(BashTool, "_stop_process", return_value=stopped) as stop:
                        with self.assertRaisesRegex(ExecutionStopped, "budget_exceeded:tokens") as caught:
                            registry.execute("bash", {"command": "pytest"})
                stop.assert_called_once_with(proc)
                self.assertEqual(caught.exception.process_id, proc.pid)
                self.assertEqual(caught.exception.process_running, not stopped)
                registry.bind_runtime(lambda: None, lambda: 10)
                with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                    result = registry.execute("bash", {"command": "pytest"})
                self.assertEqual(result.status, "success" if stopped else "blocked")
                self.assertEqual(start.call_count, int(stopped))

    def test_wait_respects_remaining_task_budget(self):
        proc = FakeProcess()
        tool = BashTool()
        tool.bind_runtime(cancellation_check=lambda: None, remaining_seconds=lambda: 0.01)
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
            self.assertEqual(tool.run("echo").status, "success")
        self.assertEqual(proc.wait_timeouts, [0.01])

    def test_ctrl_c_cleans_owned_process_and_propagates_without_replay(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                interrupt = KeyboardInterrupt()
                def interrupted_wait():
                    raise interrupt
                proc = FakeProcess(waits=[interrupted_wait])
                tool = BashTool()
                registry = ToolRegistry()
                registry.register(tool)
                with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                    with patch.object(BashTool, "_stop_process", return_value=stopped) as stop:
                        with self.assertRaises(KeyboardInterrupt) as caught:
                            registry.execute("bash", {"command": "pytest"})
                self.assertIs(caught.exception, interrupt)
                self.assertEqual(caught.exception.process_id, proc.pid)
                self.assertEqual(caught.exception.process_running, not stopped)
                stop.assert_called_once_with(proc)
                start.assert_called_once()
                with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                    result = registry.execute("bash", {"command": "pytest"})
                self.assertEqual(result.status, "success" if stopped else "blocked")
                self.assertEqual(start.call_count, int(stopped))

    def test_explicit_argv_cwd_environment_and_output_contract(self):
        platform = RuntimePlatform("Test OS", "Test Shell", "test-shell", ("--command",), "test")
        proc = FakeProcess(stdout="Error: quoted source\n", stderr="warning\n")
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true", "CODEAGENT_TRACE_SUBPROCESSES": "false"}):
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                tool = BashTool(runtime_platform=platform)
                result = tool.run('echo "a b"')
        self.assertEqual(start.call_args.args[0], ["test-shell", "--command", 'echo "a b"'])
        kwargs = start.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(tool.cwd))
        self.assertEqual(kwargs["env"]["LANGSMITH_TRACING"], "false")
        self.assertEqual(kwargs["env"]["LANGCHAIN_TRACING_V2"], "false")
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(result, "Error: quoted source\n\n[stderr]\nwarning")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.process_id, proc.pid)
        self.assertGreaterEqual(result.duration_seconds, 0)

    def test_explicit_tracing_opt_in_is_preserved(self):
        proc = FakeProcess()
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "CODEAGENT_TRACE_SUBPROCESSES": "true"}):
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                BashTool().run("test")
        self.assertEqual(start.call_args.kwargs["env"]["LANGSMITH_TRACING"], "true")

    def test_nonzero_exit_is_diagnostic_and_does_not_disable_shell_or_retry(self):
        proc = FakeProcess(returncode=1, stdout="FAILED test_one - AssertionError")
        tool = BashTool()
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            for _ in range(4):
                result = tool.run("pytest")
                self.assertEqual(result.status, "error")
                self.assertEqual(result.outcome, "diagnostic")
                self.assertFalse(result.deterministic)
                self.assertFalse(result.state_known)
                self.assertFalse(result.retryable)
                self.assertFalse(result.retry_safe)
                self.assertIn("[exit code: 1]", result)
            self.assertEqual(start.call_count, 4)

    def test_no_output_and_truncation_are_preserved(self):
        for raw, expected in [("", "(no output)"), ("a" * 16000, "truncated (16000 chars total)")]:
            proc = FakeProcess(stdout=raw)
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
                result = BashTool().run("echo")
            self.assertIn(expected, result)
            self.assertLess(len(result), 10000)

    def test_runner_duration_noise_does_not_change_failure_signature(self):
        for summary in ["Ran 1 test in {}s", "=== 1 failed in {}s ==="]:
            signatures = []
            for duration in ["0.12", "1.34"]:
                proc = FakeProcess(returncode=1, stdout="FAILED test_one - AssertionError\n" + summary.format(duration))
                with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
                    result = BashTool().run("test")
                self.assertIn(duration, result)
                signatures.append(result.result_signature)
            self.assertEqual(*signatures)

    def test_signature_retains_diagnostics_even_in_truncated_output(self):
        signatures = []
        for diagnostic in ["FAILED test_one: 1 != 2", "FAILED test_two: 1 != 2", "FAILED test_one: 3 != 2"]:
            proc = FakeProcess(returncode=1, stdout="a" * 7000 + diagnostic + "b" * 10000)
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
                result = BashTool().run("test")
            self.assertNotIn(diagnostic, result)
            signatures.append(result.result_signature)
        self.assertEqual(len(set(signatures)), 3)

    def timeout(self, tool, proc, *, stopped):
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            with patch("codeagent.tools.bash.time.monotonic", side_effect=[0, 2, 2]):
                with patch.object(BashTool, "_stop_process", return_value=stopped) as stop:
                    result = tool.run("pytest", timeout=1)
        start.assert_called_once()
        stop.assert_called_once_with(proc)
        return result

    def test_uncertain_timeout_blocks_exact_command_without_starting_it(self):
        tool = BashTool()
        proc = FakeProcess()
        result = self.timeout(tool, proc, stopped=False)
        self.assertEqual(result.outcome, "timeout")
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.process_running)
        self.assertIn("副作用", result)
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            blocked = tool.run("pytest", timeout=20)
            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(blocked.outcome, "process_pending")
            self.assertEqual(blocked.process_id, proc.pid)
            start.assert_not_called()
            self.assertEqual(tool.run("pytest -k other").outcome, "success")
            self.assertEqual(tool.run("PYTEST").outcome, "success")
            self.assertEqual(tool.run('pytest "test one"').outcome, "success")

    def test_confirmed_cleanup_allows_explicit_retry_but_never_replays(self):
        tool = BashTool()
        proc = FakeProcess()
        self.assertFalse(self.timeout(tool, proc, stopped=True).process_running)
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            self.assertEqual(tool.run("pytest").status, "success")
            start.assert_called_once()

    def test_same_command_in_different_cwd_and_other_instance_is_independent(self):
        proc = FakeProcess()
        with tempfile.TemporaryDirectory() as directory:
            tool = BashTool()
            self.timeout(tool, proc, stopped=False)
            tool._cwd = Path(directory)
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
                self.assertEqual(tool.run("pytest").status, "success")
                self.assertEqual(BashTool().run("pytest").status, "success")
                self.assertEqual(start.call_count, 2)

    def test_parallel_identical_request_is_blocked_before_spawn(self):
        tool = BashTool()
        nested = []
        proc = FakeProcess(waits=[lambda: nested.append(tool.run("pytest"))])
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            result = tool.run("pytest")
        self.assertEqual(result.status, "success")
        self.assertEqual(nested[0].status, "blocked")
        self.assertEqual(nested[0].process_id, proc.pid)
        start.assert_called_once()

    def test_cancel_before_start_propagates_through_registry(self):
        token = CancellationToken()
        token.cancel("stop now")
        registry = ToolRegistry()
        registry.register(BashTool(cancellation_check=token.raise_if_cancelled))
        with patch("codeagent.tools.bash.subprocess.Popen") as start:
            with self.assertRaisesRegex(CancelledError, "stop now"):
                registry.execute("bash", {"command": "pytest"})
        start.assert_not_called()

    def test_cancel_during_wait_cleans_up_and_preserves_unknown_process(self):
        token = CancellationToken()
        def cancel_wait():
            token.cancel("user cancelled")
            raise subprocess.TimeoutExpired("pytest", 0.1)
        proc = FakeProcess(waits=[cancel_wait])
        tool = BashTool(cancellation_check=token.raise_if_cancelled)
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start):
            with patch.object(BashTool, "_stop_process", return_value=False) as stop:
                with self.assertRaises(CancelledError) as caught:
                    tool.run("pytest")
        stop.assert_called_once_with(proc)
        self.assertEqual(caught.exception.process_id, proc.pid)
        self.assertTrue(caught.exception.process_running)
        tool.cancellation_check = CancellationToken().raise_if_cancelled
        with patch("codeagent.tools.bash.subprocess.Popen") as start:
            self.assertEqual(tool.run("pytest").status, "blocked")
        start.assert_not_called()

    def test_invalid_timeout_is_correctable_and_maximum_is_enforced(self):
        tool = BashTool(max_timeout_seconds=0.05)
        proc = FakeProcess()
        with patch("codeagent.tools.bash.subprocess.Popen", side_effect=proc.start) as start:
            for timeout in [0, -1, float("inf"), float("nan"), True, "5"]:
                self.assertEqual(tool.run("echo", timeout=timeout).outcome, "parameter_error")
            start.assert_not_called()
            with patch("codeagent.tools.bash.time.monotonic", side_effect=[0, 0, 0.001]):
                self.assertEqual(tool.run("echo", timeout=120).status, "success")
        self.assertTrue(all(0 < timeout <= 0.05 for timeout in proc.wait_timeouts))

    def test_spawn_failure_is_infrastructure_error_and_has_no_automatic_retry(self):
        for error, outcome in [(OSError("service unavailable"), "infrastructure_error"), (PermissionError("denied"), "permission_denied")]:
            with patch("codeagent.tools.bash.subprocess.Popen", side_effect=error) as start:
                result = BashTool().run("pytest")
            self.assertEqual(result.outcome, outcome)
            self.assertFalse(result.retryable)
            self.assertIsNone(result.process_id)
            start.assert_called_once()

    def test_security_checks_still_prevent_start(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = BashTool(workspace_guard=WorkspaceGuard(Path(directory)))
            with patch("codeagent.tools.bash.subprocess.Popen") as start:
                self.assertEqual(tool.run("rm -rf /").status, "blocked")
                self.assertEqual(tool.run("cd ..").status, "blocked")
            start.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows cleanup")
    def test_windows_cleanup_is_bounded_and_targets_only_owned_pid(self):
        proc = Mock(pid=321, returncode=None)
        proc.poll.return_value = None
        with patch("codeagent.tools.bash.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as cleanup:
            self.assertTrue(BashTool._stop_process(proc))
        self.assertEqual(cleanup.call_args.args[0], ["taskkill", "/PID", "321", "/T", "/F"])
        self.assertEqual(cleanup.call_args.kwargs["timeout"], 2)
        proc.wait.assert_called_once_with(timeout=1)
        proc.poll.return_value = 0
        with patch("codeagent.tools.bash.subprocess.run") as cleanup:
            self.assertFalse(BashTool._stop_process(proc))
        cleanup.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows cleanup")
    def test_cleanup_failure_does_not_claim_tree_is_dead(self):
        proc = Mock(pid=321)
        proc.poll.return_value = None
        with patch("codeagent.tools.bash.subprocess.run", side_effect=subprocess.TimeoutExpired("taskkill", 2)):
            self.assertFalse(BashTool._stop_process(proc))
        with patch("codeagent.tools.bash.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertFalse(BashTool._stop_process(proc))
        proc.kill.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX cleanup")
    def test_posix_cleanup_targets_owned_group_and_checks_remaining_members(self):
        proc = Mock(pid=321)
        with patch("codeagent.tools.bash.os.killpg", side_effect=[None, ProcessLookupError]) as killpg:
            self.assertTrue(BashTool._stop_process(proc))
        self.assertEqual([call.args[0] for call in killpg.call_args_list], [321, 321])
        with patch("codeagent.tools.bash.os.killpg", return_value=None):
            self.assertFalse(BashTool._stop_process(proc))

    def test_real_process_output_and_timeout(self):
        tool = BashTool(runtime_platform=python_platform())
        result = tool.run("import sys; print('ok'); print('details', file=sys.stderr); sys.exit(3)")
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.outcome, "diagnostic")
        self.assertIn("ok\n\n[stderr]\ndetails", result)
        started = time.monotonic()
        result = tool.run("import time; time.sleep(10)", timeout=0.2)
        self.assertEqual(result.outcome, "timeout")
        self.assertFalse(result.process_running)
        self.assertLess(time.monotonic() - started, 4)

    def test_real_cancellation_terminates_parent_and_child(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            leaked = Path(directory) / "leaked"
            child = (
                f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); "
                f"time.sleep(1.5); Path({str(leaked)!r}).write_text('leaked')"
            )
            parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            token = CancellationToken()
            def check():
                if ready.exists():
                    token.cancel("child started")
                token.raise_if_cancelled()
            tool = BashTool(runtime_platform=python_platform(), cancellation_check=check)
            started = time.monotonic()
            with self.assertRaisesRegex(CancelledError, "child started") as caught:
                tool.run(parent, timeout=5)
            self.assertIsInstance(caught.exception.process_id, int)
            self.assertLess(time.monotonic() - started, 4)
            time.sleep(1.7)
            self.assertFalse(leaked.exists(), "owned child survived cancellation")


if __name__ == "__main__":
    unittest.main()
