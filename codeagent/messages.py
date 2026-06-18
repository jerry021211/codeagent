"""Message helpers shared by the Anthropic client and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

Message: TypeAlias = dict[str, Any]
ContentBlock: TypeAlias = dict[str, Any] | Any


@dataclass(frozen=True, slots=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


def _field(block: ContentBlock, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def normalize_tool_uses(content: Any) -> list[ToolUse]:
    """Return tool calls from dict blocks or SDK response objects."""

    blocks = content if isinstance(content, list) else [content]
    tool_uses: list[ToolUse] = []
    for block in blocks:
        if _field(block, "type") != "tool_use":
            continue
        tool_uses.append(
            ToolUse(
                id=str(_field(block, "id", "")),
                name=str(_field(block, "name", "")),
                input=dict(_field(block, "input", {}) or {}),
            )
        )
    return tool_uses


def extract_text(content: Any) -> str:
    """Extract human-readable text from common model response content shapes."""

    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif _field(block, "type") == "text":
            text = _field(block, "text", "")
            if text:
                parts.append(str(text))
    return "\n".join(parts)
