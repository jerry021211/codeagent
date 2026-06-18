"""Subagent task tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from codeagent.tools.base import ToolDefinition

TASK_TOOL_NAME = "task"


@dataclass(slots=True)
class TaskTool:
    """Delegate a focused task to a caller-provided subagent runner."""

    spawn_fn: Callable[[str], str]
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=TASK_TOOL_NAME,
            description=(
                "Launch a subagent to handle a focused subtask with a fresh "
                "message list. Returns only the subagent's final conclusion."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Clear, self-contained task for the subagent.",
                    }
                },
                "required": ["description"],
            },
        ),
        init=False,
    )

    def run(self, description: str) -> str:
        return self.spawn_fn(description)
