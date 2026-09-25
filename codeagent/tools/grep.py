"""Content search tool with regex support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.workspace import WorkspaceGuard

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


@dataclass(frozen=True, slots=True)
class GrepTool:
    """Search file contents with regular expressions."""

    definition: ToolDefinition = ToolDefinition(
        name="grep",
        description=(
            "按正则搜索文件内容，返回路径、行号和匹配行。已知相关区域时用 path/include 缩小范围；结论和修改需读取周边代码。零匹配只表示本次范围未命中。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索正则表达式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索文件或目录，默认当前目录",
                },
                "include": {
                    "type": "string",
                    "description": "只搜索匹配该 glob 的文件，例如 *.py",
                },
            },
            "required": ["pattern"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(self, pattern: str, path: str = ".", include: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Invalid regex: {exc}"

        try:
            if self.workspace_guard is not None:
                base = self.workspace_guard.resolve(path)
                if include is not None:
                    include = self.workspace_guard.validate_pattern(include)
            else:
                base = Path(path).expanduser().resolve()
        except Exception as exc:
            return f"Error: {exc}"
        if not base.exists():
            return f"Error: {path} not found"

        files = [base] if base.is_file() else self._walk(base, include)
        matches: list[str] = []

        for file_path in files:
            if self.workspace_guard is not None and not self.workspace_guard.allows(
                file_path
            ):
                continue
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

    def _walk(self, root: Path, include: str | None) -> list[Path]:
        results: list[Path] = []
        for item in root.rglob(include or "*"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if self.workspace_guard is not None and not self.workspace_guard.allows(item):
                continue
            if item.is_file():
                results.append(item)
            if len(results) >= 5000:
                break
        return results
