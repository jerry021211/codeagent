"""Read-only verification for user-performed candidate integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4


class ManualIntegrationVerifier:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def verify(
        self,
        team_run_id: str,
        *,
        target_ref: str,
        verified_by: str,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        team = self.repository.get_team_run(team_run_id)
        if team is None:
            raise ValueError(f"TeamRun not found: {team_run_id}")
        conversation = self.repository.get_conversation(team.conversation_id)
        if conversation is None:
            raise ValueError("Team conversation no longer exists")
        workspace = Path(conversation.workspace).resolve()
        target_commit = self._git(workspace, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
        base_valid = self._is_ancestor(workspace, team.base_commit, target_commit)
        if not base_valid:
            raise ValueError("Integration target does not descend from Team base_commit")
        candidates = [
            item
            for item in self.repository.list_candidates(team_run_id)
            if item.commit_hash is not None
        ]
        if not candidates:
            raise ValueError("TeamRun has no candidate commits to verify")
        checks: list[dict[str, Any]] = []
        for candidate in candidates:
            assert candidate.commit_hash is not None
            ancestor = self._is_ancestor(
                workspace, candidate.commit_hash, target_commit
            )
            patch_equivalent = False
            if not ancestor:
                output = self._git(
                    workspace,
                    "cherry",
                    target_commit,
                    candidate.commit_hash,
                    f"{candidate.commit_hash}^",
                    allow_empty=True,
                )
                patch_equivalent = output.startswith("-")
            checks.append(
                {
                    "candidate_id": candidate.id,
                    "candidate_commit": candidate.commit_hash,
                    "ancestor": ancestor,
                    "patch_equivalent": patch_equivalent,
                    "integrated": ancestor or patch_equivalent,
                    "changed_files": list(candidate.changed_files),
                }
            )
        return self.repository.record_manual_integration_check(
            team_run_id,
            target_ref=target_ref,
            target_commit=target_commit,
            checks=checks,
            verified_by=verified_by,
            command_id=command_id or f"verify-integration:{team_run_id}:{uuid4().hex}",
        )

    @staticmethod
    def _is_ancestor(workspace: Path, ancestor: str, descendant: str) -> bool:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode not in {0, 1}:
            raise RuntimeError(
                proc.stderr.decode("utf-8", errors="replace").strip()
                or "Git ancestry check failed"
            )
        return proc.returncode == 0

    @staticmethod
    def _git(
        workspace: Path, *args: str, allow_empty: bool = False
    ) -> str:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "Git command failed")
        output = proc.stdout.strip()
        if not output and not allow_empty:
            raise RuntimeError("Git command returned an empty result")
        return output


__all__ = ["ManualIntegrationVerifier"]
