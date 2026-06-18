"""Content search tool with regex support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


@dataclass(frozen=True, slots=True)
class GrepTool:
    """Search file contents with regular expressions."""

    definition: ToolDefinition = ToolDefinition(
        name="grep",
        description=(
            "Search file contents with regex. "
            "Returns matching lines with file path and line number."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: cwd)",
                },
                "include": {
                    "type": "string",
                    "description": "Only search files matching this glob (e.g. '*.py')",
                },
            },
            "required": ["pattern"],
        },
    )

    def run(self, pattern: str, path: str = ".", include: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Invalid regex: {exc}"

        base = Path(path).expanduser().resolve()
        if not base.exists():
            return f"Error: {path} not found"

        files = [base] if base.is_file() else self._walk(base, include)
        matches: list[str] = []

        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{file_path}:{line_number}: {line.rstrip()}")
                    if len(matches) >= 200:
                        matches.append("... (200 match limit reached)")
                        return "\n".join(matches)

        return "\n".join(matches) if matches else "No matches found."

    @staticmethod
    def _walk(root: Path, include: str | None) -> list[Path]:
        results: list[Path] = []
        for item in root.rglob(include or "*"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.is_file():
                results.append(item)
            if len(results) >= 5000:
                break
        return results
