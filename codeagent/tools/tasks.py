"""Claude Code-style persistent task tools."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from codeagent.tasks import TaskResource
from codeagent.tools.base import ToolDefinition


TASK_TOOL_NAMES = frozenset({"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"})


class TaskService(Protocol):
    def create_task(
        self,
        task_list_id: str,
        *,
        subject: str,
        description: str,
        active_form: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> TaskResource: ...

    def get_task_resource(self, task_list_id: str, task_id: str) -> TaskResource: ...

    def list_task_resources(
        self,
        task_list_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
    ) -> list[TaskResource]: ...

    def update_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        changes: Mapping[str, Any],
        expected_revision: int | None = None,
        actor_owner: str | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        human_override: bool = False,
    ) -> TaskResource: ...


@dataclass(slots=True)
class _TaskToolContext:
    service: TaskService
    task_list_id: str
    conversation_id: str | None = None
    run_id: str | None = None
    agent_id: str = "agent_root"

    @property
    def owner_id(self) -> str:
        if self.conversation_id:
            return f"{self.conversation_id}:{self.agent_id}"
        return self.agent_id


@dataclass(slots=True)
class TaskCreateTool:
    context: _TaskToolContext
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="TaskCreate",
            description=(
                "Create a persistent project task in the current task list. "
                "Use it for meaningful multi-step work, not tiny execution steps."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Concise task title."},
                    "description": {
                        "type": "string",
                        "description": "Context, requirements, and verifiable completion conditions.",
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "Short present-progress label shown while in progress.",
                    },
                    "metadata": {"type": "object"},
                },
                "required": ["subject", "description"],
            },
        ),
        init=False,
    )

    def run(
        self,
        subject: str,
        description: str,
        activeForm: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        resource = self.context.service.create_task(
            self.context.task_list_id,
            subject=subject,
            description=description,
            active_form=activeForm,
            metadata=metadata,
            conversation_id=self.context.conversation_id,
            run_id=self.context.run_id,
            agent_id=self.context.agent_id,
        )
        return _render(resource)


@dataclass(slots=True)
class TaskGetTool:
    context: _TaskToolContext
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="TaskGet",
            description="Get one task from the current task list.",
            input_schema={
                "type": "object",
                "properties": {"taskId": {"type": "string"}},
                "required": ["taskId"],
            },
        ),
        init=False,
    )

    def run(self, taskId: str) -> str:
        return _render(self.context.service.get_task_resource(self.context.task_list_id, taskId))


@dataclass(slots=True)
class TaskListTool:
    context: _TaskToolContext
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="TaskList",
            description="List tasks in the current task list, including dependencies and owners.",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                    "owner": {"type": "string"},
                },
            },
        ),
        init=False,
    )

    def run(self, status: str | None = None, owner: str | None = None) -> str:
        resources = self.context.service.list_task_resources(
            self.context.task_list_id,
            status=status,
            owner=owner,
        )
        return json.dumps(
            [resource.task.to_dict(camel_case=True) for resource in resources],
            ensure_ascii=False,
            indent=2,
        )


@dataclass(slots=True)
class TaskUpdateTool:
    context: _TaskToolContext
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="TaskUpdate",
            description=(
                "Update one task in the current task list. Claim ready work by setting "
                "status to in_progress; the harness assigns the current agent as owner."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "taskId": {"type": "string"},
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "activeForm": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                    "addBlocks": {"type": "array", "items": {"type": "string"}},
                    "addBlockedBy": {"type": "array", "items": {"type": "string"}},
                    "removeBlocks": {"type": "array", "items": {"type": "string"}},
                    "removeBlockedBy": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                },
                "required": ["taskId"],
            },
        ),
        init=False,
    )

    def run(self, taskId: str, **kwargs: Any) -> str:
        changes = _drop_missing(kwargs)
        resource = self.context.service.update_task(
            self.context.task_list_id,
            taskId,
            changes=changes,
            actor_owner=self.context.owner_id,
            conversation_id=self.context.conversation_id,
            run_id=self.context.run_id,
            agent_id=self.context.agent_id,
        )
        return _render(resource)


def create_task_tools(
    service: TaskService,
    task_list_id: str,
    *,
    conversation_id: str | None = None,
    run_id: str | None = None,
    agent_id: str = "agent_root",
) -> Sequence[object]:
    context = _TaskToolContext(
        service=service,
        task_list_id=task_list_id,
        conversation_id=conversation_id,
        run_id=run_id,
        agent_id=agent_id,
    )
    return (
        TaskCreateTool(context),
        TaskGetTool(context),
        TaskListTool(context),
        TaskUpdateTool(context),
    )


def _drop_missing(values: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "activeForm": "active_form",
        "addBlocks": "add_blocks",
        "addBlockedBy": "add_blocked_by",
        "removeBlocks": "remove_blocks",
        "removeBlockedBy": "remove_blocked_by",
    }
    return {aliases.get(key, key): value for key, value in values.items()}


def _render(resource: TaskResource) -> str:
    return json.dumps(resource.task.to_dict(camel_case=True), ensure_ascii=False, indent=2)


__all__ = [
    "TASK_TOOL_NAMES",
    "TaskCreateTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskService",
    "TaskUpdateTool",
    "create_task_tools",
]
