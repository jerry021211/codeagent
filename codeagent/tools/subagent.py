"""Tool adapter for delegating work to a subagent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from codeagent.tools.base import ToolDefinition

SUBAGENT_TOOL_NAME = "subagent"


@dataclass(slots=True)
class SubagentTool:
    """Delegate focused work to a caller-provided subagent runner."""

    spawn_fn: Callable[[str], str]
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=SUBAGENT_TOOL_NAME,
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
