"""Planning backend selection shared by composition roots."""

from __future__ import annotations

from enum import Enum


class PlanningBackend(str, Enum):
    AUTO = "auto"
    TASKS = "tasks"
    TODO = "todo"

    @classmethod
    def parse(cls, value: str | "PlanningBackend") -> "PlanningBackend":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            raise ValueError("planning backend must be auto, tasks, or todo") from exc


def resolve_planning_backend(
    value: str | PlanningBackend,
    *,
    interactive: bool,
) -> PlanningBackend:
    backend = PlanningBackend.parse(value)
    if backend is PlanningBackend.AUTO:
        return PlanningBackend.TASKS if interactive else PlanningBackend.TODO
    return backend


__all__ = ["PlanningBackend", "resolve_planning_backend"]
