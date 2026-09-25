"""Simple hook manager for agent lifecycle events."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

HookHandler = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class HookDecision:
    """Synchronous control, in addition to the existing string/None contract."""

    action: str
    message: str = ""
    reason: str = ""
    outcome: str = "blocked"


class HookManager:
    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    def register(self, event: str, handler: HookHandler, *, first: bool = False) -> None:
        if first:
            self._handlers[event].insert(0, handler)
        else:
            self._handlers[event].append(handler)

    def copy(
        self, *, rebind: Callable[[HookHandler], HookHandler] | None = None,
        exclude_owner: Any = None,
    ) -> HookManager:
        """Copy registrations so per-Agent guards do not leak to other Agents."""
        manager = HookManager()
        for event, handlers in self._handlers.items():
            manager._handlers[event] = [
                rebind(handler) if rebind else handler for handler in handlers
                if exclude_owner is None or getattr(handler, "__self__", None) is not exclude_owner
            ]
        return manager

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Run hooks in order; first non-None result blocks or alters flow."""

        for handler in self._handlers[event]:
            result = handler(*args, **kwargs)
            if result is not None:
                return result
        return None
