"""File creation and overwrite tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class WriteFileTool:
    """Create or completely overwrite a file."""

    definition: ToolDefinition = ToolDefinition(
        name="write_file",
        description=(
            "Create a new file or completely overwrite an existing one. "
            "For small edits to existing files, prefer edit_file instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path for the file",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write",
                },
            },
            "required": ["file_path", "content"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None
    changed_files: set[str] = field(default_factory=set, repr=False)

    def run(self, file_path: str, content: str) -> str:
        try:
            path = (
                self.workspace_guard.resolve(file_path)
                if self.workspace_guard is not None
                else Path(file_path).expanduser().resolve()
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.changed_files.add(str(path))
            line_count = content.count("\n")
            if content and not content.endswith("\n"):
                line_count += 1
            return f"Wrote {line_count} lines to {file_path}"
        except Exception as exc:
            return f"Error: {exc}"
