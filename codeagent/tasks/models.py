"""Persistent task-domain records.

``TaskRecord`` deliberately contains only the nine semantic task fields.
Storage concerns such as list membership, revisions and timestamps live in
separate resource records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


JsonValue = Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TaskListScope(str, Enum):
    CONVERSATION_PRIVATE = "conversation_private"
    WORKSPACE_SHARED = "workspace_shared"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """The nine model-visible task fields."""

    id: str
    subject: str
    description: str
    active_form: str | None = None
    owner: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self, *, camel_case: bool = False) -> dict[str, JsonValue]:
        result = asdict(self)
        result["status"] = self.status.value
        result["blocks"] = list(self.blocks)
        result["blocked_by"] = list(self.blocked_by)
        if camel_case:
            result["activeForm"] = result.pop("active_form")
            result["blockedBy"] = result.pop("blocked_by")
        return result


@dataclass(frozen=True, slots=True)
class TaskResource:
    """Persistence envelope around a semantic task record."""

    task_list_id: str
    task: TaskRecord
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self, *, camel_case: bool = False) -> dict[str, JsonValue]:
        if camel_case:
            return {
                "taskListId": self.task_list_id,
                "task": self.task.to_dict(camel_case=True),
                "revision": self.revision,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
            }
        return {
            "task_list_id": self.task_list_id,
            "task": self.task.to_dict(),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TaskListRecord:
    id: str
    workspace: str
    name: str
    scope: TaskListScope
    origin_conversation_id: str | None
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        result = asdict(self)
        result["scope"] = self.scope.value
        return result


@dataclass(frozen=True, slots=True)
class TaskActivityRecord:
    id: int
    task_list_id: str
    task_id: str
    event_type: str
    conversation_id: str | None
    run_id: str | None
    agent_id: str | None
    payload: dict[str, JsonValue]
    created_at: str

    def to_dict(self) -> dict[str, JsonValue]:
        return asdict(self)


__all__ = [
    "JsonValue",
    "TaskActivityRecord",
    "TaskListRecord",
    "TaskListScope",
    "TaskRecord",
    "TaskResource",
    "TaskStatus",
]
