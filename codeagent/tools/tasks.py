"""Claude Code-style persistent task tools."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
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
        blocked_by: Sequence[str] | None = None,
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
                    "blockedBy": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs of already-created prerequisite tasks.",
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
        blockedBy: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        resource = self.context.service.create_task(
            self.context.task_list_id,
            subject=subject,
            description=description,
            active_form=activeForm,
            blocked_by=blockedBy,
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
            description=(
                "Get one task's full details by taskId from the current task list, "
                "including its description, completion conditions, and metadata. "
                "Use after TaskList when you need to inspect or execute a task."
            ),
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
            description=(
                "List task summaries in the current task list: id, subject, status, "
                "owner, blocks, and blockedBy. Descriptions and metadata are omitted; "
                "use TaskGet with a taskId for full details before executing a task."
            ),
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
            [
                {
                    "id": resource.task.id,
                    "subject": resource.task.subject,
                    "status": resource.task.status.value,
                    "owner": resource.task.owner,
                    "blocks": list(resource.task.blocks),
                    "blockedBy": list(resource.task.blocked_by),
                }
                for resource in resources
            ],
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


def create_task_reminder_hook(
    task_state_provider: Callable[[], str],
    *,
    interval: int = 5,
):
    """Remind the model when persistent task progress has gone stale."""

    last_activity_count = 0
    rounds_since_update = 0
    initialized = False

    def hook(messages: list[dict[str, Any]]) -> str | None:
        nonlocal initialized, last_activity_count, rounds_since_update

        if interval <= 0:
            return None

        activity_count = _task_activity_count(messages)
        if not initialized:
            initialized = True
            last_activity_count = activity_count
            return None

        if activity_count != last_activity_count:
            last_activity_count = activity_count
            rounds_since_update = 0
            return None

        if not any(message.get("role") == "assistant" for message in messages):
            return None

        rounds_since_update += 1
        if rounds_since_update < interval:
            return None

        rounds_since_update = 0
        open_tasks = _open_task_state(task_state_provider())
        if not open_tasks:
            return None

        return "\n".join(
            [
                "<reminder>Persistent tasks have not been updated for "
                f"{interval} model calls.",
                "Review the current stage before continuing. Mark completed work, "
                "start the next ready stage, and do not expand the investigation "
                "without a specific remaining gap.",
                open_tasks,
                "</reminder>",
            ]
        )

    return hook


def _task_activity_count(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            1
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") in {"TaskCreate", "TaskUpdate"}
        )
    return count


def _open_task_state(value: str) -> str:
    tasks = json.loads(value)
    open_tasks = []
    for item in tasks:
        task = item.get("task", item)
        if task.get("status") != "completed":
            open_tasks.append(task)
    if not open_tasks:
        return ""
    return json.dumps(open_tasks, ensure_ascii=False, indent=2)


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
    "create_task_reminder_hook",
    "create_task_tools",
]
