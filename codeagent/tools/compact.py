"""Manual context compaction tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from codeagent.tools.base import ToolDefinition

COMPACT_TOOL_NAME = "compact"


@dataclass(slots=True)
class CompactTool:
    """Let the model request explicit context compaction."""

    compact_fn: Callable[[], str]
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=COMPACT_TOOL_NAME,
            description=(
                "历史过长或噪声过多时请求压缩；返回请求状态，实际压缩在下一次模型调用前完成。"
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
        ),
        init=False,
    )

    def run(self) -> str:
        return self.compact_fn()
