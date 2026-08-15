"""Workspace path confinement shared by filesystem-facing tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolationError(ValueError):
    """Raised when user-controlled input escapes the configured workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceGuard:
    """Resolve relative user paths without allowing workspace traversal.

    ``Path.resolve`` is intentionally applied before the containment check so an
    existing symlink or directory junction cannot redirect access outside root.
    Absolute paths and parent components are accepted only when their resolved
    target remains within the workspace, preserving normal coding-agent inputs.
    """

    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"Workspace does not exist: {self.root}")
        if not root.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.root}")
        object.__setattr__(self, "root", root)

    def resolve(self, path: str | Path, *, base: Path | None = None) -> Path:
        """Resolve one user path, raising when it is unsafe or outside root."""

        raw = Path(path).expanduser()
        anchor = self.root if base is None else self.ensure_within(base)
        candidate = raw if raw.is_absolute() else anchor / raw
        return self.ensure_within(candidate)

    def ensure_within(self, path: str | Path) -> Path:
        """Validate an internal candidate, following any existing symlinks."""

        candidate = Path(path).expanduser().resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolationError(
                f"Workspace access denied: path escapes workspace: {path}"
            ) from exc
        return candidate

    def allows(self, path: str | Path) -> bool:
        """Return whether an internally discovered path remains inside root."""

        try:
            self.ensure_within(path)
        except WorkspaceViolationError:
            return False
        return True

    def validate_pattern(self, pattern: str) -> str:
        """Validate a glob supplied by a caller without resolving wildcards."""

        raw = Path(pattern)
        if raw.is_absolute():
            raise WorkspaceViolationError(
                f"Workspace access denied: absolute patterns are not allowed: {pattern}"
            )
        if ".." in raw.parts:
            raise WorkspaceViolationError(
                f"Workspace access denied: parent traversal is not allowed: {pattern}"
            )
        return pattern
