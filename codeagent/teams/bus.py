"""Durable Agent Team message bus.

SQLite remains the source of truth.  The bus is intentionally a thin facade;
the repository performs routing, generation, ordering, and idempotency checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class MessageBus:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def send(
        self,
        team_run_id: str,
        *,
        sender_type: str,
        recipient_type: str,
        recipient_agent_id: str,
        recipient_generation: int,
        message_type: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        sender_agent_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        artifact_refs: Sequence[str] = (),
        priority: str = "normal",
        payload_version: int = 1,
    ) -> Any:
        return self.repository.send_team_message(
            team_run_id,
            sender_type=sender_type,
            sender_agent_id=sender_agent_id,
            recipient_type=recipient_type,
            recipient_agent_id=recipient_agent_id,
            recipient_generation=recipient_generation,
            message_type=message_type,
            payload_version=payload_version,
            payload=payload,
            artifact_refs=artifact_refs,
            correlation_id=correlation_id,
            causation_id=causation_id,
            dedupe_key=dedupe_key,
            task_id=task_id,
            attempt_id=attempt_id,
            priority=priority,
        )

    def receive(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[Any]:
        return self.repository.fetch_unacked_team_messages(session_id, limit=limit)
