"""Candidate freezing, Lead review checks, Runtime validation and commit."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.runtime_platform import current_runtime_platform
from codeagent.teams.models import CandidateRecord, CandidateStatus
from codeagent.worktrees import WorktreeError, WorktreeManager


class CandidateService:
    """Orchestrate Git side effects around transactional Candidate state."""

    def __init__(
        self,
        repository: Any,
        worktrees: WorktreeManager,
        *,
        validation_timeout: int = 600,
    ) -> None:
        self.repository = repository
        self.worktrees = worktrees
        self.validation_timeout = max(1, int(validation_timeout))

    def submit(
        self,
        attempt_id: str,
        *,
        summary: str,
        tests_reported: Sequence[str] = (),
        known_risks: Sequence[str] = (),
        command_id: str | None = None,
    ) -> CandidateRecord:
        attempt = self.repository.get_task_attempt(attempt_id)
        binding = self.repository.get_attempt_worktree_binding(attempt_id)
        if binding is None:
            raise WorktreeError("Candidate Attempt has no Worktree binding")
        binding = self.worktrees.validate_binding(binding.id)
        candidate_id = f"candidate_{uuid4().hex}"
        snapshot = self.worktrees.capture_candidate(
            binding.id, candidate_id=candidate_id
        )
        return self.repository.submit_candidate(
            attempt_id,
            candidate_id=candidate_id,
            summary=summary,
            changed_files=snapshot.changed_files,
            untracked_files=snapshot.untracked_files,
            diff_ref=snapshot.diff_ref,
            diff_hash=snapshot.diff_hash,
            base_commit=binding.base_commit,
            worktree_head=snapshot.head_commit,
            tests_reported=tests_reported,
            known_risks=known_risks,
            submitted_by=attempt.agent_id,
            command_id=command_id or f"submit-candidate:{candidate_id}",
        )

    def review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reviewed_by: str,
        reason: str,
        command_id: str,
    ) -> CandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        binding = self.repository.get_attempt_worktree_binding(candidate.attempt_id)
        if binding is None:
            raise WorktreeError("Candidate Worktree binding is missing")
        binding = self.worktrees.validate_binding(binding.id)
        if not self.worktrees.candidate_snapshot_matches(
            binding.id,
            expected_hash=candidate.diff_hash,
            expected_head=candidate.worktree_head,
        ):
            raise WorktreeError("Candidate changed after submission")
        return self.repository.decide_candidate_review(
            candidate_id,
            decision=decision,
            reviewed_by=reviewed_by,
            reason=reason,
            validated_diff_hash=candidate.diff_hash,
            command_id=command_id,
        )

    def validate_and_commit(
        self,
        candidate_id: str,
        *,
        trace_id: str | None = None,
    ) -> CandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.status is not CandidateStatus.ACCEPTED:
            raise ValueError("Lead must accept Candidate before Runtime validation")
        if (
            candidate.user_approval_required
            and candidate.user_decision != "approved"
        ):
            raise ValueError(
                "High-risk Candidate requires user approval before Runtime validation"
            )
        attempt = self.repository.get_task_attempt(candidate.attempt_id)
        binding = self.repository.get_attempt_worktree_binding(attempt.id)
        if binding is None:
            raise WorktreeError("Candidate Worktree binding is missing")
        binding = self.worktrees.validate_binding(binding.id)
        self._require_frozen_snapshot(candidate, binding.id)
        candidate = self.repository.mark_candidate_validating(candidate_id)
        task = self.repository.get_task_resource(attempt.task_list_id, attempt.task_id)
        team = self.repository.get_team_run(attempt.team_run_id)
        assert team is not None
        commands = ["git diff --check HEAD"]
        commands.extend(_string_list(team.metadata.get("mandatory_validation_commands")))
        commands.extend(_string_list(task.task.metadata.get("validation_commands")))
        commands = list(dict.fromkeys(commands))
        last_validation_id = ""
        for command in commands:
            validation = self.repository.begin_validation_run(
                candidate_id, command=command
            )
            last_validation_id = validation.id
            status, exit_code, output, duration_ms = self._run_validation(
                Path(binding.path), command
            )
            output_ref = self.worktrees.write_artifact(
                team.id, f"{validation.id}.log", output
            )
            self.repository.finish_validation_run(
                validation.id,
                status=status,
                exit_code=exit_code,
                output_ref=output_ref,
                duration_ms=duration_ms,
            )
            if status != "passed":
                return self.repository.mark_candidate_validation_failed(
                    candidate_id,
                    summary=f"Validation failed: {command}",
                    validation_run_id=validation.id,
                )
            if not self.worktrees.candidate_snapshot_matches(
                binding.id,
                expected_hash=candidate.diff_hash,
                expected_head=candidate.worktree_head,
            ):
                return self.repository.mark_candidate_validation_failed(
                    candidate_id,
                    summary=f"Validation changed the frozen Candidate: {command}",
                    validation_run_id=validation.id,
                )

        commit_validation = self.repository.begin_validation_run(
            candidate_id, command="runtime:candidate_commit"
        )
        last_validation_id = commit_validation.id
        started = time.monotonic()
        old_head = candidate.worktree_head
        try:
            commit = self.worktrees.runtime_commit_candidate(
                binding.id,
                expected_paths=tuple(
                    dict.fromkeys(
                        [*candidate.changed_files, *candidate.untracked_files]
                    )
                ),
                message=_commit_message(candidate, attempt, trace_id),
            )
        except BaseException as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            output_ref = self.worktrees.write_artifact(
                team.id,
                f"{commit_validation.id}.log",
                f"{type(exc).__name__}: {exc}",
            )
            self.repository.finish_validation_run(
                commit_validation.id,
                status="failed",
                exit_code=1,
                output_ref=output_ref,
                duration_ms=duration_ms,
            )
            if self.worktrees.current_head(binding.id) != old_head:
                self.repository.mark_candidate_commit_unknown(
                    candidate_id,
                    reason=f"Candidate commit outcome requires inspection: {exc}",
                )
                raise
            return self.repository.mark_candidate_validation_failed(
                candidate_id,
                summary=f"Runtime candidate commit failed: {exc}",
                validation_run_id=commit_validation.id,
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        output_ref = self.worktrees.write_artifact(
            team.id,
            f"{commit_validation.id}.log",
            f"Created candidate commit {commit.commit_hash}\n",
        )
        self.repository.finish_validation_run(
            commit_validation.id,
            status="passed",
            exit_code=0,
            output_ref=output_ref,
            duration_ms=duration_ms,
        )
        try:
            return self.repository.complete_candidate_commit(
                candidate_id,
                commit_hash=commit.commit_hash,
                head_commit=commit.head_commit,
                worktree_fingerprint=commit.fingerprint,
                validation_run_id=last_validation_id,
            )
        except BaseException as exc:
            self.repository.mark_candidate_commit_unknown(
                candidate_id,
                reason=f"Commit created but persistence failed: {exc}",
            )
            raise

    def _require_frozen_snapshot(
        self, candidate: CandidateRecord, worktree_id: str
    ) -> None:
        if not self.worktrees.candidate_snapshot_matches(
            worktree_id,
            expected_hash=candidate.diff_hash,
            expected_head=candidate.worktree_head,
        ):
            raise WorktreeError("Candidate changed after review")

    def _run_validation(
        self, cwd: Path, command: str
    ) -> tuple[str, int, str, int]:
        started = time.monotonic()
        argv = (
            ["git", "-C", str(cwd), "diff", "--check", "HEAD"]
            if command == "git diff --check HEAD"
            else current_runtime_platform().command_argv(command)
        )
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.validation_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return (
                "timed_out",
                124,
                output or f"Timed out after {self.validation_timeout}s",
                round((time.monotonic() - started) * 1000),
            )
        output = proc.stdout
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        return (
            "passed" if proc.returncode == 0 else "failed",
            proc.returncode,
            output or "(no output)",
            round((time.monotonic() - started) * 1000),
        )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("Validation commands must be a list")
    return [str(item) for item in value if str(item).strip()]


def _commit_message(candidate: CandidateRecord, attempt: Any, trace_id: str | None) -> str:
    subject = candidate.summary.strip().splitlines()[0][:72] or "Agent Team candidate"
    trailers = [
        f"Team-Run: {candidate.team_run_id}",
        f"Task: {candidate.task_id}",
        f"Attempt: {candidate.attempt_id}",
        f"Attempt-Ordinal: {attempt.ordinal}",
        f"Candidate: {candidate.id}",
    ]
    if trace_id:
        trailers.append(f"Trace: {trace_id}")
    return subject + "\n\n" + "\n".join(trailers)


__all__ = ["CandidateService"]
