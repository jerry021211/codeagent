"""Model response structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codeagent.events import TokenUsage


@dataclass(slots=True)
class ModelResponse:
    stop_reason: str
    content: Any
    raw: Any | None = None
    usage: TokenUsage | None = None
