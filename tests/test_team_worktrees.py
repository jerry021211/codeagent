from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from codeagent import Agent, AgentConfig, ModelResponse
from codeagent.runtime import TeamSupervisor
from codeagent.teams import (
    AgentSessionState,
    AttemptPlanStatus,
    CandidateStatus,
    CandidateService,
    ManualIntegrationVerifier,
    TaskAttemptState,
    TeamToolExecutionGate,
    create_teammate_tools,
)
from codeagent.tools import (
    BashTool,
    ToolDefinition,
    ToolRegistry,
    WorkspaceGuard,
    WriteFileTool,
)
from codeagent.web.storage import (
    InvalidStateTransitionError,
    SQLiteRepository,
    StorageConflictError,
)
from codeagent.worktrees import (
    DirtyWorkspaceConfirmationRequired,
    WorktreeError,
    WorktreeManager,
)
from codeagent.teams.tools import TeamQuestionTool, TeamAnswerQuestionTool


class TeamWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "CodeAgent Tests")
        (self.source / "allowed").mkdir()
        (self.source / "allowed" / "base.txt").write_text("base\n", encoding="utf-8")
        (self.source / "outside.txt").write_text("outside\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base_commit = self._git("rev-parse", "HEAD")

        self.repository = SQLiteRepository(
            self.root / "state.db", recover_incomplete=False
        )
        conversation = self.repository.create_conversation(
            title="Worktrees", workspace=str(self.source)
        )
        run = self.repository.create_run(conversation.id, status="running")
        assert conversation.active_task_list_id is not None
        self.task_list_id = conversation.active_task_list_id
        self.team = self.repository.create_team_run(
            conversation_id=conversation.id,
            root_run_id=run.id,
            task_list_id=self.task_list_id,
            base_commit=self.base_commit,
            team_run_id="team_worktree",
            lead_agent_id="agent_lead",
            max_teammates=3,
        )
        plan = self.repository.create_team_plan_revision(
            self.team.id,
            plan={"base_commit": self.base_commit, "tasks": ["code"]},
            created_by="agent_lead",
            command_id="create-plan",
        )
        self.repository.submit_team_plan_revision(
            self.team.id, plan.revision, command_id="submit-plan"
        )
        self.repository.decide_team_plan_revision(
            self.team.id,
            plan.revision,
            decision="approve",
            decided_by="user",
            reason="approved",
            command_id="approve-plan",
        )
        self.manager = WorktreeManager(
            self.repository, self.source, self.root / "managed"
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(cwd or self.source), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout.strip()

    def _attempt(self, suffix: str, scope: str = "allowed", metadata=None):
        values = {
            "kind": "code",
            "risk_level": "low",
            "write_scopes": [scope],
        }
        values.update(metadata or {})
        task = self.repository.create_task(
            self.task_list_id,
            subject=f"Code {suffix}",
            description=f"Change {suffix}",
            metadata=values,
        )
        agent = self.repository.create_team_agent(
            self.team.id,
            name=f"Teammate {suffix}",
            agent_id=f"agent_{suffix}",
        )
        session = self.repository.create_agent_session(
            self.team.id, agent.id, session_id=f"session_{suffix}"
        )
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id=f"claim-{suffix}",
        )
        return attempt

    def _confirm(self, *, allow_dirty: bool = False) -> None:
        self.manager.confirm_baseline(
            self.team.id,
            confirmed_by="user" if allow_dirty else "runtime",
            allow_dirty=allow_dirty,
            command_id=f"confirm-{allow_dirty}",
        )

    def test_model_timeout_keeps_worktree_frozen_until_validated_resume(self) -> None:
        attempt = self._attempt("model-timeout")
        self._confirm()
        binding = self.manager.create_for_attempt(attempt)
        head_before = self._git("rev-parse", "HEAD")
        self.repository.mark_attempt_suspect(attempt.id, reason="model_call_timeout")
        paused = self.repository.pause_attempt_after_worker_exit(attempt.id, reason="model_call_timeout")
        self.assertEqual(paused.state, TaskAttemptState.WAITING)
        self.assertFalse(paused.result_unknown)
        frozen = self.repository.get_attempt_worktree_binding(attempt.id)
        self.assertEqual(frozen.state, "frozen")
        self.assertFalse(frozen.write_enabled)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(self._git("rev-parse", "HEAD"), head_before)
        supervisor = TeamSupervisor(self.repository, lambda *_: None, enabled=True,
                                    worktree_manager=self.manager)
        try:
            resumed = supervisor.resume_attempt(attempt.id, resumed_by="user", reason="checked", command_id="resume-timeout")
            self.assertEqual(resumed.id, attempt.id)
            self.assertTrue(resumed.write_enabled)
            self.assertEqual(self.repository.get_attempt_worktree_binding(attempt.id).id, binding.id)
        finally:
            supervisor.stop()

    def test_code_question_answer_preserves_write_scope_and_rejects_frozen_binding(self) -> None:
        attempt = self._attempt("question")
        self._confirm()
        binding = self.manager.create_for_attempt(attempt)
        self.repository.create_agent_session(self.team.id, self.team.lead_agent_id)
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                reasons = []
                TeamQuestionTool(self.repository, attempt, reasons.append).run("Clarify naming", True)
                question = [m for m in self.repository.list_team_messages(self.team.id) if m.type == "QUESTION"][-1]
                self.repository.save_agent_session_checkpoint(
                    attempt.session_id, messages=[], context={}, safe_boundary="agent_yield",
                    target_state="waiting", waiting_reason=reasons[0],
                )
                answer = TeamAnswerQuestionTool(self.repository, self.team.id, self.team.lead_agent_id)
                if frozen:
                    with self.repository._transaction(immediate=True) as connection:
                        connection.execute("UPDATE worktree_bindings SET state = 'frozen', write_enabled = 0 WHERE id = ?", (binding.id,))
                    with self.assertRaisesRegex(StorageConflictError, "invalid Worktree"):
                        answer.run(question.id, "Use snake_case", False)
                    self.assertEqual(self.repository.get_agent_session(attempt.session_id).state.value, "waiting")
                else:
                    answer.run(question.id, "Use snake_case", False)
                    self.assertEqual(self.repository.get_agent_session(attempt.session_id).state.value, "work")
                    self.assertTrue(self.repository.get_task_attempt(attempt.id).write_enabled)
                    self.assertEqual(self.repository.get_attempt_worktree_binding(attempt.id).write_scopes, binding.write_scopes)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.base_commit)

    def test_model_timeout_does_not_clear_unknown_write_or_audit_record(self) -> None:
        attempt = self._attempt("unknown-model")
        self._confirm()
        binding = self.manager.create_for_attempt(attempt)
        execution = self.repository.begin_tool_execution(
            attempt.id, tool_call_id="unfinished-write", tool_name="write_file",
            risk="medium", is_write=True, input={"file_path": "allowed/new.txt"},
            worktree_id=binding.id,
        )
        paused = self.repository.pause_attempt_after_worker_exit(attempt.id, reason="model_response_timeout")
        self.assertTrue(paused.result_unknown)
        self.assertTrue(self.repository.get_tool_execution(execution.id).result_unknown)
        again = self.repository.pause_attempt_after_worker_exit(attempt.id, reason="model_call_timeout")
        self.assertTrue(again.result_unknown)
        supervisor = TeamSupervisor(self.repository, lambda *_: None, enabled=True,
                                    worktree_manager=self.manager)
        try:
            with self.assertRaisesRegex(WorktreeError, "acknowledged"):
                supervisor.resume_attempt(attempt.id, resumed_by="user", reason="checked", command_id="no-ack")
        finally:
            supervisor.stop()

    def _plan_required_attempt(self, suffix: str = "planned"):
        task = self.repository.create_task(
            self.task_list_id,
            subject=f"High risk {suffix}",
            description="Read before planning",
            metadata={
                "kind": "code",
                "risk_level": "high",
                "write_scopes": ["allowed"],
            },
        )
        agent = self.repository.create_team_agent(
            self.team.id,
            name=f"Planner {suffix}",
            agent_id=f"agent_planner_{suffix}",
        )
        session = self.repository.create_agent_session(
            self.team.id, agent.id, session_id=f"session_planner_{suffix}"
        )
        attempt = self.repository.claim_task_attempt(
            self.team.id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id=f"claim-planner-{suffix}",
            plan_required=True,
        )
        binding = self.manager.create_for_attempt(attempt)
        return attempt, binding

    def _create_attempt_plan(self, attempt, suffix: str):
        return self.repository.create_attempt_plan(
            attempt.id,
            summary=f"Plan {suffix}",
            planned_files=["allowed/base.txt"],
            planned_commands=["write_file"],
            planned_tests=["git diff --check"],
            write_scopes=["allowed"],
            risk_level="high",
            created_by=attempt.agent_id,
            command_id=f"create-attempt-plan-{suffix}",
        )

    def test_dirty_source_requires_confirmation_and_patch_is_not_copied(self) -> None:
        source_file = self.source / "allowed" / "base.txt"
        source_file.write_text("uncommitted\n", encoding="utf-8")
        with self.assertRaises(DirtyWorkspaceConfirmationRequired):
            self._confirm(allow_dirty=False)
        self._confirm(allow_dirty=True)

        binding = self.manager.create_for_attempt(self._attempt("dirty"))

        self.assertEqual(
            (Path(binding.path) / "allowed" / "base.txt").read_text(
                encoding="utf-8"
            ),
            "base\n",
        )
        self.assertEqual(source_file.read_text(encoding="utf-8"), "uncommitted\n")
        self.assertTrue(binding.write_enabled)

    def test_two_attempts_get_independent_worktrees_without_changing_source(self) -> None:
        self._confirm()
        source_branch = self._git("branch", "--show-current")

        first = self.manager.create_for_attempt(self._attempt("one", "allowed/one"))
        second = self.manager.create_for_attempt(self._attempt("two", "allowed/two"))

        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.branch, second.branch)
        self.assertEqual(self._git("branch", "--show-current"), source_branch)
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_invalid_binding_fails_closed_without_source_fallback(self) -> None:
        self._confirm()
        binding = self.manager.create_for_attempt(self._attempt("invalid"))
        self._git("checkout", "--detach", cwd=Path(binding.path))

        with self.assertRaisesRegex(WorktreeError, "branch"):
            self.manager.validate_binding(binding.id)

    def test_file_scope_violation_is_blocked_before_execution(self) -> None:
        self._confirm()
        attempt = self._attempt("file")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        allowed = gate.execute(
            "write_file", {"file_path": "allowed/new.txt", "content": "ok\n"}
        )
        blocked = gate.execute(
            "write_file", {"file_path": "outside.txt", "content": "bad\n"}
        )

        self.assertIn("Wrote", allowed)
        self.assertIn('"status":"blocked"', blocked)
        self.assertIn('"executed":false', blocked)
        self.assertIn('"reason_code":"path_outside_write_scope"', blocked)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        active = self.repository.get_worktree_binding(binding.id)
        self.assertEqual(active.state, "active")
        self.assertTrue(active.write_enabled)
        self.assertEqual(
            (Path(binding.path) / "outside.txt").read_text(encoding="utf-8"),
            "outside\n",
        )

    def test_parent_traversal_is_blocked_before_file_write(self) -> None:
        self._confirm()
        attempt = self._attempt("traversal")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "write_file", {"file_path": "../escape.txt", "content": "bad\n"}
        )

        self.assertIn('"reason_code":"path_escapes_worktree"', output)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertFalse((Path(binding.path).parent / "escape.txt").exists())

    def test_symlink_cannot_redirect_an_allowed_path_outside_worktree(self) -> None:
        self._confirm()
        attempt = self._attempt("symlink")
        binding = self.manager.create_for_attempt(attempt)
        external = self.root / "external"
        external.mkdir()
        link = Path(binding.path) / "allowed" / "link"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation is unavailable: {exc}")
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "write_file",
            {"file_path": "allowed/link/escape.txt", "content": "bad\n"},
        )

        self.assertIn('"reason_code":"path_escapes_worktree"', output)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertFalse((external / "escape.txt").exists())

    def test_shell_diff_audit_catches_out_of_scope_write(self) -> None:
        self._confirm()
        attempt = self._attempt("shell")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(BashTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "bash",
            {
                "command": (
                    "python -c \"from pathlib import Path; "
                    "Path('outside.txt').write_text('changed')\""
                )
            },
        )

        self.assertIn("scope violation", output)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.WAITING,
        )
        self.assertTrue(
            any(
                item.status == "scope_violation"
                for item in self.repository.list_tool_executions(attempt.id)
            )
        )

    def test_shell_stream_redirection_is_not_treated_as_a_file_path(self) -> None:
        self._confirm()
        attempt = self._attempt("stream-redirection")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(BashTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "bash",
            {"command": "Get-Content allowed/base.txt 2>&1"},
        )

        self.assertIn("base", output)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertEqual(
            self.repository.get_worktree_binding(binding.id).state,
            "active",
        )

    def test_unresolved_powershell_write_path_is_blocked_before_execution(self) -> None:
        self._confirm()
        attempt = self._attempt("powershell-unresolved")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(BashTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "bash",
            {"command": "Set-Content -Value bad -NoNewline"},
        )

        self.assertIn('"reason_code":"shell_write_path_unresolved"', output)
        self.assertIn('"executed":false', output)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertEqual(
            self.repository.get_worktree_binding(binding.id).state,
            "active",
        )

    def test_unknown_write_result_freezes_until_user_acknowledges_it(self) -> None:
        self._confirm()
        attempt = self._attempt("unknown-write")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()

        def partial_write(file_path: str, content: str) -> str:
            (Path(binding.path) / file_path).write_text(content, encoding="utf-8")
            raise RuntimeError("connection lost after write")

        registry.register_handler(
            ToolDefinition(
                name="write_file",
                description="Test partial write",
                input_schema={"type": "object"},
            ),
            partial_write,
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "write_file",
            {"file_path": "allowed/partial.txt", "content": "partial\n"},
        )

        self.assertIn('"reason_code":"unknown_write_result"', output)
        waiting = self.repository.get_task_attempt(attempt.id)
        self.assertEqual(waiting.state, TaskAttemptState.WAITING)
        self.assertTrue(waiting.result_unknown)
        frozen = self.repository.get_worktree_binding(binding.id)
        self.assertEqual(frozen.state, "frozen")
        self.assertFalse(frozen.write_enabled)
        executions = self.repository.list_tool_executions(attempt.id)
        self.assertEqual(len(executions), 1)
        self.assertTrue(executions[0].result_unknown)

        verified = self.manager.validate_recoverable_binding(binding.id)
        with self.assertRaisesRegex(StorageConflictError, "must be acknowledged"):
            self.repository.resume_task_attempt(
                attempt.id,
                resumed_by="user",
                reason="checked",
                command_id="resume-unknown-without-ack",
                validated_worktree_fingerprint=verified.fingerprint,
            )

        resumed = self.repository.resume_task_attempt(
            attempt.id,
            resumed_by="user",
            reason="I inspected the Worktree",
            command_id="resume-unknown",
            validated_worktree_fingerprint=verified.fingerprint,
            acknowledge_unknown_result=True,
        )
        repeated = self.repository.resume_task_attempt(
            attempt.id,
            resumed_by="user",
            reason="I inspected the Worktree",
            command_id="resume-unknown",
            validated_worktree_fingerprint=verified.fingerprint,
            acknowledge_unknown_result=True,
        )

        self.assertEqual(resumed.state, TaskAttemptState.RUNNING)
        self.assertFalse(resumed.result_unknown)
        self.assertEqual(repeated.id, resumed.id)
        active = self.repository.get_worktree_binding(binding.id)
        self.assertEqual(active.state, "active")
        self.assertTrue(active.write_enabled)
        resumed_messages = [
            item
            for item in self.repository.list_team_messages(self.team.id)
            if item.type == "ATTEMPT_RESUMED" and item.attempt_id == attempt.id
        ]
        self.assertEqual(len(resumed_messages), 1)

    def test_supervisor_rechecks_scope_before_resuming_a_frozen_attempt(self) -> None:
        self._confirm()
        attempt = self._attempt("resume-scope")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(BashTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)
        gate.execute(
            "bash",
            {
                "command": (
                    "python -c \"from pathlib import Path; "
                    "Path('outside.txt').write_text('changed')\""
                )
            },
        )
        supervisor = TeamSupervisor(
            self.repository,
            lambda *_args: self.fail("Recovery test must not start a worker"),
            enabled=True,
            write_enabled=True,
            worktree_manager=self.manager,
        )
        try:
            with self.assertRaisesRegex(WorktreeError, "outside"):
                supervisor.resume_attempt(
                    attempt.id,
                    resumed_by="user",
                    reason="not fixed yet",
                    command_id="resume-before-fix",
                )
            self.assertEqual(
                self.repository.get_task_attempt(attempt.id).state,
                TaskAttemptState.WAITING,
            )

            (Path(binding.path) / "outside.txt").write_text(
                "outside\n", encoding="utf-8"
            )
            resumed = supervisor.resume_attempt(
                attempt.id,
                resumed_by="user",
                reason="outside file restored",
                command_id="resume-after-fix",
            )
            self.assertEqual(resumed.state, TaskAttemptState.RUNNING)
        finally:
            supervisor.stop()

    def test_stale_attempt_plan_scope_hash_blocks_recovery(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt("stale-recovery-plan")
        plan = self._create_attempt_plan(attempt, "stale-recovery-plan")
        self.repository.submit_attempt_plan(
            attempt.id,
            plan.revision,
            command_id="submit-stale-recovery-plan",
        )
        self.repository.decide_attempt_plan(
            attempt.id,
            plan.revision,
            decision="approve",
            decided_by="agent_lead",
            reason="approved before corruption",
            command_id="approve-stale-recovery-plan",
            validated_worktree_fingerprint=self.manager.validate_binding(
                binding.id
            ).fingerprint,
        )
        self.repository.pause_attempt_after_worker_exit(
            attempt.id,
            reason="protocol_incomplete",
        )
        with self.repository._transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE attempt_plans SET scope_hash = 'stale' WHERE id = ?",
                (plan.id,),
            )

        verified = self.manager.validate_recoverable_binding(binding.id)
        with self.assertRaisesRegex(StorageConflictError, "scope hash"):
            self.repository.resume_task_attempt(
                attempt.id,
                resumed_by="user",
                reason="try stale plan",
                command_id="resume-stale-recovery-plan",
                validated_worktree_fingerprint=verified.fingerprint,
            )
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.WAITING,
        )

    def test_supervisor_builds_worker_from_refreshed_attempt_after_binding(self) -> None:
        self._confirm()
        task = self.repository.create_task(
            self.task_list_id,
            subject="Fresh code Attempt",
            description="Use the post-binding write permission",
            metadata={
                "kind": "code",
                "risk_level": "low",
                "write_scopes": ["allowed"],
            },
        )
        agent = self.repository.create_team_agent(
            self.team.id,
            name="Fresh Teammate",
            agent_id="agent_fresh",
        )
        self.repository.create_agent_session(
            self.team.id, agent.id, session_id="session_fresh"
        )
        built = threading.Event()
        observed_write_permissions: list[bool] = []

        class _Client:
            def create_message(self, **_kwargs):
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        def builder(_session, worker_attempt, cancellation):
            observed_write_permissions.append(worker_attempt.write_enabled)
            built.set()
            return Agent(
                client=_Client(),
                tools=ToolRegistry(),
                config=AgentConfig(model="fake"),
                cancellation=cancellation,
                allow_subagents=False,
            )

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            write_enabled=True,
            max_workers=1,
            worktree_manager=self.manager,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 1)
            self.assertTrue(built.wait(2))
            self.assertTrue(observed_write_permissions[0])
            attempts = self.repository.list_task_attempts(
                self.team.id, task_id=task.task.id
            )
            self.assertEqual(len(attempts), 1)
            self.assertTrue(attempts[0].write_enabled)
        finally:
            supervisor.stop()

    def test_powershell_new_item_options_do_not_look_like_write_paths(self) -> None:
        self._confirm()
        attempt = self._attempt(
            "new-item",
            metadata={"write_scopes": ["components/__init__.py"]},
        )
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()

        def create_directory(command: str) -> str:
            self.assertIn("New-Item", command)
            (Path(binding.path) / "components").mkdir()
            return "created"

        registry.register_handler(
            ToolDefinition(
                name="bash",
                description="Test shell handler",
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            ),
            create_directory,
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "bash",
            {
                "command": (
                    "New-Item -ItemType Directory -Force -Path components"
                )
            },
        )

        self.assertEqual(output, "created")
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertEqual(
            self.repository.get_worktree_binding(binding.id).state,
            "active",
        )

    def test_recursive_directory_scope_allows_nested_files_only(self) -> None:
        self._confirm()
        allowed_attempt = self._attempt(
            "recursive-scope",
            scope="components/calculator/**",
        )
        allowed_binding = self.manager.create_for_attempt(allowed_attempt)
        allowed_registry = ToolRegistry()
        allowed_registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(allowed_binding.path)))
        )
        allowed_gate = TeamToolExecutionGate(
            self.repository, self.manager, allowed_attempt.id
        ).wrap(allowed_registry)

        allowed = allowed_gate.execute(
            "write_file",
            {
                "file_path": "components/calculator/calculator.py",
                "content": "def add(a, b):\n    return a + b\n",
            },
        )

        self.assertIn("Wrote", allowed)
        self.assertEqual(
            self.repository.get_task_attempt(allowed_attempt.id).state,
            TaskAttemptState.RUNNING,
        )

        blocked = allowed_gate.execute(
            "write_file",
            {
                "file_path": "components/calculator_extra/bad.py",
                "content": "bad\n",
            },
        )

        self.assertIn('"reason_code":"path_outside_write_scope"', blocked)
        self.assertEqual(
            self.repository.get_task_attempt(allowed_attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertFalse(
            (Path(allowed_binding.path) / "components/calculator_extra/bad.py").exists()
        )

    def test_teammate_git_write_commands_are_blocked(self) -> None:
        self._confirm()
        attempt = self._attempt("git")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(BashTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        for command in ("git add .", "git commit -m nope", "git merge other"):
            with self.subTest(command=command):
                output = gate.execute("bash", {"command": command})
                self.assertIn('"reason_code":"git_write_forbidden"', output)
                self.assertIn("cannot run Git write", output)
                self.assertEqual(
                    self.repository.get_task_attempt(attempt.id).state,
                    TaskAttemptState.RUNNING,
                )

    def test_unapproved_mcp_is_blocked_and_approved_mcp_is_diff_audited(self) -> None:
        self._confirm()
        attempt = self._attempt("mcp")
        binding = self.manager.create_for_attempt(attempt)
        definition = ToolDefinition(
            name="mcp__test__write",
            description="test",
            input_schema={"type": "object", "properties": {}},
        )
        registry = ToolRegistry()

        def write_outside() -> str:
            (Path(binding.path) / "outside.txt").write_text(
                "mcp change\n", encoding="utf-8"
            )
            return "changed"

        registry.register_handler(definition, write_outside)
        blocked_gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)
        self.assertIn(
            "not approved", blocked_gate.execute("mcp__test__write", {})
        )
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )

        audited_gate = TeamToolExecutionGate(
            self.repository,
            self.manager,
            attempt.id,
            allowed_mcp_tools={"mcp__test__write"},
        ).wrap(registry)
        output = audited_gate.execute("mcp__test__write", {})

        self.assertIn("scope violation", output)
        self.assertEqual(
            self.repository.get_worktree_binding(binding.id).state, "frozen"
        )

    def test_plan_required_attempt_has_read_only_worktree(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt()
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        output = gate.execute(
            "write_file", {"file_path": "allowed/no.txt", "content": "no\n"}
        )

        self.assertIn("write permission is not enabled", output)
        self.assertFalse((Path(binding.path) / "allowed" / "no.txt").exists())
        self.assertFalse(binding.write_enabled)

    def test_rejected_attempt_plan_is_immutable_and_idempotent(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt("reject")
        first = self._create_attempt_plan(attempt, "p1")
        self.repository.submit_attempt_plan(
            attempt.id, first.revision, command_id="submit-p1"
        )
        rejected = self.repository.decide_attempt_plan(
            attempt.id,
            first.revision,
            decision="reject",
            decided_by="agent_lead",
            reason="Narrow the command list",
            command_id="reject-p1",
        )
        repeated = self.repository.decide_attempt_plan(
            attempt.id,
            first.revision,
            decision="reject",
            decided_by="agent_lead",
            reason="Narrow the command list",
            command_id="reject-p1",
        )

        self.assertEqual(rejected, repeated)
        self.assertEqual(rejected.status, AttemptPlanStatus.REJECTED)
        self.assertEqual(rejected.decided_by, "agent_lead")
        self.assertIsNotNone(rejected.decided_at)
        self.assertEqual(rejected.decision_reason, "Narrow the command list")
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.PLAN_REQUIRED,
        )
        retained = self.repository.get_worktree_binding(binding.id)
        self.assertEqual(retained.state, "active")
        self.assertFalse(retained.write_enabled)
        self.assertEqual(len(self.repository.list_attempt_plans(attempt.id)), 1)
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.decide_attempt_plan(
                attempt.id,
                first.revision,
                decision="approve",
                decided_by="agent_lead",
                reason="changed mind",
                command_id="approve-rejected-p1",
            )

    def test_p2_cannot_write_until_separately_approved(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt("p2")
        first = self._create_attempt_plan(attempt, "first")
        self.repository.submit_attempt_plan(
            attempt.id, first.revision, command_id="submit-first"
        )
        self.repository.decide_attempt_plan(
            attempt.id,
            first.revision,
            decision="reject",
            decided_by="agent_lead",
            reason="revise",
            command_id="reject-first",
        )
        second = self._create_attempt_plan(attempt, "second")
        self.repository.submit_attempt_plan(
            attempt.id, second.revision, command_id="submit-second"
        )
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)

        before = gate.execute(
            "write_file", {"file_path": "allowed/p2.txt", "content": "no\n"}
        )
        planning_tools = {
            tool.definition.name
            for tool in create_teammate_tools(
                self.repository,
                self.manager,
                self.repository.get_task_attempt(attempt.id),
                write_enabled=False,
                yield_callback=lambda _reason: None,
            )
        }
        self.assertIn("write permission is not enabled", before)
        self.assertIn("team_submit_attempt_plan", planning_tools)
        self.assertNotIn("team_submit_candidate", planning_tools)
        self.assertFalse((Path(binding.path) / "allowed" / "p2.txt").exists())

        approved = self.repository.decide_attempt_plan(
            attempt.id,
            second.revision,
            decision="approve",
            decided_by="agent_lead",
            reason="within approved Team Plan",
            command_id="approve-second",
            validated_worktree_fingerprint=self.manager.validate_binding(
                binding.id
            ).fingerprint,
        )
        after = gate.execute(
            "write_file", {"file_path": "allowed/p2.txt", "content": "yes\n"}
        )
        work_tools = {
            tool.definition.name
            for tool in create_teammate_tools(
                self.repository,
                self.manager,
                self.repository.get_task_attempt(attempt.id),
                write_enabled=True,
                yield_callback=lambda _reason: None,
            )
        }

        self.assertEqual(approved.status, AttemptPlanStatus.APPROVED)
        self.assertIn("Wrote", after)
        self.assertIn("team_submit_candidate", work_tools)
        self.assertNotIn("team_submit_attempt_plan", work_tools)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.RUNNING,
        )
        self.assertTrue(self.repository.get_worktree_binding(binding.id).write_enabled)

    def test_supervisor_resumes_existing_attempt_after_plan_approval(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt("resume")
        plan = self._create_attempt_plan(attempt, "resume")
        self.repository.submit_attempt_plan(
            attempt.id, plan.revision, command_id="submit-resume"
        )
        self.repository.decide_attempt_plan(
            attempt.id,
            plan.revision,
            decision="approve",
            decided_by="agent_lead",
            reason="approved",
            command_id="approve-resume",
            validated_worktree_fingerprint=self.manager.validate_binding(
                binding.id
            ).fingerprint,
        )
        built = []

        class _Client:
            def create_message(self, **_kwargs):
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "resumed"}],
                )

        def builder(_session, resumed, cancellation):
            built.append(resumed.id)
            return Agent(
                client=_Client(),
                tools=ToolRegistry(),
                config=AgentConfig(model="fake"),
                cancellation=cancellation,
                allow_subagents=False,
            )

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            write_enabled=True,
            max_workers=1,
            worktree_manager=self.manager,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 1)
            for _ in range(100):
                supervisor.dispatch_once(self.team.id)
                if not supervisor.active_attempt_ids():
                    break
            self.assertEqual(built, [attempt.id, attempt.id])
            self.assertEqual(len(self.repository.list_task_attempts(self.team.id)), 1)
            self.assertEqual(
                self.repository.get_task_attempt(attempt.id).state,
                TaskAttemptState.WAITING,
            )
        finally:
            supervisor.stop()

    def test_supervisor_creates_binding_before_starting_code_worker(self) -> None:
        task = self.repository.create_task(
            self.task_list_id,
            subject="Supervised code",
            description="Run in Worktree",
            metadata={
                "kind": "code",
                "risk_level": "low",
                "write_scopes": ["allowed"],
            },
        )
        agent = self.repository.create_team_agent(
            self.team.id, name="Worker", agent_id="agent_worker"
        )
        self.repository.create_agent_session(
            self.team.id, agent.id, session_id="session_worker"
        )

        class _Client:
            def create_message(self, **_kwargs):
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "done"}],
                )

        def builder(_session, attempt, cancellation):
            binding = self.repository.get_attempt_worktree_binding(attempt.id)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertTrue(binding.write_enabled)
            return Agent(
                client=_Client(),
                tools=ToolRegistry(),
                config=AgentConfig(model="fake"),
                cancellation=cancellation,
                allow_subagents=False,
            )

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            write_enabled=True,
            max_workers=1,
            worktree_manager=self.manager,
        )
        try:
            self.assertEqual(supervisor.dispatch_once(self.team.id), 1)
            for _ in range(100):
                supervisor.dispatch_once(self.team.id)
                if not supervisor.active_attempt_ids():
                    break
            attempts = self.repository.list_task_attempts(
                self.team.id, task_id=task.task.id
            )
            self.assertEqual(len(attempts), 1)
            self.assertIsNotNone(
                self.repository.get_attempt_worktree_binding(attempts[0].id)
            )
        finally:
            supervisor.stop()

    def test_running_write_execution_becomes_unknown_after_restart(self) -> None:
        self._confirm()
        attempt = self._attempt("restart")
        binding = self.manager.create_for_attempt(attempt)
        old_session = self.repository.get_agent_session(attempt.session_id)
        assigned = next(
            item
            for item in self.repository.list_team_messages(self.team.id)
            if item.attempt_id == attempt.id and item.type == "TASK_ASSIGNED"
        )
        self.repository.save_agent_session_checkpoint(
            old_session.id,
            messages=[{"role": "assistant", "content": "safe checkpoint"}],
            context={"marker": "safe"},
            safe_boundary="tool_result",
            acknowledged_message_ids=[assigned.id],
        )
        pending = self.repository.send_team_message(
            self.team.id,
            sender_type="runtime",
            recipient_type="teammate",
            recipient_agent_id=attempt.agent_id,
            recipient_generation=old_session.generation,
            message_type="SYSTEM_ERROR",
            payload={
                "reason_code": "restart-test",
                "reason": "Inspect the retained Worktree",
                "effective_scope": "attempt",
            },
            dedupe_key="restart-teammate-message",
            task_id=attempt.task_id,
            attempt_id=attempt.id,
            priority="control",
        )
        execution = self.repository.begin_tool_execution(
            attempt.id,
            tool_call_id="provider-call",
            tool_name="write_file",
            risk="medium",
            is_write=True,
            input={"file_path": "allowed/new.txt"},
            worktree_id=binding.id,
        )
        self.repository.close()
        self.repository = SQLiteRepository(
            self.root / "state.db", recover_incomplete=True
        )
        self.manager = WorktreeManager(
            self.repository, self.source, self.root / "managed"
        )

        recovered = self.repository.get_tool_execution(execution.id)
        self.assertEqual(recovered.status, "failed")
        self.assertTrue(recovered.result_unknown)
        interrupted = self.repository.get_task_attempt(attempt.id)
        self.assertEqual(interrupted.state, TaskAttemptState.WAITING)
        self.assertTrue(interrupted.result_unknown)
        self.assertEqual(
            self.repository.get_agent_session(old_session.id).state,
            AgentSessionState.LOST,
        )
        self.assertEqual(
            self.repository.get_worktree_binding(binding.id).state, "frozen"
        )
        self.assertEqual(
            self.repository.list_resource_leases(
                self.team.id, attempt_id=attempt.id
            )[0].state,
            "active",
        )

        started = threading.Event()
        release = threading.Event()
        built = []

        class _Client:
            def create_message(self, **_kwargs):
                started.set()
                release.wait(timeout=3)
                return ModelResponse(
                    stop_reason="end_turn",
                    content=[{"type": "text", "text": "inspected"}],
                )

        def builder(session, resumed_attempt, cancellation):
            built.append((session, resumed_attempt))
            return Agent(
                client=_Client(),
                tools=ToolRegistry(),
                config=AgentConfig(model="fake"),
                cancellation=cancellation,
                allow_subagents=False,
            )

        supervisor = TeamSupervisor(
            self.repository,
            builder,
            enabled=True,
            write_enabled=True,
            max_workers=1,
            worktree_manager=self.manager,
        )
        try:
            resumed = supervisor.resume_attempt(
                attempt.id,
                resumed_by="user",
                reason="I inspected the retained Worktree",
                command_id="resume-after-service-restart",
                acknowledge_unknown_result=True,
            )
            self.assertEqual(resumed.id, attempt.id)
            self.assertEqual(resumed.state, TaskAttemptState.RUNNING)
            self.assertFalse(resumed.result_unknown)
            new_session = self.repository.get_agent_session(resumed.session_id)
            self.assertNotEqual(new_session.id, old_session.id)
            self.assertEqual(new_session.generation, old_session.generation + 1)
            self.assertEqual(new_session.state, AgentSessionState.WORK)

            rebound = self.repository.get_worktree_binding(binding.id)
            self.assertEqual(rebound.state, "active")
            self.assertEqual(rebound.session_id, new_session.id)
            self.assertEqual(rebound.generation, new_session.generation)
            self.manager.validate_binding(binding.id)
            lease = self.repository.list_resource_leases(
                self.team.id, attempt_id=attempt.id
            )[0]
            self.assertEqual(lease.state, "active")
            self.assertEqual(lease.generation, new_session.generation)

            checkpoint = self.repository.get_latest_agent_session_checkpoint(
                new_session.id
            )
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            self.assertEqual(checkpoint.safe_boundary, "recovery_checkpoint")
            self.assertEqual(checkpoint.context["marker"], "safe")
            redirected = self.repository.get_team_message(pending.id)
            assert redirected is not None
            self.assertEqual(
                redirected.recipient_generation, new_session.generation
            )
            resumed_message = next(
                item
                for item in self.repository.list_team_messages(self.team.id)
                if item.attempt_id == attempt.id and item.type == "ATTEMPT_RESUMED"
            )
            self.assertTrue(resumed_message.payload["do_not_replay"])
            self.assertTrue(
                self.repository.get_tool_execution(execution.id).result_unknown
            )

            self.assertEqual(supervisor.dispatch_once(self.team.id), 1)
            self.assertTrue(started.wait(2))
            self.assertEqual(len(built), 1)
            self.assertEqual(built[0][0].id, new_session.id)
            self.assertEqual(built[0][1].id, attempt.id)
            repeated = supervisor.resume_attempt(
                attempt.id,
                resumed_by="user",
                reason="I inspected the retained Worktree",
                command_id="resume-after-service-restart",
                acknowledge_unknown_result=True,
            )
            self.assertEqual(repeated.id, attempt.id)
            self.assertEqual(
                len(
                    [
                        item
                        for item in self.repository.list_team_messages(self.team.id)
                        if item.attempt_id == attempt.id
                        and item.type == "ATTEMPT_RESUMED"
                    ]
                ),
                1,
            )
        finally:
            release.set()
            supervisor.stop()

    def test_legacy_service_restart_orphan_can_reuse_attempt_and_worktree(self) -> None:
        self._confirm()
        attempt = self._attempt("legacy-restart")
        binding = self.manager.create_for_attempt(attempt)
        old_session = self.repository.get_agent_session(attempt.session_id)
        self.repository.orphan_attempt_after_worker_exit(
            attempt.id,
            reason="service_restart",
        )
        with self.repository._transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE task_attempts SET error_json = ? WHERE id = ?",
                (
                    '{"type":"service_restart","message":"legacy restart"}',
                    attempt.id,
                ),
            )

        supervisor = TeamSupervisor(
            self.repository,
            lambda *_args: self.fail("Legacy recovery should not start eagerly"),
            enabled=True,
            write_enabled=True,
            max_workers=1,
            worktree_manager=self.manager,
        )
        try:
            resumed = supervisor.resume_attempt(
                attempt.id,
                resumed_by="user",
                reason="I inspected the legacy Worktree",
                command_id="resume-legacy-service-restart",
                acknowledge_unknown_result=True,
            )
            self.assertEqual(resumed.id, attempt.id)
            self.assertEqual(resumed.state, TaskAttemptState.RUNNING)
            self.assertNotEqual(resumed.session_id, old_session.id)
            rebound = self.repository.get_worktree_binding(binding.id)
            self.assertEqual(rebound.state, "active")
            self.assertEqual(rebound.session_id, resumed.session_id)
            self.manager.validate_binding(binding.id)
        finally:
            supervisor.stop()

    def test_candidate_freezes_diff_and_lead_rework_reopens_scoped_writes(self) -> None:
        self._confirm()
        attempt = self._attempt("candidate-rework")
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(
            WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path)))
        )
        gate = TeamToolExecutionGate(
            self.repository, self.manager, attempt.id
        ).wrap(registry)
        gate.execute(
            "write_file",
            {"file_path": "allowed/change.txt", "content": "first\n"},
        )
        candidates = CandidateService(self.repository, self.manager)

        first = candidates.submit(attempt.id, summary="First candidate")

        self.assertEqual(first.status, CandidateStatus.SUBMITTED)
        self.assertTrue(Path(first.diff_ref).is_file())
        self.assertFalse(self.repository.get_worktree_binding(binding.id).write_enabled)
        self.assertIn(
            "write permission is not enabled",
            gate.execute(
                "write_file",
                {"file_path": "allowed/change.txt", "content": "blocked\n"},
            ),
        )
        rework = candidates.review(
            first.id,
            decision="rework",
            reviewed_by="agent_lead",
            reason="Add details",
            command_id="rework-first",
        )
        self.assertEqual(rework.status, CandidateStatus.REWORK)
        self.assertTrue(self.repository.get_worktree_binding(binding.id).write_enabled)
        self.assertIn(
            "Wrote",
            gate.execute(
                "write_file",
                {"file_path": "allowed/change.txt", "content": "second\n"},
            ),
        )
        second = candidates.submit(attempt.id, summary="Second candidate")
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(first.diff_hash, second.diff_hash)

    def test_candidate_mutation_after_submission_blocks_review(self) -> None:
        self._confirm()
        attempt = self._attempt("candidate-mutated")
        binding = self.manager.create_for_attempt(attempt)
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "candidate\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(attempt.id, summary="Frozen")
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "mutated later\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(WorktreeError, "changed"):
            candidates.review(
                candidate.id,
                decision="accept",
                reviewed_by="agent_lead",
                reason="looks good",
                command_id="accept-mutated",
            )
        self.assertEqual(
            self.repository.get_candidate(candidate.id).status,
            CandidateStatus.SUBMITTED,
        )

    def test_high_risk_candidate_needs_separate_immutable_user_approval(self) -> None:
        self._confirm()
        attempt, binding = self._plan_required_attempt("candidate-approval")
        plan = self._create_attempt_plan(attempt, "candidate-approval")
        self.repository.submit_attempt_plan(
            attempt.id, plan.revision, command_id="submit-high-risk-plan"
        )
        self.repository.decide_attempt_plan(
            attempt.id,
            plan.revision,
            decision="approve",
            decided_by="agent_lead",
            reason="Plan stays within the approved scope",
            command_id="approve-high-risk-plan",
            validated_worktree_fingerprint=self.manager.validate_binding(
                binding.id
            ).fingerprint,
        )
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "high risk candidate\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(attempt.id, summary="High-risk candidate")
        accepted = candidates.review(
            candidate.id,
            decision="accept",
            reviewed_by="agent_lead",
            reason="Semantic review passed",
            command_id="lead-accept-high-risk",
        )

        self.assertTrue(accepted.user_approval_required)
        self.assertIsNone(accepted.user_decision)
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.WAITING,
        )
        self.assertFalse(
            self.repository.get_worktree_binding(binding.id).write_enabled
        )
        with self.assertRaisesRegex(ValueError, "requires user approval"):
            candidates.validate_and_commit(candidate.id)

        approved = self.repository.decide_candidate_user_approval(
            candidate.id,
            decision="approve",
            decided_by="user",
            reason="Approved after reviewing the risk",
            command_id="user-approve-high-risk",
        )
        repeated = self.repository.decide_candidate_user_approval(
            candidate.id,
            decision="approve",
            decided_by="user",
            reason="Approved after reviewing the risk",
            command_id="user-approve-high-risk",
        )

        self.assertEqual(approved, repeated)
        self.assertEqual(approved.user_decision, "approved")
        self.assertEqual(
            self.repository.get_task_attempt(attempt.id).state,
            TaskAttemptState.VALIDATING,
        )
        self.assertFalse(
            self.repository.get_worktree_binding(binding.id).write_enabled
        )
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.decide_candidate_user_approval(
                candidate.id,
                decision="reject",
                decided_by="user",
                reason="Try to change an immutable decision",
                command_id="user-reject-after-approval",
            )

    def test_runtime_validates_and_creates_retained_candidate_commit_only(self) -> None:
        self._confirm()
        attempt = self._attempt(
            "candidate-commit",
            metadata={"validation_commands": ["git status --short"]},
        )
        binding = self.manager.create_for_attempt(attempt)
        source_head = self._git("rev-parse", "HEAD")
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "candidate commit\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(
            attempt.id,
            summary="Create candidate commit",
            tests_reported=["manual check"],
        )
        candidates.review(
            candidate.id,
            decision="accept",
            reviewed_by="agent_lead",
            reason="Semantics accepted",
            command_id="accept-candidate",
        )

        committed = candidates.validate_and_commit(
            candidate.id, trace_id="trace-test"
        )

        self.assertEqual(committed.status, CandidateStatus.COMMITTED)
        self.assertIsNotNone(committed.commit_hash)
        self.assertEqual(self._git("rev-parse", "HEAD"), source_head)
        self.assertEqual(self._git("status", "--porcelain"), "")
        retained = self.repository.get_worktree_binding(binding.id)
        self.assertEqual(retained.state, "retained")
        self.assertFalse(retained.write_enabled)
        self.assertEqual(
            self._git("status", "--porcelain", cwd=Path(binding.path)), ""
        )
        message = self._git(
            "show", "-s", "--format=%B", committed.commit_hash, cwd=Path(binding.path)
        )
        self.assertIn(f"Candidate: {candidate.id}", message)
        self.assertIn("Trace: trace-test", message)

    def test_failed_runtime_validation_does_not_commit_or_release_lease(self) -> None:
        self._confirm()
        attempt = self._attempt(
            "candidate-fail",
            metadata={"validation_commands": ["exit 7"]},
        )
        binding = self.manager.create_for_attempt(attempt)
        old_head = self._git("rev-parse", "HEAD", cwd=Path(binding.path))
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "will fail\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(attempt.id, summary="Fail validation")
        candidates.review(
            candidate.id,
            decision="accept",
            reviewed_by="agent_lead",
            reason="Review passed",
            command_id="accept-failing",
        )

        failed = candidates.validate_and_commit(candidate.id)

        self.assertEqual(failed.status, CandidateStatus.VALIDATION_FAILED)
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=Path(binding.path)), old_head
        )
        self.assertEqual(
            self.repository.list_resource_leases(
                self.team.id, attempt_id=attempt.id
            )[0].state,
            "active",
        )
        self.assertFalse(self.repository.get_worktree_binding(binding.id).write_enabled)

    def test_manual_cherry_pick_is_verified_read_only_and_completes_team(self) -> None:
        self._confirm()
        attempt = self._attempt("manual-integration")
        binding = self.manager.create_for_attempt(attempt)
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "integrated\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(attempt.id, summary="Manual integration")
        candidates.review(
            candidate.id,
            decision="accept",
            reviewed_by="agent_lead",
            reason="accepted",
            command_id="accept-manual",
        )
        committed = candidates.validate_and_commit(candidate.id)
        assert committed.commit_hash is not None
        self._git("checkout", "-b", "user/integration")
        self._git("cherry-pick", "-x", committed.commit_hash)
        target = self._git("rev-parse", "HEAD")

        check = ManualIntegrationVerifier(self.repository).verify(
            self.team.id,
            target_ref="user/integration",
            verified_by="user",
            command_id="verify-manual",
        )

        self.assertEqual(check["target_commit"], target)
        self.assertEqual(check["status"], "verified")
        self.assertTrue(check["checks"][0]["integrated"])
        self.assertIsNotNone(
            self.repository.get_candidate(candidate.id).integrated_at
        )
        self.assertEqual(
            self.repository.get_team_run(self.team.id).state.value, "completed"
        )

    def test_user_approved_cleanup_removes_only_retained_worktree_directory(self) -> None:
        self._confirm()
        attempt = self._attempt("cleanup")
        binding = self.manager.create_for_attempt(attempt)
        (Path(binding.path) / "allowed" / "base.txt").write_text(
            "cleanup\n", encoding="utf-8"
        )
        candidates = CandidateService(self.repository, self.manager)
        candidate = candidates.submit(attempt.id, summary="Cleanup")
        candidates.review(
            candidate.id,
            decision="accept",
            reviewed_by="agent_lead",
            reason="accepted",
            command_id="accept-cleanup",
        )
        committed = candidates.validate_and_commit(candidate.id)
        assert committed.commit_hash is not None

        cleaned = self.manager.cleanup_retained(binding.id)

        self.assertEqual(cleaned.state, "cleaned")
        self.assertFalse(Path(binding.path).exists())
        self.assertEqual(
            self._git("rev-parse", committed.commit_hash), committed.commit_hash
        )


if __name__ == "__main__":
    unittest.main()
