"""Message helpers shared by the Anthropic client and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
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
                id=str(_field(block, "id", "") or ""),
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


def reconcile_tool_history(
    messages: list[Message], *, repair_missing: bool = False
) -> tuple[list[Message], list[str]]:
    """Validate adjacency and pairing; only legacy missing results are repairable.

    Never infer success or replay a tool. The input checkpoint is left untouched.
    Ambiguous IDs and orphan results fail closed rather than losing information.
    """
    history = deepcopy(messages) if repair_missing else messages
    repaired: list[str] = []
    index = 0
    while index < len(history):
        message = history[index]
        blocks = message.get("content", [])
        blocks = blocks if isinstance(blocks, list) else []
        if any(_field(block, "type") == "tool_result" for block in blocks):
            raise ValueError("工具历史损坏：存在没有对应调用的 tool_result")
        calls = normalize_tool_uses(blocks)
        if not calls:
            index += 1
            continue
        ids = [call.id for call in calls]
        if message.get("role") != "assistant" or any(not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("工具历史损坏：tool_use 的角色或 ID 无效/重复")
        following = history[index + 1] if index + 1 < len(history) else None
        content = following.get("content", []) if following else []
        results = [block for block in content if _field(block, "type") == "tool_result"] if isinstance(content, list) else []
        result_ids = [_field(block, "tool_use_id") for block in results]
        if results and (following.get("role") != "user" or len(set(result_ids)) != len(result_ids) or not set(result_ids).issubset(ids)):
            raise ValueError("工具历史损坏：tool_result 的角色或 ID 无效/重复")
        if results and normalize_tool_uses(content):
            raise ValueError("工具历史损坏：结果消息中存在 tool_use")
        if results and any(_field(block, "type") != "tool_result" for block in content[:len(results)]):
            if not repair_missing:
                raise ValueError("工具历史损坏：tool_result 必须出现在结果消息的开头")
            following["content"] = content = results + [block for block in content if _field(block, "type") != "tool_result"]
        missing = [value for value in ids if value not in result_ids]
        if missing:
            if not repair_missing:
                raise ValueError("工具历史不完整：缺少 tool_result: " + ", ".join(missing))
            additions = [{
                "type": "tool_result", "tool_use_id": value, "is_error": True,
                "content": "历史运行中断，未保存此工具的执行结果。结果未知，操作可能已产生副作用；请先核实实际状态，不要据此认定成功或自动重复执行。",
            } for value in missing]
            if results:
                following["content"] = results + additions + [block for block in content if _field(block, "type") != "tool_result"]
            else:
                history.insert(index + 1, {"role": "user", "content": additions})
            repaired.extend(missing)
        index += 2
    return history, repaired


def validate_tool_history(messages: list[Message]) -> None:
    reconcile_tool_history(messages)
