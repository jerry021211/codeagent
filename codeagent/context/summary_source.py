"""Bounded evidence for a summarizer, separate from canonical/provider history.

The returned objects are JSON data embedded inside the summary request's user
message. Preview objects are not executable tool inputs or provider messages.
Exact tool identities, paths and explicit outcome fields remain available even
when that means the final request budget must reject an unusually large record.
"""

from __future__ import annotations

import math
from typing import Any

from codeagent.context.projection import truncate_head_tail


_LIST_ITEMS = 32
_PREVIEW_MARKER = "\n[…摘要材料省略…]\n"
_IDENTITY_FIELDS = frozenset({
    "role", "type", "id", "name", "tool_name", "tool_use_id", "tool_call_id",
    "call_id", "run_id", "source_run_id", "event_id", "evidence_id", "reference_id",
    "path", "file_path", "file", "filename", "file_name", "target_path",
    "artifact_id", "artifact_path", "url", "uri", "status", "is_error",
    "error_code", "exit_code", "stop_reason", "finish_reason",
})
_REASONING_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning", "reasoning_text"})
_REASONING_FIELDS = frozenset({"thinking", "reasoning", "reasoning_content", "signature", "encrypted_content"})
_MEDIA_TYPES = frozenset({"image", "input_image", "document", "audio", "input_audio", "video"})
_MEDIA_METADATA = _IDENTITY_FIELDS | frozenset({
    "title", "mime_type", "media_type", "format", "width", "height", "duration",
    "duration_seconds", "file_id", "document_id", "source",
})


def bounded_summary_data(value: Any, *, text_limit: int = 4000) -> Any:
    """Build loss-marked JSON data with bounded strings and long-list previews.

    Lists keep their first and last 16 items when they exceed 32. Identity and
    explicit outcome fields are never shortened. This is a field budget, not a
    total-request guarantee; the caller must still run its complete fit check.
    """
    _check_limit("text_limit", text_limit)
    return _bounded(value, text_limit)


def bounded_summary_messages(
    messages: list[Any], *, text_limit: int = 4000, argument_limit: int = 2000,
) -> list[Any]:
    """Preserve every message/tool block, bounding only its summary evidence.

    All parallel tool IDs and explicit failure/status fields survive. Tool
    parameters are summarized by field, not by slicing serialized JSON. Media
    payloads and provider reasoning are excluded with explicit omission notes.
    The input, including SDK content objects, is never modified.
    """
    _check_limit("text_limit", text_limit)
    _check_limit("argument_limit", argument_limit)
    return _messages(messages, text_limit, argument_limit)


def summary_source_messages(messages: list[Any]) -> list[Any]:
    """Keep visible text and tool evidence in full, excluding media/reasoning.

    Use this for the first, loss-minimizing fit attempt. Payload sanitization is
    still required when the complete visible transcript happens to fit.
    """
    return _messages(messages, None, None)


def summary_file_ledger(messages: list[Any]) -> list[str]:
    """Extract exact paths from this batch's structured execution records.

    Do not infer paths from prose, shell commands or source code. A tool input
    records an attempted operation, not proof that the file exists or changed.
    """
    paths: dict[str, None] = {}
    for message in messages:
        message = _json_shape(message)
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            block = _json_shape(block)
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                arguments = block.get("input")
                if isinstance(arguments, dict):
                    for key in ("file_path", "path", "target_path", "artifact_path"):
                        path = arguments.get(key)
                        if isinstance(path, str) and path.strip():
                            paths[path] = None
            elif block.get("type") == "tool_result":
                changed = block.get("changed_files", [])
                if isinstance(changed, (list, tuple)):
                    for path in changed:
                        if isinstance(path, str) and path.strip():
                            paths[path] = None
    return list(paths)


def _messages(messages: list[Any], text_limit: int | None, argument_limit: int | None) -> list[Any]:
    result = []
    for original in messages:
        message = _json_shape(original)
        if not isinstance(message, dict):
            result.append(_bounded(message, text_limit))
            continue
        item = {}
        for key, value in message.items():
            if key in _REASONING_FIELDS:
                item[key] = _reasoning_omission()
            elif key == "content":
                item[key] = _content(value, text_limit, argument_limit)
            else:
                item[key] = _bounded(value, text_limit, key=str(key))
        result.append(item)
    return result


