"""File pattern matching tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition


@dataclass(frozen=True, slots=True)
class GlobTool:
    """Find files matching a glob pattern."""

    definition: ToolDefinition = ToolDefinition(
        name="glob",
        description=(
            "Find files matching a glob pattern. "
            "Supports ** for recursive matching (e.g. '**/*.py')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: cwd)",
                },
            },
            "required": ["pattern"],
        },
    )

    def run(self, pattern: str, path: str = ".") -> str:
        try:
            base = Path(path).expanduser().resolve()
            if not base.is_dir():
                return f"Error: {path} is not a directory"

            hits = list(base.glob(pattern))
            hits.sort(
                key=lambda candidate: (
                    candidate.stat().st_mtime if candidate.exists() else 0
                ),
                reverse=True,
            )

            total = len(hits)
            shown = hits[:100]
            result = "\n".join(str(hit) for hit in shown)

            if total > 100:
                result += f"\n... ({total} matches, showing first 100)"
            return result or "No files matched."
        except Exception as exc:
            return f"Error: {exc}"
