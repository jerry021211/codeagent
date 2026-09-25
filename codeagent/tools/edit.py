"""Exact-match file editing tool."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeagent.tools.base import (
    ToolDefinition, ToolInputState, ToolOutput, parameter_error, validate_tool_arguments,
)
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class EditFileTool:
    """Edit a file by replacing one unique exact string."""

    definition: ToolDefinition = ToolDefinition(
        name="edit_file",
        description=(
            "按唯一精确文本匹配修改文件。old_string 必须恰好出现一次，提供足够上下文。匹配失败时重新读取当前文本后调整，不原样重试。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "待修改文件路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的原文，必须在文件中唯一",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None
    changed_files: set[str] = field(default_factory=set, repr=False)

    def parameter_error(self, args: dict[str, Any]) -> ToolOutput | None:
        if isinstance(args, dict) and ("old_string" not in args or args["old_string"] == ""):
            return parameter_error(
                "old_string must contain non-empty original text; read the file and supply a unique match.",
                "edit:empty_old_string",
            )
        invalid = validate_tool_arguments(self.definition, args)
        if invalid is not None:
            return invalid
        if args["old_string"] == args["new_string"]:
            return parameter_error(
                "old_string and new_string are identical; supply a different replacement or skip this edit.",
                "edit:unchanged_replacement",
            )
        return None

    def _resolve(self, file_path: str) -> Path:
        return (
            self.workspace_guard.resolve(file_path)
            if self.workspace_guard is not None
            else Path(file_path).expanduser().resolve()
        )

    def _state(self, path: Path, data: bytes) -> ToolInputState:
        fingerprint = hashlib.sha256(str(path).encode("utf-8") + b"\0" + data).hexdigest()
        cwd = self.workspace_guard.root if self.workspace_guard is not None else Path.cwd()
        return ToolInputState(fingerprint=fingerprint, known=True, cwd=str(cwd))

    def input_state(self, args: dict[str, Any]) -> ToolInputState:
        cwd = self.workspace_guard.root if self.workspace_guard is not None else Path.cwd()
        try:
            path = self._resolve(args["file_path"])
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                return self._state(path, b"missing")
            if stat.S_ISDIR(mode):
                return self._state(path, b"directory")
            if stat.S_ISREG(mode):
                return self._state(path, b"file\0" + path.read_bytes())
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return ToolInputState(cwd=str(cwd))

    def run(
        self, file_path: str | None = None, old_string: str | None = None,
        new_string: str | None = None,
    ) -> ToolOutput:
        from codeagent.context.projection import is_projection_placeholder

        invalid = self.parameter_error({
            name: value for name, value in {
                "file_path": file_path, "old_string": old_string, "new_string": new_string,
            }.items() if value is not None
        })
        if invalid is not None:
            return invalid
        if is_projection_placeholder(old_string) or is_projection_placeholder(new_string):
            return parameter_error(
                "历史上下文投影占位符不能写入磁盘；请读取原文并提供真实替换内容。",
                "edit:projection_placeholder",
            )
        try:
            path = self._resolve(file_path)
            if not path.exists():
                return ToolOutput(f"Error: {file_path} not found", status="error")
            if not path.is_file():
                return ToolOutput(f"Error: {file_path} is a directory, not a file", status="error")

            original = path.read_bytes()
            state = self._state(path, b"file\0" + original)
            content = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            occurrences = content.count(old_string)

            if occurrences == 0:
                preview = content[:500] + ("..." if len(content) > 500 else "")
                return ToolOutput(
                    f"Error: old_string not found in {file_path}.\n"
                    f"Read the current file and adjust old_string.\nFile starts with:\n{preview}",
                    status="error", outcome="diagnostic", deterministic=True,
                    input_state=state.fingerprint, state_known=state.known,
                    result_signature="edit:old_string_not_found",
                )
            if occurrences > 1:
                return ToolOutput(
                    f"Error: old_string appears {occurrences} times in {file_path}. "
                    "Include more surrounding lines to make it unique.",
                    status="error", outcome="diagnostic", deterministic=True,
                    input_state=state.fingerprint, state_known=state.known,
                    result_signature="edit:ambiguous_old_string",
                )

            new_content = content.replace(old_string, new_string, 1)
            if new_content.replace("\n", os.linesep).encode("utf-8") == original:
                return parameter_error(
                    "The replacement leaves file content unchanged; supply a different replacement or skip this edit.",
                    "edit:unchanged_replacement",
                )
            path.write_text(new_content, encoding="utf-8")
            self.changed_files.add(str(path))

            diff = _unified_diff(content, new_content, str(path))
            return ToolOutput(
                f"Edited {file_path}\n{diff}", changed_files=(str(path),),
                input_state=state.fingerprint, state_known=state.known,
            )
        except UnicodeDecodeError as exc:
            return ToolOutput(f"Error: unable to read {file_path} as UTF-8: {exc}", status="error")
        except Exception as exc:
            return ToolOutput(f"Error: {exc}", status="error")


def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    result = "".join(diff)
    if len(result) > 3000:
        result = result[:2500] + "\n... (diff truncated)\n"
    return result
