"""Simple hook manager for agent lifecycle events."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

HookHandler = Callable[..., Any]


class HookManager:
    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    def register(self, event: str, handler: HookHandler) -> None:
        self._handlers[event].append(handler)

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Run hooks in order; first non-None result blocks or alters flow."""

        for handler in self._handlers[event]:
            result = handler(*args, **kwargs)
            if result is not None:
                return result
        return None
