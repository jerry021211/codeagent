"""Bound and redact event data before it leaves the agent core."""

from __future__ import annotations

import json
import os
import re
from typing import Any

DEFAULT_EVENT_PAYLOAD_BYTES = 32 * 1024
_SENSITIVE_KEYS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "cookie",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def redact_payload(value: Any, *, max_bytes: int = DEFAULT_EVENT_PAYLOAD_BYTES) -> Any:
    """Redact common secrets and cap the serialized payload size."""

    secrets = tuple(
        candidate
        for name, candidate in os.environ.items()
        if candidate and len(candidate) >= 8 and _is_sensitive_key(name)
    )
    sanitized = _redact(value, secrets=secrets, depth=0)
    encoded = json.dumps(sanitized, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return sanitized
    preview = encoded[: max(0, max_bytes - 256)].decode("utf-8", errors="ignore")
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "preview": preview,
    }


def _redact(value: Any, *, secrets: tuple[str, ...], depth: int) -> Any:
    if depth > 8:
        return "[truncated depth]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", text)
        return text
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            name = str(key)
            result[name] = (
                "[REDACTED]"
                if _is_sensitive_key(name)
                else _redact(item, secrets=secrets, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _redact(item, secrets=secrets, depth=depth + 1)
            for item in list(value)[:200]
        ]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _redact(model_dump(exclude_none=True), secrets=secrets, depth=depth + 1)
    return _redact(str(value), secrets=secrets, depth=depth + 1)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_KEYS)
