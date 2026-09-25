"""Canonical history boundaries and provider-compatible conversation views."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from codeagent.messages import Message, _field, extract_text, normalize_tool_uses


def serializable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    return serializable(dump(mode="json")) if callable(dump) else value


def history_hash(messages: list[Message]) -> str:
    body = json.dumps(serializable(messages), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def user_content(message: Message) -> Message | None:
    """Remove protocol tool results, retaining real text and attachment blocks."""
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return message if content else None
    blocks = [block for block in content if _field(block, "type") != "tool_result"]
    return {**message, "content": blocks} if blocks else None


def is_user_turn(message: Message) -> bool:
    if message.get("_context_source") == "runtime":
        return False
    content = message.get("content")
    if isinstance(content, list) and any(_field(block, "type") == "tool_result" for block in content):
        return False
    user = user_content(message)
    if user is None:
        return False
    text = extract_text(user.get("content"))
    return not text.startswith(("[运行时提醒：", "<context_summary "))


def conversation_view(messages: list[Message], turn_start: int) -> list[Message]:
    """Keep unsummarized evidence across turns; only omit old empty replies.

    Size-based tool projection is the sole deterministic cleanup policy. Merely
    starting another user turn must not discard unarchived results/attachments.
    """
    if turn_start <= 0:
        return messages
    result = []
    for message in messages[:turn_start]:
        content = message.get("content")
        if (message.get("role") == "assistant" and not normalize_tool_uses(content)
                and not extract_text(content).strip()):
            continue
        result.append(message)
    result.extend(messages[turn_start:])
    return result
