"""Markdown template loading for prompt fragments."""

from __future__ import annotations

from pathlib import Path


class PromptTemplateLoader:
    """Load prompt templates from project overrides or built-in defaults."""

    def __init__(self, *, workspace: Path, template_dir: Path | None = None) -> None:
        self.workspace = workspace
        self.override_dir = template_dir
        self.builtin_dir = Path(__file__).with_name("templates")

    def load(self, name: str) -> str:
        filename = f"{name}.md"
        for directory in self._candidate_dirs():
            path = directory / filename
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        return ""

    def _candidate_dirs(self) -> list[Path]:
        candidates: list[Path] = []
        if self.override_dir is not None:
            override = self.override_dir
            if not override.is_absolute():
                override = self.workspace / override
            candidates.append(override)
        candidates.append(self.workspace / ".prompts")
        candidates.append(self.builtin_dir)
        return candidates
