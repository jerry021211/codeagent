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
                "在当前任务列表创建有意义的阶段任务，不用于细碎执行步骤。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "简短任务标题"},
                    "description": {
                        "type": "string",
                        "description": "上下文、要求和可验证的验收条件",
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "进行中显示的简短进展描述",
                    },
                    "blockedBy": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "已创建的前置任务 ID",
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
                "按 taskId 获取完整要求、验收条件和元数据；TaskList 后需要查看或执行任务时使用。"
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
                "列出任务摘要：id、subject、status、owner、blocks、blockedBy。执行前通过 TaskGet 读取完整说明和元数据。"
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
                "更新任务；将就绪任务设为 in_progress 时由运行时分配当前执行者。完成状态必须符合实际进展。"
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
                f"<reminder>已有任务连续 {interval} 次模型调用未更新。",
                "检查当前阶段的实际进展，标记已完成工作并开始下一个就绪阶段；"
                "没有具体未知问题时，不要扩大调查范围。",
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
