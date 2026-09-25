"""Safe discovery and validation of local project workspaces."""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path


_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Makefile",
)


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    name: str
    path: str
    is_project: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "is_project": self.is_project,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    current: str
    parent: str | None
    roots: tuple[str, ...]
    entries: tuple[WorkspaceEntry, ...]


class WorkspaceCatalog:
    """Expose directory names only and reject remote/network workspaces."""

    def __init__(self, default_workspace: str | Path, *, max_entries: int = 500) -> None:
        self.default_workspace = self.resolve(default_workspace)
        self.max_entries = max(1, int(max_entries))

    def resolve(self, value: str | Path) -> Path:
        raw = str(value).strip()
        if not raw:
            raise ValueError("Workspace path cannot be empty")
        normalized = raw.replace("/", "\\") if os.name == "nt" else raw
        if os.name == "nt" and normalized.startswith("\\\\"):
            raise ValueError("Network and UNC workspaces are not supported")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Workspace path must be absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Workspace does not exist or is not accessible: {raw}") from exc
        if os.name == "nt" and str(resolved).startswith("\\\\"):
            raise ValueError("Network and UNC workspaces are not supported")
        if not resolved.is_dir():
            raise ValueError("Workspace must be an existing directory")
        return resolved

    def list(
        self, value: str | Path | None = None, *, query: str | None = None
    ) -> WorkspaceListing:
        current = self.resolve(value or self.default_workspace)
        term = ""
        if query and query.strip():
            raw = query.strip()
            normalized = raw.replace("/", "\\") if os.name == "nt" else raw
            if os.name == "nt" and normalized.startswith("\\\\"):
                raise ValueError("Network and UNC workspaces are not supported")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                if candidate.drive or candidate.root:
                    raise ValueError("Workspace path must be absolute")
                candidate = current / candidate
            if raw.endswith(("/", "\\") if os.name == "nt" else ("/",)) or candidate == candidate.parent:
                current = self.resolve(candidate)
            else:
                current = self.resolve(candidate.parent)
                term = candidate.name.casefold()
        entries: list[WorkspaceEntry] = []
        try:
            children = sorted(
                (item for item in current.iterdir() if item.is_dir() and self._match_rank(item.name, term) < 4),
                key=lambda item: (self._match_rank(item.name, term), not self._is_project(item), item.name.casefold()),
            )
        except OSError as exc:
            raise ValueError(f"Directory cannot be read: {current}") from exc
        for child in children[: self.max_entries]:
            try:
                path = child.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            entries.append(
                WorkspaceEntry(
                    name=child.name,
                    path=str(path),
                    is_project=self._is_project(path),
                )
            )
        parent = current.parent if current.parent != current else None
        return WorkspaceListing(
            current=str(current),
            parent=str(parent) if parent else None,
            roots=self._roots(current),
            entries=tuple(entries),
        )

    @staticmethod
    def _match_rank(name: str, term: str) -> int:
        """Rank exact, prefix, substring, then ordered-character matches."""
        name = name.casefold()
        if not term or name == term:
            return 0
        if name.startswith(term):
            return 1
        if term in name:
            return 2
        letters = iter(name)
        return 3 if all(character in letters for character in term) else 4

    @staticmethod
    def _is_project(path: Path) -> bool:
        return any((path / marker).exists() for marker in _PROJECT_MARKERS)

    @staticmethod
    def _roots(current: Path) -> tuple[str, ...]:
        if os.name != "nt":
            return (str(Path("/").resolve()),)
        roots: list[str] = []
        for letter in string.ascii_uppercase:
            candidate = Path(f"{letter}:\\")
            try:
                if candidate.is_dir():
                    roots.append(str(candidate))
            except OSError:
                continue
        anchor = current.anchor
        if anchor and anchor not in roots:
            roots.append(anchor)
        return tuple(roots)


__all__ = ["WorkspaceCatalog", "WorkspaceEntry", "WorkspaceListing"]
