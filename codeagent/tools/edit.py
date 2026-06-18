"""Exact-match file editing tool."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition

_changed_files: set[str] = set()


@dataclass(frozen=True, slots=True)
class EditFileTool:
    """Edit a file by replacing one unique exact string."""

    definition: ToolDefinition = ToolDefinition(
        name="edit_file",
        description=(
            "Edit a file by replacing an exact string match. "
            "old_string must appear exactly once in the file for safety. "
            "Include enough surrounding context to ensure uniqueness."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must be unique in file)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    )

    def run(self, file_path: str, old_string: str, new_string: str) -> str:
        try:
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                return f"Error: {file_path} not found"
            if not path.is_file():
                return f"Error: {file_path} is a directory, not a file"

            content = path.read_text(encoding="utf-8")
            occurrences = content.count(old_string)

            if occurrences == 0:
                preview = content[:500] + ("..." if len(content) > 500 else "")
                return (
                    f"Error: old_string not found in {file_path}.\n"
                    f"File starts with:\n{preview}"
                )
            if occurrences > 1:
                return (
                    f"Error: old_string appears {occurrences} times in {file_path}. "
                    "Include more surrounding lines to make it unique."
                )

            new_content = content.replace(old_string, new_string, 1)
            path.write_text(new_content, encoding="utf-8")
            _changed_files.add(str(path))

            diff = _unified_diff(content, new_content, str(path))
            return f"Edited {file_path}\n{diff}"
        except UnicodeDecodeError as exc:
            return f"Error: unable to read {file_path} as UTF-8: {exc}"
        except Exception as exc:
            return f"Error: {exc}"


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
