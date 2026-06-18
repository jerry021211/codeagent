"""Task model placeholder."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TaskRecord:
    id: str
    subject: str
    status: str = "pending"
    blocked_by: list[str] = field(default_factory=list)
