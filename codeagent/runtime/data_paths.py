"""Locations for CodeAgent-owned data outside user repositories."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def default_runtime_data_dir() -> Path:
    """Return the platform default, allowing one explicit environment override."""

    configured = os.getenv("CODEAGENT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        base = (
            Path(local_app_data)
            if local_app_data
            else _home_or_temp() / "AppData" / "Local"
        )
        return (base / "CodeAgent" / "data").resolve()
    if sys.platform == "darwin":
        return (
            _home_or_temp() / "Library" / "Application Support" / "CodeAgent"
        ).resolve()
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else _home_or_temp() / ".local" / "share"
    return (base / "codeagent").resolve()


def _home_or_temp() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        return Path(tempfile.gettempdir())


@dataclass(frozen=True, slots=True)
class RuntimeDataPaths:
    """Build stable paths for one CodeAgent installation and its projects."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @classmethod
    def default(cls) -> "RuntimeDataPaths":
        return cls(default_runtime_data_dir())

    def workspace_id(self, workspace: str | Path) -> str:
        resolved = Path(workspace).expanduser().resolve()
        canonical = os.path.normcase(str(resolved))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return f"{_safe_component(resolved.name)}-{digest}"

    def workspace_dir(self, workspace: str | Path) -> Path:
        return self.root / "workspaces" / self.workspace_id(workspace)

    def memory_dir(self, workspace: str | Path) -> Path:
        return self.workspace_dir(workspace) / "memory"

    def context_dir(
        self,
        workspace: str | Path,
        *,
        conversation_id: str,
        agent_id: str,
        generation: int | None = None,
    ) -> Path:
        path = (
            self.workspace_dir(workspace)
            / "contexts"
            / _safe_component(conversation_id)
            / _safe_component(agent_id)
        )
        return path / f"g{generation}" if generation is not None else path

    def legacy_context_dir(self, workspace: str | Path, name: str) -> Path:
        return self.workspace_dir(workspace) / "legacy-context" / _safe_component(name)

    @property
    def state_database(self) -> Path:
        return self.root / "state" / "state.db"

    def worktree_root(self, configured: str | Path) -> Path:
        candidate = Path(configured).expanduser()
        return candidate.resolve() if candidate.is_absolute() else self.root / candidate

    @staticmethod
    def legacy_path(workspace: str | Path, configured: str | Path) -> Path:
        candidate = Path(configured).expanduser()
        return candidate.resolve() if candidate.is_absolute() else Path(workspace) / candidate

    @staticmethod
    def import_legacy_directory(source: Path, destination: Path) -> bool:
        """Copy one legacy directory once; never overwrite or delete the source."""

        source = source.expanduser()
        if source.is_symlink():
            return False
        source = source.resolve()
        destination = destination.expanduser().resolve()
        if destination.exists() or not source.is_dir():
            return False
        if any(path.is_symlink() for path in source.rglob("*")):
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, destination)
        except FileExistsError:
            return False
        return True


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
    return cleaned[:64] or "workspace"


__all__ = ["RuntimeDataPaths", "default_runtime_data_dir"]
