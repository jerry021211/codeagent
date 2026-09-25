"""File reading tool with line numbers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class ReadFileTool:
    """Read a file's contents with 1-based line numbers."""

    definition: ToolDefinition = ToolDefinition(
        name="read_file",
        description=(
            "读取文件并返回行号；修改前读取相关上下文。用 offset/limit 限定行段，截断时按需继续读取。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号，从 1 开始；默认 1。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取行数，默认 2000。",
                },
            },
            "required": ["file_path"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        try:
            path = (
                self.workspace_guard.resolve(file_path)
                if self.workspace_guard is not None
                else Path(file_path).expanduser().resolve()
            )
            if not path.exists():
                return f"Error: {file_path} not found"
            if not path.is_file():
                return f"Error: {file_path} is a directory, not a file"

            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            total = len(lines)

            start = max(0, offset)
            chunk = lines[start : start + limit]
            numbered = [f"{start + index + 1}\t{line}" for index, line in enumerate(chunk)]
            result = "\n".join(numbered)

            if total > start + limit:
                result += (
                    f"\n... ({total} lines total, "
                    f"showing {start + 1}-{start + len(chunk)})"
                )
            return result or "(empty file)"
        except Exception as exc:
            return f"Error: {exc}"
