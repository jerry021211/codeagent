"""Project-scoped Memory write coordination for Agent Team runs."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class MemoryWriteBlocked(RuntimeError):
    """Raised when project Memory is read-only for the current runtime state."""


class MemoryAccessPolicy:
    """Guard Memory writes with a shared project lock and a live Team-state check."""

    def __init__(
        self,
        lock: threading.RLock,
        active_team: Callable[[], bool],
        *,
        always_read_only: bool = False,
    ) -> None:
        self._lock = lock
        self._active_team = active_team
        self._always_read_only = always_read_only

    @contextmanager
    def writing(self) -> Iterator[None]:
        with self._lock:
            if self._always_read_only or self._active_team():
                raise MemoryWriteBlocked(
                    "Memory is read-only while an Agent Team is active"
                )
            yield


class MemoryAccessController:
    """Share one Memory/Team creation lock for every project in this process."""

    def __init__(self, active_team: Callable[[Path], bool]) -> None:
        self._active_team = active_team
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def policy(
        self, workspace: str | Path, *, always_read_only: bool = False
    ) -> MemoryAccessPolicy:
        project = Path(workspace).expanduser().resolve()
        lock = self._project_lock(project)
        return MemoryAccessPolicy(
            lock,
            lambda: self._active_team(project),
            always_read_only=always_read_only,
        )

    @contextmanager
    def creating_team(self, workspace: str | Path) -> Iterator[None]:
        with self._project_lock(Path(workspace).expanduser().resolve()):
            yield

    def _project_lock(self, workspace: Path) -> threading.RLock:
        key = str(workspace).casefold()
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())


__all__ = [
    "MemoryAccessController",
    "MemoryAccessPolicy",
    "MemoryWriteBlocked",
]
