"""Git Worktree ownership for first-phase Agent Team code Attempts."""

from __future__ import annotations

import hashlib
import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeagent.teams.models import TaskAttemptRecord, WorktreeBindingRecord


class WorktreeError(RuntimeError):
    """Raised when a Worktree cannot be created or its binding is invalid."""


class DirtyWorkspaceConfirmationRequired(WorktreeError):
    """Raised when uncommitted source changes have not been acknowledged."""


@dataclass(frozen=True, slots=True)
class GitBaselineInspection:
    repository_root: Path
    base_commit: str
    source_head: str
    source_dirty: bool
    status_text: str
    status_hash: str


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    changed_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    diff_ref: str
    diff_hash: str
    head_commit: str


@dataclass(frozen=True, slots=True)
class RuntimeCommitResult:
    commit_hash: str
    head_commit: str
    fingerprint: str


class WorktreeManager:
    """Create and validate one retained Git Worktree per code Attempt.

    This class deliberately has no merge, rebase, cherry-pick, cleanup, or push
    methods.  Those operations are outside the first delivery.
    """

    def __init__(
        self,
        repository: Any,
        source_workspace: str | Path,
        managed_root: str | Path,
    ) -> None:
        self.repository = repository
        self.source_workspace = Path(source_workspace).expanduser().resolve()
        self.managed_root = Path(managed_root).expanduser().resolve()
        repository_root = Path(
            self._git(self.source_workspace, "rev-parse", "--show-toplevel")
        ).resolve()
        if repository_root != self.source_workspace:
            raise WorktreeError(
                "Agent Team source workspace must be the Git repository root"
            )
        try:
            self.managed_root.relative_to(self.source_workspace)
        except ValueError:
            pass
        else:
            raise WorktreeError("Managed Worktree root must be outside source workspace")

    def inspect_baseline(self, base_commit: str) -> GitBaselineInspection:
        canonical_base = self._git(
            self.source_workspace, "rev-parse", "--verify", f"{base_commit}^{{commit}}"
        )
        source_head = self._git(self.source_workspace, "rev-parse", "HEAD")
        status_text = self._git(
            self.source_workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            allow_empty=True,
        )
        return GitBaselineInspection(
            repository_root=self.source_workspace,
            base_commit=canonical_base,
            source_head=source_head,
            source_dirty=bool(status_text),
            status_text=status_text,
            status_hash=hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        )

    def confirm_baseline(
        self,
        team_run_id: str,
        *,
        confirmed_by: str,
        allow_dirty: bool,
        command_id: str,
    ) -> GitBaselineInspection:
        team = self.repository.get_team_run(team_run_id)
        if team is None:
            raise WorktreeError(f"TeamRun not found: {team_run_id}")
        inspection = self.inspect_baseline(team.base_commit)
        if inspection.source_dirty and not allow_dirty:
            raise DirtyWorkspaceConfirmationRequired(
                "Source workspace has uncommitted changes; they will not be copied "
                "into Team Worktrees"
            )
        self.repository.record_team_base_confirmation(
            team_run_id,
            base_commit=inspection.base_commit,
            source_head=inspection.source_head,
            source_dirty=inspection.source_dirty,
            status_hash=inspection.status_hash,
            confirmed_by=confirmed_by,
            command_id=command_id,
        )
        return inspection

    def baseline_is_confirmed(self, team_run_id: str) -> bool:
        team = self.repository.get_team_run(team_run_id)
        if team is None:
            return False
        try:
            inspection = self.inspect_baseline(team.base_commit)
        except WorktreeError:
            return False
        confirmation = self.repository.get_team_base_confirmation(team_run_id)
        return bool(
            confirmation
            and confirmation["base_commit"] == inspection.base_commit
            and confirmation["source_head"] == inspection.source_head
            and confirmation["source_dirty"] == inspection.source_dirty
            and confirmation["status_hash"] == inspection.status_hash
        )

    def ensure_baseline_ready(self, team_run_id: str) -> GitBaselineInspection:
        """Auto-confirm a clean snapshot; require the user for a dirty one."""

        team = self.repository.get_team_run(team_run_id)
        if team is None:
            raise WorktreeError(f"TeamRun not found: {team_run_id}")
        inspection = self.inspect_baseline(team.base_commit)
        if self.baseline_is_confirmed(team_run_id):
            return inspection
        if inspection.source_dirty:
            raise DirtyWorkspaceConfirmationRequired(
                "Dirty source workspace requires explicit confirmation"
            )
        return self.confirm_baseline(
            team_run_id,
            confirmed_by="runtime",
            allow_dirty=False,
            command_id=f"confirm-clean-base:{team_run_id}:{inspection.status_hash}",
        )

    def create_for_attempt(self, attempt: TaskAttemptRecord) -> WorktreeBindingRecord:
        existing = self.repository.get_attempt_worktree_binding(attempt.id)
        if existing is not None:
            return self.validate_binding(existing.id)
        team = self.repository.get_team_run(attempt.team_run_id)
        if team is None:
            raise WorktreeError(f"TeamRun not found: {attempt.team_run_id}")
        inspection = self.inspect_baseline(attempt.attempt_base_commit)
        self.ensure_baseline_ready(team.id)
        if not self.baseline_is_confirmed(team.id):
            raise DirtyWorkspaceConfirmationRequired(
                "Source workspace changed after baseline confirmation"
            )
        task = self.repository.get_task_resource(attempt.task_list_id, attempt.task_id)
        scopes = tuple(str(item) for item in task.task.metadata.get("write_scopes", []))
        session = self.repository.get_agent_session(attempt.session_id)
        branch = self._branch_name(attempt)
        path = (
            self.managed_root
            / _safe_component(team.id)
            / f"task-{_safe_component(attempt.task_id)}-a{attempt.ordinal}"
        ).resolve()
        if path.exists():
            raise WorktreeError(f"Managed Worktree path already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            self.source_workspace,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            inspection.base_commit,
        )
        head = self._git(path, "rev-parse", "HEAD")
        fingerprint = self._fingerprint(
            path=path,
            branch=branch,
            head=head,
            generation=session.generation,
        )
        return self.repository.create_worktree_binding(
            attempt.id,
            path=str(path),
            branch=branch,
            base_commit=inspection.base_commit,
            head_commit=head,
            fingerprint=fingerprint,
            generation=session.generation,
            write_scopes=scopes,
            command_id=f"bind-worktree:{attempt.id}:{session.generation}",
        )

    def validate_binding(self, worktree_id: str) -> WorktreeBindingRecord:
        binding = self.repository.get_worktree_binding(worktree_id)
        if binding.state != "active":
            raise WorktreeError(f"Worktree binding is not active: {binding.state}")
        return self._validate_binding_record(binding)

    def validate_recoverable_binding(
        self, worktree_id: str
    ) -> WorktreeBindingRecord:
        """Validate a retained execution directory without reopening writes."""

        binding = self.repository.get_worktree_binding(worktree_id)
        if binding.state not in {"active", "frozen", "orphaned"}:
            raise WorktreeError(
                f"Worktree binding is not recoverable: {binding.state}"
            )
        return self._validate_binding_record(binding)

    def _validate_binding_record(
        self, binding: WorktreeBindingRecord
    ) -> WorktreeBindingRecord:
        path = Path(binding.path).resolve()
        try:
            path.relative_to(self.managed_root)
        except ValueError as exc:
            raise WorktreeError("Worktree binding escapes managed root") from exc
        if not path.is_dir():
            raise WorktreeError(f"Bound Worktree is missing: {path}")
        top_level = Path(self._git(path, "rev-parse", "--show-toplevel")).resolve()
        try:
            branch = self._git(path, "symbolic-ref", "--short", "HEAD")
        except WorktreeError as exc:
            raise WorktreeError(
                "Worktree branch no longer matches binding: detached HEAD"
            ) from exc
        head = self._git(path, "rev-parse", "HEAD")
        session = self.repository.get_agent_session(binding.session_id)
        fingerprint = self._fingerprint(
            path=path,
            branch=branch,
            head=head,
            generation=session.generation,
        )
        if top_level != path:
            raise WorktreeError("Git top-level no longer matches Worktree binding")
        if branch != binding.branch:
            raise WorktreeError("Worktree branch no longer matches binding")
        if head != binding.head_commit:
            raise WorktreeError("Worktree HEAD no longer matches binding")
        if session.generation != binding.generation:
            raise WorktreeError("Worktree session generation is stale")
        if fingerprint != binding.fingerprint:
            raise WorktreeError("Worktree fingerprint no longer matches binding")
        return binding

    def changed_paths(self, worktree_id: str) -> tuple[str, ...]:
        return tuple(path for _status, path in self.status_entries(worktree_id))

    def recovery_changed_paths(self, worktree_id: str) -> tuple[str, ...]:
        binding = self.validate_recoverable_binding(worktree_id)
        return tuple(path for _status, path in self._status_entries(binding))

    def recovery_fingerprint(self, worktree_id: str, generation: int) -> str:
        """Bind a validated retained Worktree to a replacement Session generation."""

        binding = self.validate_recoverable_binding(worktree_id)
        return self._fingerprint(
            path=Path(binding.path).resolve(),
            branch=binding.branch,
            head=binding.head_commit,
            generation=int(generation),
        )

    def status_entries(self, worktree_id: str) -> tuple[tuple[str, str], ...]:
        binding = self.validate_binding(worktree_id)
        return self._status_entries(binding)

    def _status_entries(
        self, binding: WorktreeBindingRecord
    ) -> tuple[tuple[str, str], ...]:
        raw = self._git_bytes(
            Path(binding.path),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        entries = raw.split(b"\0")
        paths: list[tuple[str, str]] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            text = entry.decode("utf-8", errors="surrogateescape")
            status = text[:2]
            path = text[3:]
            if "R" in status or "C" in status:
                if index < len(entries) and entries[index]:
                    index += 1
            paths.append((status, path.replace("\\", "/")))
        return tuple(dict.fromkeys(paths))

    def capture_candidate(
        self, worktree_id: str, *, candidate_id: str
    ) -> CandidateSnapshot:
        binding = self.validate_binding(worktree_id)
        path = Path(binding.path)
        entries = self.status_entries(worktree_id)
        if not entries:
            raise WorktreeError("Candidate has no repository changes")
        changed = tuple(item for status, item in entries if status != "??")
        untracked = tuple(item for status, item in entries if status == "??")
        digest = self._snapshot_hash(path, entries)
        artifact_dir = (
            self.managed_root / _safe_component(binding.team_run_id) / "artifacts"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = (artifact_dir / f"{_safe_component(candidate_id)}.patch").resolve()
        artifact_path.write_text(
            self._candidate_patch(path, untracked), encoding="utf-8"
        )
        return CandidateSnapshot(
            changed_files=changed,
            untracked_files=untracked,
            diff_ref=str(artifact_path),
            diff_hash=digest,
            head_commit=self._git(path, "rev-parse", "HEAD"),
        )

    def candidate_snapshot_matches(
        self,
        worktree_id: str,
        *,
        expected_hash: str,
        expected_head: str,
    ) -> bool:
        binding = self.validate_binding(worktree_id)
        entries = self.status_entries(worktree_id)
        current_head = self._git(Path(binding.path), "rev-parse", "HEAD")
        return (
            current_head == expected_head
            and self._snapshot_hash(Path(binding.path), entries) == expected_hash
        )

    def runtime_commit_candidate(
        self,
        worktree_id: str,
        *,
        expected_paths: tuple[str, ...],
        message: str,
    ) -> RuntimeCommitResult:
        """Create one Runtime-owned candidate commit without integrating it."""

        binding = self.validate_binding(worktree_id)
        path = Path(binding.path)
        current_paths = tuple(sorted(self.changed_paths(worktree_id)))
        if current_paths != tuple(sorted(expected_paths)):
            raise WorktreeError("Candidate paths changed before Runtime commit")
        self._git(path, "diff", "--check", "HEAD", allow_empty=True)
        self._git(path, "add", "-A", "--", ".", allow_empty=True)
        staged = tuple(
            item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            for item in self._git_bytes(
                path, "diff", "--cached", "--name-only", "-z"
            ).split(b"\0")
            if item
        )
        if tuple(sorted(staged)) != current_paths:
            raise WorktreeError("Runtime staging set differs from frozen Candidate")
        self._git(
            path,
            "-c",
            "user.name=CodeAgent Runtime",
            "-c",
            "user.email=runtime@codeagent.invalid",
            "commit",
            "-m",
            message,
        )
        head = self._git(path, "rev-parse", "HEAD")
        session = self.repository.get_agent_session(binding.session_id)
        return RuntimeCommitResult(
            commit_hash=head,
            head_commit=head,
            fingerprint=self._fingerprint(
                path=path,
                branch=binding.branch,
                head=head,
                generation=session.generation,
            ),
        )

    def current_head(self, worktree_id: str) -> str:
        """Read HEAD without accepting a changed binding as valid."""

        binding = self.repository.get_worktree_binding(worktree_id)
        path = Path(binding.path).resolve()
        try:
            path.relative_to(self.managed_root)
        except ValueError as exc:
            raise WorktreeError("Worktree binding escapes managed root") from exc
        return self._git(path, "rev-parse", "HEAD")

    def write_artifact(self, team_run_id: str, name: str, content: str) -> str:
        artifact_dir = (
            self.managed_root / _safe_component(team_run_id) / "artifacts"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = (artifact_dir / _safe_component(name)).resolve()
        path.write_text(content, encoding="utf-8")
        return str(path)

    def cleanup_retained(self, worktree_id: str) -> WorktreeBindingRecord:
        """Remove a clean retained directory; keep its candidate branch and commit."""

        binding = self.repository.get_worktree_binding(worktree_id)
        if binding.state == "cleaned":
            return binding
        if binding.state != "retained":
            raise WorktreeError("Only retained candidate Worktrees can be cleaned")
        path = Path(binding.path).resolve()
        try:
            path.relative_to(self.managed_root)
        except ValueError as exc:
            raise WorktreeError("Worktree cleanup target escapes managed root") from exc
        if self._git(path, "status", "--porcelain", allow_empty=True):
            raise WorktreeError("Retained Worktree is not clean; cleanup refused")
        if self._git(path, "rev-parse", "HEAD") != binding.head_commit:
            raise WorktreeError("Retained Worktree HEAD changed; cleanup refused")
        self._git(
            self.source_workspace,
            "worktree",
            "remove",
            str(path),
            allow_empty=True,
        )
        return self.repository.mark_worktree_cleaned(worktree_id)

    def _snapshot_hash(
        self, path: Path, entries: tuple[tuple[str, str], ...]
    ) -> str:
        digest = hashlib.sha256()
        for status, relative in sorted(entries, key=lambda item: item[1]):
            target = (path / relative).resolve()
            try:
                target.relative_to(path.resolve())
            except ValueError as exc:
                raise WorktreeError(
                    f"Candidate path escapes Worktree: {relative}"
                ) from exc
            digest.update(status.encode("ascii", errors="replace"))
            digest.update(b"\0")
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if target.is_symlink():
                digest.update(b"symlink:")
                digest.update(str(target.readlink()).encode("utf-8"))
            elif target.is_file():
                digest.update(target.read_bytes())
            else:
                digest.update(b"deleted")
            digest.update(b"\0")
        return digest.hexdigest()

    def _candidate_patch(self, path: Path, untracked: tuple[str, ...]) -> str:
        tracked = self._git(
            path, "diff", "--binary", "--no-ext-diff", "HEAD", allow_empty=True
        )
        sections = [tracked] if tracked else []
        for relative in untracked:
            target = (path / relative).resolve()
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                sections.append(
                    f"Binary untracked file {relative} "
                    f"sha256={hashlib.sha256(target.read_bytes()).hexdigest()}"
                )
                continue
            diff = difflib.unified_diff(
                [],
                content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{relative}",
            )
            sections.append("".join(diff))
        return "\n".join(sections)

    def _fingerprint(
        self,
        *,
        path: Path,
        branch: str,
        head: str,
        generation: int,
    ) -> str:
        git_dir = Path(self._git(path, "rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = (path / git_dir).resolve()
        payload = "\n".join(
            [str(path.resolve()), str(git_dir.resolve()), branch, head, str(generation)]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _branch_name(attempt: TaskAttemptRecord) -> str:
        return (
            f"codex/{_safe_component(attempt.team_run_id)}/"
            f"task-{_safe_component(attempt.task_id)}-a{attempt.ordinal}"
        )

    @staticmethod
    def _git(cwd: Path, *args: str, allow_empty: bool = False) -> str:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise WorktreeError(proc.stderr.strip() or "Git command failed")
        value = proc.stdout.strip()
        if not value and not allow_empty:
            raise WorktreeError("Git command returned an empty result")
        return value

    @staticmethod
    def _git_bytes(cwd: Path, *args: str) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise WorktreeError(
                proc.stderr.decode("utf-8", errors="replace").strip()
                or "Git command failed"
            )
        return proc.stdout


def _safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
    return result[:48] or "item"


class WorktreeManagerRegistry:
    """Route Team operations to the manager for each conversation workspace."""

    def __init__(self, repository: Any, configured_root: str | Path) -> None:
        self.repository = repository
        self.configured_root = Path(configured_root).expanduser()
        self._managers: dict[str, WorktreeManager] = {}

    def for_team(self, team_run_id: str) -> WorktreeManager:
        team = self.repository.get_team_run(team_run_id)
        if team is None:
            raise WorktreeError(f"TeamRun not found: {team_run_id}")
        conversation = self.repository.get_conversation(team.conversation_id)
        if conversation is None:
            raise WorktreeError("Team conversation no longer exists")
        return self.for_workspace(conversation.workspace)

    def for_workspace(self, workspace: str | Path) -> WorktreeManager:
        source = Path(workspace).expanduser().resolve()
        key = str(source)
        manager = self._managers.get(key)
        if manager is None:
            suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
            root = (
                self.configured_root.resolve()
                / f"{_safe_component(source.name)}-{suffix}"
                if self.configured_root.is_absolute()
                else source.parent
                / self.configured_root
                / f"{_safe_component(source.name)}-{suffix}"
            )
            manager = WorktreeManager(self.repository, source, root)
            self._managers[key] = manager
        return manager

    def ensure_baseline_ready(self, team_run_id: str) -> GitBaselineInspection:
        return self.for_team(team_run_id).ensure_baseline_ready(team_run_id)

    def confirm_baseline(self, team_run_id: str, **kwargs: Any) -> GitBaselineInspection:
        return self.for_team(team_run_id).confirm_baseline(team_run_id, **kwargs)

    def create_for_attempt(self, attempt: TaskAttemptRecord) -> WorktreeBindingRecord:
        return self.for_team(attempt.team_run_id).create_for_attempt(attempt)

    def validate_binding(self, worktree_id: str) -> WorktreeBindingRecord:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).validate_binding(worktree_id)

    def validate_recoverable_binding(
        self, worktree_id: str
    ) -> WorktreeBindingRecord:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).validate_recoverable_binding(
            worktree_id
        )

    def changed_paths(self, worktree_id: str) -> tuple[str, ...]:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).changed_paths(worktree_id)

    def recovery_changed_paths(self, worktree_id: str) -> tuple[str, ...]:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).recovery_changed_paths(worktree_id)

    def recovery_fingerprint(self, worktree_id: str, generation: int) -> str:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).recovery_fingerprint(
            worktree_id, generation
        )

    def capture_candidate(self, worktree_id: str, **kwargs: Any) -> CandidateSnapshot:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).capture_candidate(
            worktree_id, **kwargs
        )

    def candidate_snapshot_matches(self, worktree_id: str, **kwargs: Any) -> bool:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).candidate_snapshot_matches(
            worktree_id, **kwargs
        )

    def runtime_commit_candidate(
        self, worktree_id: str, **kwargs: Any
    ) -> RuntimeCommitResult:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).runtime_commit_candidate(
            worktree_id, **kwargs
        )

    def current_head(self, worktree_id: str) -> str:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).current_head(worktree_id)

    def write_artifact(self, team_run_id: str, name: str, content: str) -> str:
        return self.for_team(team_run_id).write_artifact(team_run_id, name, content)

    def cleanup_retained(self, worktree_id: str) -> WorktreeBindingRecord:
        binding = self.repository.get_worktree_binding(worktree_id)
        return self.for_team(binding.team_run_id).cleanup_retained(worktree_id)


__all__ = [
    "DirtyWorkspaceConfirmationRequired",
    "CandidateSnapshot",
    "GitBaselineInspection",
    "RuntimeCommitResult",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeManagerRegistry",
]
