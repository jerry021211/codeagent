"""Anthropic SDK-backed model client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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
        params = {
            "model": model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
        }
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
            if self.stream:
                response = self._create_streaming_message(params)
            else:
                response = self._message_to_response(
                    self._client.messages.create(**params)
                )
            llm_trace.end(
                outputs={
                    "stop_reason": response.stop_reason,
                    "content": response.content,
                }
            )
            return response

    def fork(
        self,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AnthropicModelClient:
        """Create another wrapper around the same SDK client."""

        return AnthropicModelClient(
            stream=self.stream if stream is None else stream,
            on_text=on_text,
            sdk_client=self._client,
        )

    def _create_streaming_message(self, params: dict[str, Any]) -> ModelResponse:
        with self._client.messages.stream(**params) as stream:
            for text in stream.text_stream:
                if self.on_text is not None:
                    self.on_text(text)
            return self._message_to_response(stream.get_final_message())

    @staticmethod
    def _message_to_response(message: Any) -> ModelResponse:
        return ModelResponse(
            stop_reason=str(getattr(message, "stop_reason", None) or "end_turn"),
            content=_normalize_content(getattr(message, "content", [])),
            raw=message,
        )


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
