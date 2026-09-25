"""File creation and overwrite tool."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from codeagent.tools.base import ToolDefinition, ToolOutput, parameter_error, validate_tool_arguments
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class WriteFileTool:
    """Create or completely overwrite a file."""

    definition: ToolDefinition = ToolDefinition(
        name="write_file",
        description=(
            "创建文件或完整覆盖已有文件。已有文件的小改动优先 edit_file；完整覆盖前检查原内容，保留用户无关修改。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容",
                },
            },
            "required": ["file_path", "content"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None
    changed_files: set[str] = field(default_factory=set, repr=False)

    def run(self, file_path: str | None = None, content: str | None = None) -> ToolOutput:
        # Import lazily: context setup also imports the built-in tool registry.
        from codeagent.context.projection import is_projection_placeholder

        invalid = validate_tool_arguments(self.definition, {
            name: value for name, value in {"file_path": file_path, "content": content}.items()
            if value is not None
        })
        if invalid is not None:
            return invalid
        if is_projection_placeholder(content):
            return parameter_error(
                "历史上下文投影占位符不能写入磁盘；请提供真实完整正文。",
                "write:projection_placeholder",
            )
        try:
            path = (
                self.workspace_guard.resolve(file_path)
                if self.workspace_guard is not None
                else Path(file_path).expanduser().resolve()
            )
            # Match write_text's platform newline translation when comparing bytes.
            desired = content.replace("\n", os.linesep).encode("utf-8")
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                original = None
            changed = original != desired
            if changed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                self.changed_files.add(str(path))
            line_count = content.count("\n")
            if content and not content.endswith("\n"):
                line_count += 1
            return ToolOutput(
                f"Wrote {line_count} lines to {file_path}",
                changed_files=(str(path),) if changed else (),
            )
        except Exception as exc:
            return ToolOutput(f"Error: {exc}", status="error")
