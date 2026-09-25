"""Non-mutating size inspection of a complete Anthropic-style request.

Character limits are exact for the stable JSON representation used here, not
for transport bytes. Token counts are deliberately conservative estimates;
provider tokenizers and image/document processing can differ. A passing check
does not guarantee that the provider accepts the request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


_MEDIA_TYPES = frozenset({"image", "input_image", "document", "audio", "input_audio", "video"})
_MEDIA_RESERVE_TOKENS = 8192


@dataclass(frozen=True, slots=True)
class RequestBudget:
    request_chars: int
    estimated_prompt_tokens: int
    output_reserve_tokens: int
    estimated_total_tokens: int
    multimodal_blocks: int
    estimation_method: str = "utf8_bytes_div_2_plus_media_reserve"
    token_count_is_estimate: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestBudgetError(RuntimeError):
    """The assembled request exceeds a configured local budget."""

    def __init__(self, budget: RequestBudget, *, reason: str, limit: int) -> None:
        self.budget = budget
        self.reason = reason
        self.limit = limit
        if reason == "max_request_chars":
            detail = f"serialized request characters {budget.request_chars} > {limit}"
        else:
            detail = (
                f"estimated prompt tokens {budget.estimated_prompt_tokens} + "
                f"output reserve {budget.output_reserve_tokens} exceeds window budget {limit}"
            )
        super().__init__(f"Context request budget exceeded: {detail}; token counts are estimates.")


class BoundModelClient:
    """Enforce complete request budgets for side calls, including their forks.

    This adapter never performs compaction or changes input messages. A window
    resolver, when supplied, is evaluated for the model actually being sent, so
    fallback selection cannot accidentally reuse another model's window size.
    Unspecified/zero windows retain the explicit character limit.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_request_chars: int | None = None,
        context_window_tokens: int | None = None,
        window_resolver: Callable[[str], int | None] | None = None,
    ) -> None:
        self._client = client
        self.max_request_chars = max_request_chars
        self.context_window_tokens = context_window_tokens
        self.window_resolver = window_resolver

    def create_message(
        self,
        *,
        model: str,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> Any:
        params = dict(model=model, system=system, messages=messages, tools=tools)
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        window = self.window_resolver(model) if self.window_resolver is not None else self.context_window_tokens
        enforce_request(**params, max_request_chars=self.max_request_chars, context_window_tokens=window)
        return self._client.create_message(**params)

    def fork(self, **kwargs: Any) -> "BoundModelClient":
        fork = getattr(self._client, "fork", None)
        client = fork(**kwargs) if callable(fork) else self._client
        return BoundModelClient(
            client, max_request_chars=self.max_request_chars,
            context_window_tokens=self.context_window_tokens, window_resolver=self.window_resolver,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def inspect_request(
    *,
    model: str,
    system: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int | None = None,
) -> RequestBudget:
    """Inspect all request fields without truncating or changing any input.

    Counting UTF-8 bytes at two bytes per estimated token avoids the substantial
    undercount of a four-characters-per-token rule for CJK and emoji. Each media
    block additionally reserves tokens for content that JSON cannot describe,
    such as a remote image. This is a heuristic, not a model-specific tokenizer
    or a reliable upper bound for arbitrary multipage documents/video.
    An omitted max_tokens leaves output reservation unknown (reported as zero),
    rather than treating a summary's character budget as a token reservation.
    """
    if max_tokens is not None:
        _nonnegative_integer("max_tokens", max_tokens)
    request = {
        "model": model,
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    serialized = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=_json_value,
    )
    # JSON decoding also makes SDK content blocks available to the media scan;
    # it never writes back to caller-owned message/tool structures.
    normalized = json.loads(serialized)
    media_count = _media_count(normalized["messages"]) + _media_count(normalized["system"])
    estimated_prompt = (len(serialized.encode("utf-8")) + 1) // 2
    estimated_prompt += media_count * _MEDIA_RESERVE_TOKENS
    return RequestBudget(
        request_chars=len(serialized),
        estimated_prompt_tokens=estimated_prompt,
        output_reserve_tokens=max_tokens or 0,
        estimated_total_tokens=estimated_prompt + (max_tokens or 0),
        multimodal_blocks=media_count,
    )


def enforce_request(
    *,
    model: str,
    system: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int | None = None,
    max_request_chars: int | None = None,
    context_window_tokens: int | None = None,
    safety_margin_tokens: int = 0,
) -> RequestBudget:
    """Reject oversized requests; zero/None disables the respective limit.

    The caller must provide the selected model's known context window. No model
    window is guessed. Output tokens and an optional safety margin are reserved
    independently of input size, including when the input itself is very small.
    """
    budget = inspect_request(
        model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens,
    )
    return validate_budget(budget, max_request_chars=max_request_chars,
                           context_window_tokens=context_window_tokens,
                           safety_margin_tokens=safety_margin_tokens)


def validate_budget(
    budget: RequestBudget, *, max_request_chars: int | None = None,
    context_window_tokens: int | None = None, safety_margin_tokens: int = 0,
) -> RequestBudget:
    """Check an already measured request without serializing it again."""
    for name, value in (
        ("max_request_chars", max_request_chars),
        ("context_window_tokens", context_window_tokens),
        ("safety_margin_tokens", safety_margin_tokens),
    ):
        if value is not None:
            _nonnegative_integer(name, value)
        elif name == "safety_margin_tokens":
            raise ValueError("safety_margin_tokens must be a nonnegative integer")
    if max_request_chars and budget.request_chars > max_request_chars:
        raise RequestBudgetError(budget, reason="max_request_chars", limit=max_request_chars)
    if context_window_tokens and budget.estimated_total_tokens + safety_margin_tokens > context_window_tokens:
        raise RequestBudgetError(
            budget, reason="context_window_tokens", limit=context_window_tokens - safety_margin_tokens,
        )
    return budget


def _nonnegative_integer(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _json_value(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"Unsupported request value: {type(value).__name__}")


def _media_count(value: Any) -> int:
    if isinstance(value, list):
        return sum(_media_count(item) for item in value)
    if not isinstance(value, dict):
        return 0
    block_type = value.get("type")
    own = int(isinstance(block_type, str) and block_type in _MEDIA_TYPES)
    return own + sum(_media_count(item) for item in value.values())


__all__ = ["BoundModelClient", "RequestBudget", "RequestBudgetError", "inspect_request", "enforce_request", "validate_budget"]
