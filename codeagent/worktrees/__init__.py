"""Worktree isolation extension point."""

from codeagent.worktrees.manager import (
    CandidateSnapshot,
    DirtyWorkspaceConfirmationRequired,
    GitBaselineInspection,
    RuntimeCommitResult,
    WorktreeError,
    WorktreeManager,
    WorktreeManagerRegistry,
)

__all__ = [
    "DirtyWorkspaceConfirmationRequired",
    "CandidateSnapshot",
    "GitBaselineInspection",
    "RuntimeCommitResult",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeManagerRegistry",
]
