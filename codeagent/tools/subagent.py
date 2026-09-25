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
                "委派一个独立、边界明确且需要多次工具调用的编程工作单元。"
                "可用于聚焦调查、实现、修复、重构或验证。子助手使用独立上下文，只返回最终报告。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "自包含的任务说明：动作、目标、准确范围、约束、完成条件和验证方法。"
                            "允许修改文件时需明确说明。"
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
