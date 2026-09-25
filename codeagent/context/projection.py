"""Deterministic, loss-aware views of canonical Anthropic tool history.

Only the explicit built-in tools below participate. This module never executes
tools, opens files, edits canonical messages, or promises an unverified archive.
"""

from __future__ import annotations

from collections import Counter
from copy import copy
import json
import re
from typing import Any

from codeagent.messages import Message, _field

INVESTIGATION_TOOLS = frozenset({"read_file", "grep", "glob"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
TOOL_VIEW_MARKER = "[codeagent:tool-result-view:v1]"
WRITE_VIEW_MARKER = "[codeagent:write-args-view:v1]"
TRUNCATION_MARKER = "\n[系统视图截断，非磁盘内容]\n"
_READ_TAIL = re.compile(r"\n\.\.\. \(\d+ lines total, showing \d+-\d+\)$")
_BODY_KEYS = frozenset({"content", "old_string", "new_string"})
_STORED_OUTPUT_HEADER = re.compile(
    r"\A\[tool output stored\]\ntool: ([^\r\n]+)\noriginal_chars: (\d{1,20})\n"
    r"path: ([^\r\n\x00]+)\n完整结果已保存到指定路径。\n"
    r"只有在当前预览缺少必要信息时，才按精确范围读取该文件。\n\n--- head preview ---\n"
)


def truncate_head_tail(text: str, limit: int, marker: str = TRUNCATION_MARKER) -> str:
    """Bound Unicode code points, including the marker, even for tiny budgets."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(marker) >= limit:
        return marker[:limit]
    available = limit - len(marker)
    head = (available * 3 + 4) // 5
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def is_projection_placeholder(value: Any) -> bool:
    """Recognize entire known stubs/blocks, never a phrase in ordinary prose."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if value in {"[已清理]", "[已清理·须重填]"}:
        return True
    return any(
        value.startswith(marker + "\n") and value.endswith(_closing(marker))
        for marker in (TOOL_VIEW_MARKER, WRITE_VIEW_MARKER)
    )


def build_tool_projection(
    messages: list[Message],
    *,
    investigation_keep: int = 2,
    command_keep: int = 1,
    min_chars: int = 8000,
    write_keep: int = 2,
    write_min_chars: int = 8000,
) -> list[Message]:
    """Project completed old tool calls while preserving IDs, order and blocks.

    Investigation/command windows count candidate assistant rounds separately;
    writes count all assistant messages. Negative settings are invalid, and zero
    retention clears every eligible completed result. Missing/ambiguous pairs,
    failures, complete file reads, and unknown tools are preserved conservatively.
    """
    for name, value in {
        "investigation_keep": investigation_keep, "command_keep": command_keep,
        "min_chars": min_chars, "write_keep": write_keep,
        "write_min_chars": write_min_chars,
    }.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    assistant_indices = [i for i, message in enumerate(messages) if message.get("role") == "assistant"]
    calls: dict[str, tuple[int, int, Any]] = {}
    call_ids: Counter[str] = Counter()
    results: dict[str, list[tuple[int, int, Any, int | None]]] = {}
    round_index: int | None = None
    for message_index, message in enumerate(messages):
        if message.get("role") == "assistant":
            round_index = message_index
        for block_index, block in enumerate(_blocks(message.get("content"))):
            if _field(block, "type") == "tool_use" and message.get("role") == "assistant":
                call_id = _field(block, "id")
                if isinstance(call_id, str) and call_id:
                    call_ids[call_id] += 1
                    calls[call_id] = (message_index, block_index, block)
            if _field(block, "type") == "tool_result" and message.get("role") == "user":
                result_id = _field(block, "tool_use_id")
                if isinstance(result_id, str) and result_id:
                    results.setdefault(result_id, []).append((message_index, block_index, block, round_index))

    pairs = []
    for call_id, (call_message, call_block, call) in calls.items():
        matches = results.get(call_id, [])
        if call_ids[call_id] != 1 or len(matches) != 1:
            continue
        result_message, result_block, result, owner = matches[0]
        if owner != call_message or result_message <= call_message:
            continue
        arguments = _field(call, "input")
        if not isinstance(arguments, dict):
            continue
        text = _result_text(_field(result, "content"))
        if text is None:
            continue
        pairs.append((call_message, call_block, call, arguments, result_message, result_block, result, text))

    projected = messages
    copied_messages: set[int] = set()

    def replace_block(message_index: int, block_index: int, block: Any) -> None:
        nonlocal projected
        if projected is messages:
            projected = list(messages)
        if message_index not in copied_messages:
            projected[message_index] = dict(messages[message_index])
            projected[message_index]["content"] = list(_blocks(messages[message_index].get("content")))
            copied_messages.add(message_index)
        projected[message_index]["content"][block_index] = block

    for names, keep in ((INVESTIGATION_TOOLS, investigation_keep), ({"bash"}, command_keep)):
        candidates = [pair for pair in pairs if (
            _field(pair[2], "name") in names
            and len(pair[7]) >= min_chars
            and not _is_projected(pair[7])
            and not _failed(pair[6], pair[7])
            and not ((_field(pair[2], "name") == "bash" or pair[7].startswith("[tool output stored]"))
                     and _stored_output_path(_field(pair[2], "name"), pair[7]) is None)
            and not (_field(pair[2], "name") == "bash" and re.search(
                r"\b(?:FAILED|FAILURES|ERRORS)\b|\b[1-9]\d* (?:failed|errors?)\b", pair[7],
            ))
            and not (_field(pair[2], "name") == "read_file" and _complete_read(pair[3], pair[6], pair[7]))
        )]
        rounds = sorted({pair[0] for pair in candidates})
        retained = set(rounds[-keep:]) if keep else set()
        for pair in candidates:
            call_message, _, call, arguments, result_message, result_block, result, text = pair
            if call_message in retained:
                continue
            note = _result_note(_field(call, "name"), arguments, text, min_chars)
            if note is not None:
                replace_block(result_message, result_block, _updated(
                    result, content=_replace_text(_field(result, "content"), note),
                ))

    retained_writes = set(assistant_indices[-write_keep:]) if write_keep else set()
    for call_message, call_block, call, arguments, result_message, result_block, result, text in pairs:
        name = _field(call, "name")
        body_keys = {"content"} if name == "write_file" else {"old_string", "new_string"}
        bodies = [arguments[key] for key in body_keys if isinstance(arguments.get(key), str)]
        if (name not in WRITE_TOOLS or call_message in retained_writes
                or not bodies or sum(map(len, bodies)) < write_min_chars
                or not _successful_write(name, arguments, result, text)):
            continue
        identity = {key: value for key, value in arguments.items() if key not in _BODY_KEYS}
        note = _write_note(sum(map(len, bodies)))
        if len(note) >= sum(map(len, bodies)):
            continue
        replace_block(call_message, call_block, _updated(call, input=identity))
        replace_block(result_message, result_block, _updated(
            result, content=_append_text(_field(result, "content"), note),
        ))
    return projected


def _blocks(content: Any) -> list[Any]:
    return content if isinstance(content, list) else []


def _updated(block: Any, **updates: Any) -> Any:
    if isinstance(block, dict):
        return {**block, **updates}
    if callable(getattr(block, "model_copy", None)):
        return block.model_copy(update=updates)
    duplicate = copy(block)
    for key, value in updates.items():
        setattr(duplicate, key, value)
    return duplicate


def _result_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [_field(block, "text") for block in content if _field(block, "type") == "text"]
    return "\n".join(texts) if texts and all(isinstance(text, str) for text in texts) else None


def _replace_text(content: Any, text: str) -> Any:
    if isinstance(content, str):
        return text
    result = []
    inserted = False
    for block in content:
        if _field(block, "type") != "text":
            result.append(block)
        elif not inserted:
            result.append(_updated(block, text=text))
            inserted = True
    return result


def _append_text(content: Any, text: str) -> Any:
    if isinstance(content, str):
        return content + "\n\n" + text
    return list(content) + [{"type": "text", "text": text}]


def _failed(result: Any, text: str) -> bool:
    status = _field(result, "status")
    exit_code = _field(result, "exit_code")
    return bool(
        _field(result, "is_error", False)
        or (status is not None and status != "success")
        or (exit_code is not None and exit_code != 0)
        or text.startswith(("Error:", "Error running command:", "Blocked:", "Permission denied", "Invalid regex:"))
        or re.search(r"\[exit code: (?!0\])[-\d]+\]", text)
    )


def _successful_write(name: str, arguments: dict[str, Any], result: Any, text: str) -> bool:
    if _failed(result, text) or WRITE_VIEW_MARKER in text:
        return False
    path = arguments.get("file_path")
    if not isinstance(path, str) or not path:
        return False
    if name == "write_file":
        return re.fullmatch(r"Wrote \d+ lines to " + re.escape(path), text) is not None
    # The built-in editor puts an exact acknowledgement before its optional diff.
    return text.startswith("Edited " + path + "\n")


def _complete_read(arguments: dict[str, Any], result: Any, text: str) -> bool:
    if _field(result, "is_complete_view") is True:
        return True
    offset, limit = arguments.get("offset", 1), arguments.get("limit", 2000)
    if not isinstance(offset, int) or not isinstance(limit, int) or offset > 1 or limit <= 0:
        return False
    if text == "(empty file)":
        return True
    if _READ_TAIL.search(text):
        return False
    lines = text.splitlines()
    return bool(lines) and len(lines) <= limit and all(
        line.startswith(f"{index}\t") for index, line in enumerate(lines, 1)
    )


def _closing(marker: str) -> str:
    return marker.replace("[", "[/", 1)


def _is_projected(text: str) -> bool:
    return text.startswith(TOOL_VIEW_MARKER + "\n") and text.endswith(_closing(TOOL_VIEW_MARKER))


def _result_note(name: str, arguments: dict[str, Any], text: str, min_chars: int) -> str | None:
    # Keep context notices intact. If a configured threshold cannot fit an honest
    # notice, preserving the result is safer than emitting an ambiguous stub.
    budget = min(1800, min_chars - 1, len(text) - 1)
    archive = _stored_output_path(name, text)
    if (text.startswith("[tool output stored]") or name == "bash") and archive is None:
        # Old command output has no safe way to be recovered by replay. Keep it
        # until semantic compaction supplies a real canonical transcript archive.
        # Likewise do not erase malformed/oversized existing archive references.
        return None
    identity = next(((key, arguments[key]) for key in ("file_path", "path", "pattern", "command") if key in arguments), None)
    details = f"tool={name}; received_chars={len(text)}"
    if identity is not None:
        key, value = identity
        details += f"; {key}=" + json.dumps(truncate_head_tail(str(value), 160, "…"), ensure_ascii=False)
    if archive is not None:
        instruction = (
            "旧预览已从模型视图移除；已接收结果提供的归档引用（读取仍受当前执行者权限校验）：\n"
            "load_tool_output file_path=" + json.dumps(archive, ensure_ascii=False)
            + "；按 offset/char_offset 分页读取所需原文。"
        )
        if name == "bash":
            instruction += "此前命令已执行，不得仅为查看输出而重跑；工具返回成功不证明测试全部通过。"
    else:
        instruction = "旧结果已从模型视图移除，磁盘未变；仅在缺少必要原文时按原参数重新读取，当前文件可能已改变。"
        if name == "read_file":
            instruction += "read_file 默认最多2000行；需要全文时依据返回行数分段读取。"
    body = details + "\n" + instruction
    note = TOOL_VIEW_MARKER + "\n" + body + "\n" + _closing(TOOL_VIEW_MARKER)
    return note if len(note) <= budget else None


def _stored_output_path(name: str, text: str) -> str | None:
    """Read only the exact producer envelope, without guessing an artifact path.

    This is a received reference, not filesystem authorization: the runtime
    reader still validates the path beneath its actor-specific archive root.
    """
    match = _STORED_OUTPUT_HEADER.match(text)
    if match is None or match[1] != name or len(match[3]) > 600:
        return None
    # The envelope may be followed by a short preview or the complete body.
    if int(match[2]) < len(text) - match.end():
        return None
    return match[3]


def _write_note(body_chars: int) -> str:
    detail = f"此前写入工具返回成功；历史正文参数（{body_chars}字符）仅从模型视图移除，磁盘与原始历史未变。"
    detail += "后续编辑需读取当前文件核实，不能将此注记作为写入正文。"
    return WRITE_VIEW_MARKER + "\n" + detail + "\n" + _closing(WRITE_VIEW_MARKER)
