"""Tool adapter for delegating work to a subagent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from codeagent.tools.base import ToolDefinition

SUBAGENT_TOOL_NAME = "subagent"


@dataclass(slots=True)
class SubagentTool:
    """Delegate one independent coding work unit to a child agent."""

    spawn_fn: Callable[[str], str]
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=SUBAGENT_TOOL_NAME,
            description=(
                "Delegate one independent, bounded coding work unit that needs "
                "multiple tool calls. Suitable for focused investigation, "
                "implementation, bug fixing, refactoring, or validation. The "
                "subagent has fresh context and returns only its final report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "Self-contained assignment stating the action, goal, "
                            "exact scope, constraints, completion conditions, and "
                            "validation. Explicitly request code changes when the "
                            "subagent should edit files."
                        ),
                    }
                },
                "required": ["description"],
            },
        ),
        init=False,
    )

    def run(self, description: str) -> str:
        return self.spawn_fn(description)