def _content(value: Any, text_limit: int | None, argument_limit: int | None) -> Any:
    if not isinstance(value, list):
        return _bounded(value, text_limit)
    result = []
    # Do not apply the generic list cap to protocol blocks: every parallel tool
    # call/result remains represented, even if the complete request cannot fit.
    for original in value:
        block = _json_shape(original)
        if not isinstance(block, dict):
            result.append(_bounded(block, text_limit))
            continue
        kind = block.get("type")
        if kind in _REASONING_TYPES:
            result.append({"type": kind, **_reasoning_omission()})
        elif kind in _MEDIA_TYPES:
            result.append(_media(block, text_limit))
        else:
            item = {}
            for key, field in block.items():
                if kind == "tool_use" and key == "input":
                    item[key] = _bounded(field, argument_limit)
                elif kind == "tool_result" and key == "content":
                    item[key] = _content(field, text_limit, argument_limit)
                elif key in _REASONING_FIELDS:
                    item[key] = _reasoning_omission()
                else:
                    item[key] = _bounded(field, text_limit, key=str(key))
            result.append(item)
    return result


def _bounded(value: Any, limit: int | None, *, key: str = "", depth: int = 0) -> Any:
    value = _json_shape(value)
    if limit is not None and depth > 24:
        return {"omitted": True, "reason": "summary_source_depth_limit"}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"omitted": True, "reason": "nonfinite_number", "value": str(value)}
    if isinstance(value, str):
        if value.startswith("data:") and (key in {"url", "uri", "data", "image_url"}):
            return _payload_omission(value)
        if limit is None or len(value) <= limit or key in _IDENTITY_FIELDS:
            return value
        return {"preview": truncate_head_tail(value, limit, _PREVIEW_MARKER),
                "original_chars": len(value), "truncated": True}
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind in _REASONING_TYPES:
            return {"type": kind, **_reasoning_omission()}
        if isinstance(kind, str) and kind in (_MEDIA_TYPES | {"base64"}):
            return _media(value, limit)
        return {str(name): _bounded(item, limit, key=str(name), depth=depth + 1)
                for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        if limit is None or len(value) <= _LIST_ITEMS:
            return [_bounded(item, limit, depth=depth + 1) for item in value]
        half = _LIST_ITEMS // 2
        return {
            "head": [_bounded(item, limit, depth=depth + 1) for item in value[:half]],
            "tail": [_bounded(item, limit, depth=depth + 1) for item in value[-half:]],
            "original_items": len(value), "omitted_items": len(value) - _LIST_ITEMS,
            "truncated": True,
        }
    if isinstance(value, (bytes, bytearray)):
        return {"omitted": True, "reason": "binary_payload", "original_bytes": len(value)}
    return {"omitted": True, "reason": "unsupported_summary_value", "value_type": type(value).__name__}


def _media(value: dict[str, Any], limit: int | None) -> dict[str, Any]:
    metadata = {}
    for key, item in value.items():
        if key == "source" and isinstance(item, dict):
            metadata[key] = _media(item, limit)
        elif key in {"data", "base64", "bytes", "frames"}:
            metadata[key] = _payload_omission(item)
        elif key in _MEDIA_METADATA:
            metadata[key] = _bounded(item, limit, key=key)
    metadata["summary_omission"] = {
        "omitted": True, "reason": "media_payload_not_in_summary", "source_metadata_only": True,
    }
    return metadata


def _payload_omission(value: Any) -> dict[str, Any]:
    note: dict[str, Any] = {"omitted": True, "reason": "media_payload_not_in_summary"}
    if isinstance(value, str):
        note["original_chars"] = len(value)
    elif isinstance(value, (bytes, bytearray)):
        note["original_bytes"] = len(value)
    elif isinstance(value, list):
        note["original_items"] = len(value)
    return note


def _reasoning_omission() -> dict[str, Any]:
    return {"omitted": True, "reason": "provider_reasoning_not_summarized"}


def _json_shape(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _check_limit(name: str, value: Any) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = ["bounded_summary_messages", "bounded_summary_data", "summary_source_messages", "summary_file_ledger"]
