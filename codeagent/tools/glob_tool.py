"""File pattern matching tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class GlobTool:
    """Find files matching a glob pattern."""

    definition: ToolDefinition = ToolDefinition(
        name="glob",
        description=(
            "按 glob 模式定位路径，支持 ** 递归匹配，例如 **/*.py。未知文件路径时先定位，避免连续猜测不存在的文件。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，例如 **/*.py 或 src/**/*.ts",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录，默认当前目录",
                },
            },
            "required": ["pattern"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(self, pattern: str, path: str = ".") -> str:
        try:
            if self.workspace_guard is not None:
                base = self.workspace_guard.resolve(path)
                pattern = self.workspace_guard.validate_pattern(pattern)
            else:
                base = Path(path).expanduser().resolve()
            if not base.is_dir():
                return f"Error: {path} is not a directory"

            hits = list(base.glob(pattern))
            if self.workspace_guard is not None:
                hits = [hit for hit in hits if self.workspace_guard.allows(hit)]
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
