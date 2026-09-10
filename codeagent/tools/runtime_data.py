"""Read-only access to one Agent's persisted large tool outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codeagent.tools.base import ToolDefinition


@dataclass(frozen=True, slots=True)
class LoadToolOutputTool:
    root: Path
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="load_tool_output",
            description=(
                "Read a persisted large tool result from this Agent's private "
                "runtime output directory. This tool is read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                },
                "required": ["file_path"],
            },
        ),
        init=False,
    )

    def run(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        try:
            root = self.root.expanduser().resolve()
            candidate = Path(file_path).expanduser()
            path = (
                candidate.resolve()
                if candidate.is_absolute()
                else (root / candidate).resolve()
            )
            path.relative_to(root)
            if not path.is_file():
                return f"Error: {file_path} not found"
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, int(offset) - 1)
            count = min(max(1, int(limit)), 5000)
            chunk = lines[start : start + count]
            result = "\n".join(
                f"{start + index + 1}\t{line}" for index, line in enumerate(chunk)
            )
            if len(lines) > start + count:
                result += f"\n... ({len(lines)} lines total)"
            return result or "(empty file)"
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Error: Runtime output path is not allowed: {exc}"


__all__ = ["LoadToolOutputTool"]
