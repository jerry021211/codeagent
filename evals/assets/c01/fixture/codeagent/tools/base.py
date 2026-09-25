"""Tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Tool(Protocol):
    definition: ToolDefinition

    def run(self, **kwargs: Any) -> str:
        raise NotImplementedError


class ToolOutput(str):
    """String-compatible result carrying execution facts before text projection."""

    def __new__(cls, text: str, *, status: str = "success", exit_code: int | None = None):
        if status not in {"success", "error", "blocked"}:
            raise ValueError(f"Invalid tool status: {status}")
        result = super().__new__(cls, text)
        result.status = status
        result.exit_code = exit_code
        return result


def normalize_tool_output(value: str) -> ToolOutput:
    """Bridge legacy tools; explicit facts always override text heuristics."""
    if isinstance(value, ToolOutput):
        return value
    text = str(value)
    if text.startswith(("Blocked:", "Permission denied")):
        status = "blocked"
    elif text.startswith(("Error:", "Error running command:", "Unknown tool:", "Invalid regex:")):
        status = "error"
    else:
        status = "success"
    return ToolOutput(text, status=status)
