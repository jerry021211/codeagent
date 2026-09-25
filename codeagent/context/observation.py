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
    generation: int
    rewritten: bool
    rewrite_reason: str | None
    generation_reason: str | None
    previous_message_count: int
    current_message_count: int
    message_count_delta: int
    common_prefix_messages: int
    previous_suffix_messages: int
    current_suffix_messages: int
    previous_history_hash: str | None
    current_history_hash: str

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "history_generation": self.generation,
            "history_rewritten": self.rewritten,
            "rewrite_reason": self.rewrite_reason,
            "generation_reason": self.generation_reason,
            "previous_message_count": self.previous_message_count,
            "current_message_count": self.current_message_count,
            "message_count_delta": self.message_count_delta,
            "common_prefix_messages": self.common_prefix_messages,
            "previous_suffix_messages": self.previous_suffix_messages,
            "current_suffix_messages": self.current_suffix_messages,
            "previous_history_hash": self.previous_history_hash,
            "current_history_hash": self.current_history_hash,
        }


class HistoryObserver:
    """Compare consecutive histories sent to the main model."""

    def __init__(
        self,
        *,
        generation: int = 0,
        last_sent: list[Message] | None = None,
    ) -> None:
        self._generation = generation
        self._last_sent = deepcopy(last_sent) if last_sent is not None else None
        self._last_hash = _history_hash(self._last_sent) if self._last_sent is not None else None

    def restore(self, *, generation: int, last_sent: list[Message]) -> None:
        self._generation = generation
        self._last_sent = deepcopy(last_sent)
        self._last_hash = _history_hash(self._last_sent)

    def observe(
        self,
        messages: list[Message],
        *,
        generation: int | None = None,
        generation_reason: str | None = None,
    ) -> HistoryObservation:
        current = deepcopy(messages)
        current_hash = _history_hash(current)
        previous = self._last_sent

        if previous is None:
            if generation is not None:
                self._generation = generation
            observation = HistoryObservation(
                generation=self._generation,
                rewritten=False,
                rewrite_reason=None,
                generation_reason=None,
                previous_message_count=0,
                current_message_count=len(current),
                message_count_delta=len(current),
                common_prefix_messages=0,
                previous_suffix_messages=0,
                current_suffix_messages=len(current),
                previous_history_hash=None,
                current_history_hash=current_hash,
            )
            self._last_sent = current
            self._last_hash = current_hash
            return observation

        common_prefix = _common_prefix_length(previous, current)
        rewritten = common_prefix < len(previous)
        applied_reason: str | None = None
        if rewritten:
            if generation is not None and generation > self._generation:
                self._generation = generation
                applied_reason = generation_reason or "unexpected_prefix_change"
            else:
                self._generation += 1
                applied_reason = generation_reason or "unexpected_prefix_change"

        observation = HistoryObservation(
            generation=self._generation,
            rewritten=rewritten,
            rewrite_reason=applied_reason,
            generation_reason=applied_reason,
            previous_message_count=len(previous),
            current_message_count=len(current),
            message_count_delta=len(current) - len(previous),
            common_prefix_messages=common_prefix,
            previous_suffix_messages=max(0, len(previous) - common_prefix),
            current_suffix_messages=max(0, len(current) - common_prefix),
            previous_history_hash=self._last_hash,
            current_history_hash=current_hash,
        )
        self._last_sent = current
        self._last_hash = current_hash
        return observation


def _common_prefix_length(previous: list[Message], current: list[Message]) -> int:
    length = 0
    for old_message, new_message in zip(previous, current):
        if old_message != new_message:
            break
        length += 1
    return length


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
