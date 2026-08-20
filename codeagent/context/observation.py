"""Observe whether model message history remains append-only across calls."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from codeagent.messages import Message


@dataclass(frozen=True, slots=True)
class HistoryObservation:
    """One comparison between the last sent history and the next one."""

    generation: int
    rewritten: bool
    rewrite_reason: str | None
    previous_message_count: int
    current_message_count: int
    common_prefix_messages: int
    discarded_prefix_messages: int
    appended_messages: int
    previous_history_hash: str | None
    current_history_hash: str

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "history_generation": self.generation,
            "history_rewritten": self.rewritten,
            "rewrite_reason": self.rewrite_reason,
            "previous_message_count": self.previous_message_count,
            "current_message_count": self.current_message_count,
            "common_prefix_messages": self.common_prefix_messages,
            "discarded_prefix_messages": self.discarded_prefix_messages,
            "appended_messages": self.appended_messages,
            "previous_history_hash": self.previous_history_hash,
            "current_history_hash": self.current_history_hash,
        }


class HistoryObserver:
    """Detect sent-prefix mutation without knowing how context is managed."""

    def __init__(self) -> None:
        self._generation = 0
        self._last_sent: list[Message] | None = None

    def observe(self, messages: list[Message]) -> HistoryObservation:
        current = deepcopy(messages)
        current_hash = _history_hash(current)
        previous = self._last_sent

        if previous is None:
            observation = HistoryObservation(
                generation=self._generation,
                rewritten=False,
                rewrite_reason=None,
                previous_message_count=0,
                current_message_count=len(current),
                common_prefix_messages=0,
                discarded_prefix_messages=0,
                appended_messages=len(current),
                previous_history_hash=None,
                current_history_hash=current_hash,
            )
            self._last_sent = current
            return observation

        common_prefix = _common_prefix_length(previous, current)
        rewritten = common_prefix < len(previous)
        if rewritten:
            self._generation += 1

        observation = HistoryObservation(
            generation=self._generation,
            rewritten=rewritten,
            rewrite_reason=(
                _rewrite_reason(previous, current, common_prefix)
                if rewritten
                else None
            ),
            previous_message_count=len(previous),
            current_message_count=len(current),
            common_prefix_messages=common_prefix,
            discarded_prefix_messages=max(0, len(previous) - common_prefix),
            appended_messages=max(0, len(current) - common_prefix),
            previous_history_hash=_history_hash(previous),
            current_history_hash=current_hash,
        )
        self._last_sent = current
        return observation


def _common_prefix_length(previous: list[Message], current: list[Message]) -> int:
    length = 0
    for old_message, new_message in zip(previous, current):
        if old_message != new_message:
            break
        length += 1
    return length


def _rewrite_reason(
    previous: list[Message],
    current: list[Message],
    common_prefix: int,
) -> str:
    if common_prefix == 0:
        return "sent_history_replaced"
    if len(current) < len(previous):
        return "sent_history_shortened"
    return "sent_prefix_changed"


def _history_hash(messages: list[Message]) -> str:
    serialized = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["HistoryObservation", "HistoryObserver"]
