"""Anthropic SDK-backed model client."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from codeagent.events import EventEmitter, TokenUsage, UsageTracker
from codeagent.messages import Message
from codeagent.models import ModelResponse
from codeagent.tracing import trace_run


@dataclass(slots=True)
class AnthropicModelClient:
    """Anthropic Messages API client used by Agent."""

    api_key: str | None = None
    base_url: str | None = None
    stream: bool = False
    on_text: Callable[[str], None] | None = None
    event_emitter: EventEmitter | None = None
    usage_tracker: UsageTracker | None = None
    call_kind: str = "main"
    sdk_client: Any | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.sdk_client is not None:
            self._client = self.sdk_client
            return

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic SDK is not installed. Install project dependencies "
                "or run: pip install anthropic"
            ) from exc

        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = Anthropic(**kwargs)

    def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelResponse:
        call_id = f"call_{uuid4().hex}"
        started_at = time.monotonic()
        params = {
            "model": model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
        }
        self._emit(
            "model.started",
            {
                "call_id": call_id,
                "model": model,
                "call_kind": self.call_kind,
                "max_tokens": max_tokens,
                "message_count": len(messages),
                "tool_count": len(tools),
                "streaming": self.stream,
            },
        )
        with trace_run(
            "llm.anthropic.create_message",
            run_type="llm",
            inputs={**params, "stream": self.stream},
            metadata={
                "model": model,
                "base_url": self.base_url,
                "tool_count": len(tools),
            },
        ) as llm_trace:
            try:
                if self.stream:
                    response = self._create_streaming_message(params, call_id=call_id)
                else:
                    response = self._message_to_response(
                        self._client.messages.create(**params),
                        model=model,
                        call_kind=self.call_kind,
                    )
            except Exception as exc:
                self._emit(
                    "model.failed",
                    {
                        "call_id": call_id,
                        "model": model,
                        "call_kind": self.call_kind,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "duration_ms": round((time.monotonic() - started_at) * 1000),
                    },
                )
                raise
            if self.usage_tracker is not None:
                self.usage_tracker.record(response.usage)
            usage_payload = response.usage.to_dict() if response.usage is not None else None
            self._emit(
                "model.completed",
                {
                    "call_id": call_id,
                    "model": model,
                    "call_kind": self.call_kind,
                    "stop_reason": response.stop_reason,
                    "duration_ms": round((time.monotonic() - started_at) * 1000),
                    "usage": usage_payload,
                },
            )
            if usage_payload is not None:
                self._emit(
                    "usage.updated",
                    {"call_id": call_id, **usage_payload},
                )
            llm_trace.end(
                outputs={
                    "stop_reason": response.stop_reason,
                    "content": response.content,
                    "usage": usage_payload,
                }
            )
            return response

    def fork(
        self,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        event_emitter: EventEmitter | None = None,
        usage_tracker: UsageTracker | None = None,
        call_kind: str | None = None,
    ) -> AnthropicModelClient:
        """Create another wrapper around the same SDK client."""

        return AnthropicModelClient(
            stream=self.stream if stream is None else stream,
            on_text=on_text,
            event_emitter=(
                self.event_emitter if event_emitter is None else event_emitter
            ),
            usage_tracker=(
                self.usage_tracker if usage_tracker is None else usage_tracker
            ),
            call_kind=self.call_kind if call_kind is None else call_kind,
            sdk_client=self._client,
        )

    def _create_streaming_message(
        self,
        params: dict[str, Any],
        *,
        call_id: str,
    ) -> ModelResponse:
        with self._client.messages.stream(**params) as stream:
            for text in stream.text_stream:
                if self.on_text is not None:
                    self.on_text(text)
                self._emit(
                    "model.text_delta",
                    {"call_id": call_id, "text": text, "call_kind": self.call_kind},
                )
            return self._message_to_response(
                stream.get_final_message(),
                model=str(params.get("model") or ""),
                call_kind=self.call_kind,
            )

    @staticmethod
    def _message_to_response(
        message: Any,
        *,
        model: str = "",
        call_kind: str = "main",
    ) -> ModelResponse:
        return ModelResponse(
            stop_reason=str(getattr(message, "stop_reason", None) or "end_turn"),
            content=_normalize_content(getattr(message, "content", [])),
            raw=message,
            usage=_normalize_usage(
                _field(message, "usage"),
                model=str(_field(message, "model") or model),
                call_kind=call_kind,
            ),
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_emitter is not None:
            self.event_emitter.emit(event_type, payload)


def _normalize_content(content: Any) -> list[dict[str, Any]]:
    blocks = content if isinstance(content, list) else [content]
    return [_normalize_block(block) for block in blocks]


def _normalize_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block

    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped

    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    return {"type": str(block_type or "unknown"), "value": str(block)}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalize_usage(
    usage: Any,
    *,
    model: str,
    call_kind: str,
) -> TokenUsage | None:
    if usage is None:
        return TokenUsage(model=model, call_kind=call_kind, available=False)
    return TokenUsage(
        input_tokens=_nonnegative_int(_field(usage, "input_tokens", 0)),
        output_tokens=_nonnegative_int(_field(usage, "output_tokens", 0)),
        cache_creation_input_tokens=_nonnegative_int(
            _field(usage, "cache_creation_input_tokens", 0)
        ),
        cache_read_input_tokens=_nonnegative_int(
            _field(usage, "cache_read_input_tokens", 0)
        ),
        model=model,
        call_kind=call_kind,
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
