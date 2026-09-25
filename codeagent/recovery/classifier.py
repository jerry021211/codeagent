"""Classify provider exceptions and model responses into recovery reasons."""

from __future__ import annotations

from typing import Any

from codeagent.models import ModelResponse
from codeagent.recovery.models import RecoveryReason


def classify_exception(exc: Exception) -> RecoveryReason:
    """Return a normalized recovery reason for common API failures."""

    from codeagent.context.budget import RequestBudgetError
    if isinstance(exc, RequestBudgetError):
        # Local preparation has already exhausted its bounded compaction work.
        # Retrying it here would silently reset that bound and spend more calls.
        return RecoveryReason.NON_RETRYABLE_ERROR

    status = _status_code(exc)
    text = f"{type(exc).__name__}: {exc}".casefold()

    if _looks_prompt_too_long(status, text):
        return RecoveryReason.REACTIVE_COMPACT_RETRY
    if status == 429 or "rate limit" in text or "ratelimit" in text:
        return RecoveryReason.RATE_LIMIT_RETRY
    if status == 529 or "overload" in text or "overloaded" in text:
        return RecoveryReason.OVERLOADED_RETRY
    if "streaming is required" in text:
        return RecoveryReason.NON_RETRYABLE_ERROR
    if status in {401, 403} or _has_any(text, ("unauthorized", "forbidden", "api key")):
        return RecoveryReason.NON_RETRYABLE_ERROR
    if status in {400, 404, 422} or _has_any(
        text,
        (
            "bad request",
            "invalid request",
            "invalid model",
            "unknown model",
            "schema",
            "validation",
        ),
    ):
        return RecoveryReason.NON_RETRYABLE_ERROR
    if status is not None and 500 <= status < 600:
        return RecoveryReason.TRANSIENT_RETRY
    if _has_any(
        text,
        (
            "timeout",
            "timed out",
            "connection",
            "network",
            "temporarily unavailable",
            "dns",
            "remote disconnected",
            "connection reset",
            "connection aborted",
            "server disconnected",
            "service unavailable",
            "gateway",
        ),
    ):
        return RecoveryReason.TRANSIENT_RETRY
    return RecoveryReason.UNKNOWN_RETRY


def classify_response(response: ModelResponse) -> RecoveryReason:
    """Return a normalized reason for a successful provider response."""

    stop_reason = str(response.stop_reason or "").casefold()
    if stop_reason == "max_tokens":
        return RecoveryReason.MAX_OUTPUT_TOKENS_ESCALATE
    if stop_reason == "tool_use":
        return RecoveryReason.NEXT_TURN
    return RecoveryReason.COMPLETED


def retry_after_seconds(exc: Exception) -> float | None:
    """Best-effort extraction of provider Retry-After hints."""

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        headers = getattr(exc, "headers", None)
    if not headers:
        return None

    value = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter("retry-after") or getter("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _looks_prompt_too_long(status: int | None, text: str) -> bool:
    return status == 413 or _has_any(
        text,
        (
            "prompt_too_long",
            "prompt too long",
            "context length",
            "maximum context",
            "too many tokens",
            "413",
        ),
    )


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(exc, name, None)
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    response = getattr(exc, "response", None)
    if response is not None:
        for name in ("status_code", "status"):
            parsed = _int_or_none(getattr(response, name, None))
            if parsed is not None:
                return parsed
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        parsed = _int_or_none(body.get("status_code") or body.get("status"))
        if parsed is not None:
            return parsed
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)
