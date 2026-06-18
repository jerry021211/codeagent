"""Optional LangSmith tracing helpers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

DEFAULT_MAX_STRING_CHARS = 20_000
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_DEPTH = 6


class TraceHandle:
    """Small adapter around LangSmith's run tree object."""

    def __init__(self, run: Any | None = None) -> None:
        self._run = run

    def end(self, *, outputs: dict[str, Any] | None = None) -> None:
        if self._run is None:
            return

        end = getattr(self._run, "end", None)
        if not callable(end):
            return

        sanitized_outputs = sanitize_for_trace(outputs or {})
        try:
            end(outputs=sanitized_outputs)
        except TypeError:
            try:
                end()
            except Exception:
                return
        except Exception:
            return


@contextmanager
def trace_run(
    name: str,
    *,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[TraceHandle]:
    """Create a LangSmith span when tracing is enabled, otherwise no-op."""

    if not _tracing_enabled():
        yield TraceHandle()
        return

    _normalize_langsmith_env()
    try:
        from langsmith import trace

        trace_context = trace(
            name=name,
            run_type=run_type,
            inputs=sanitize_for_trace(inputs or {}),
            metadata=sanitize_for_trace(metadata or {}),
        )
    except Exception:
        yield TraceHandle()
        return

    enter = getattr(trace_context, "__enter__", None)
    exit_context = getattr(trace_context, "__exit__", None)
    if not callable(enter) or not callable(exit_context):
        yield TraceHandle()
        return

    try:
        run = enter()
    except Exception:
        yield TraceHandle()
        return

    try:
        yield TraceHandle(run)
    except BaseException as exc:
        try:
            suppress = exit_context(type(exc), exc, exc.__traceback__)
        except Exception:
            suppress = False
        if not suppress:
            raise
    else:
        try:
            exit_context(None, None, None)
        except Exception:
            return


def sanitize_for_trace(value: Any) -> Any:
    """Limit large or non-serializable values before sending them to LangSmith."""

    max_string_chars = _int_env(
        "LANGSMITH_TRACE_MAX_STRING_CHARS",
        DEFAULT_MAX_STRING_CHARS,
    )
    return _sanitize(value, max_string_chars=max_string_chars)


def _sanitize(
    value: Any,
    *,
    max_string_chars: int,
    depth: int = 0,
) -> Any:
    if depth > DEFAULT_MAX_DEPTH:
        return _limit_text(str(value), max_string_chars)

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return _limit_text(value, max_string_chars)

    if isinstance(value, dict):
        items = list(value.items())
        sanitized = {
            str(key): _sanitize(
                item,
                max_string_chars=max_string_chars,
                depth=depth + 1,
            )
            for key, item in items[:DEFAULT_MAX_ITEMS]
        }
        if len(items) > DEFAULT_MAX_ITEMS:
            sanitized["__truncated_items__"] = len(items) - DEFAULT_MAX_ITEMS
        return sanitized

    if isinstance(value, list | tuple):
        sanitized_items = [
            _sanitize(item, max_string_chars=max_string_chars, depth=depth + 1)
            for item in value[:DEFAULT_MAX_ITEMS]
        ]
        if len(value) > DEFAULT_MAX_ITEMS:
            sanitized_items.append(
                {"__truncated_items__": len(value) - DEFAULT_MAX_ITEMS}
            )
        return sanitized_items

    return _limit_text(str(value), max_string_chars)


def _tracing_enabled() -> bool:
    return _bool_env("LANGSMITH_TRACING") or _bool_env("LANGCHAIN_TRACING_V2")


def _normalize_langsmith_env() -> None:
    aliases = {
        "LANGCHAIN_TRACING_V2": "LANGSMITH_TRACING",
        "LANGCHAIN_API_KEY": "LANGSMITH_API_KEY",
        "LANGCHAIN_PROJECT": "LANGSMITH_PROJECT",
        "LANGCHAIN_ENDPOINT": "LANGSMITH_ENDPOINT",
    }
    for legacy, current in aliases.items():
        if os.getenv(current):
            continue
        value = os.getenv(legacy)
        if value:
            os.environ[current] = value


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, parsed)


def _limit_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n... ({len(value) - limit} more chars truncated)"
