"""HTTP schemas for the local CodeAgent web application.

The persistence records intentionally stay framework-neutral.  These Pydantic
models are the public JSON contract consumed by the React client.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Shared strict request/response model configuration."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CreateConversationRequest(ApiModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)
    workspace: str | None = Field(default=None, min_length=1, max_length=4096)


class UpdateConversationRequest(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class CreateRunRequest(ApiModel):
    content: str = Field(min_length=1, max_length=200_000)


class ApprovalDecisionRequest(ApiModel):
    decision: Literal["allow", "deny"]


class ConversationResponse(ApiModel):
    id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    archived_at: str | None = None
    last_message: str | None = None
    active_run_id: str | None = None
    run_status: str | None = None
    active_task_list_id: str | None = None


class CreateTaskListRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    workspace: str | None = Field(default=None, min_length=1, max_length=4096)


class UpdateTaskListRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    expectedRevision: int = Field(ge=1)


class BindTaskListRequest(ApiModel):
    taskListId: str = Field(min_length=1, max_length=200)


class TaskListResponse(ApiModel):
    id: str
    workspace: str
    name: str
    scope: Literal["conversation_private", "workspace_shared"]
    originConversationId: str | None = None
    revision: int
    createdAt: str
    updatedAt: str
    archivedAt: str | None = None


class CreateTaskRequest(ApiModel):
    subject: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=100_000)
    activeForm: str | None = Field(default=None, max_length=500)
    blockedBy: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateTaskRequest(ApiModel):
    expectedRevision: int = Field(ge=1)
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=100_000)
    activeForm: str | None = Field(default=None, max_length=500)
    owner: str | None = Field(default=None, max_length=500)
    status: Literal["pending", "in_progress", "completed"] | None = None
    addBlocks: list[str] = Field(default_factory=list)
    addBlockedBy: list[str] = Field(default_factory=list)
    removeBlocks: list[str] = Field(default_factory=list)
    removeBlockedBy: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class TaskDataResponse(ApiModel):
    id: str
    subject: str
    description: str
    activeForm: str | None = None
    owner: str | None = None
    status: Literal["pending", "in_progress", "completed"]
    blocks: list[str] = Field(default_factory=list)
    blockedBy: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResourceResponse(ApiModel):
    taskListId: str
    task: TaskDataResponse
    revision: int
    createdAt: str
    updatedAt: str


class TaskActivityResponse(ApiModel):
    id: int
    taskListId: str
    taskId: str
    eventType: str
    conversationId: str | None = None
    runId: str | None = None
    agentId: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    createdAt: str


class MessageResponse(ApiModel):
    id: str
    conversation_id: str
    role: str
    content: Any
    created_at: str
    run_id: str | None = None
    status: str = "complete"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResponse(ApiModel):
    id: str
    conversation_id: str
    status: str
    queue_position: int | None = None
    created_at: str
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    cancel_requested_at: str | None = None
    error: str | None = None
    token_usage: dict[str, Any] | None = None


class CreateRunResponse(ApiModel):
    run_id: str
    status: str
    queue_position: int | None = None


class ApprovalResponse(ApiModel):
    id: str
    run_id: str
    tool_name: str
    summary: str
    reason: str | None = None
    input: Any = None
    status: str
    decision: str | None = None
    requested_at: str | None = None
    resolved_at: str | None = None


class RuntimeConfigResponse(ApiModel):
    model: str | None = None
    workspace: str
    max_tokens: int | None = None
    max_iterations: int | None = None
    planning_backend: Literal["tasks", "todo"] = "tasks"
    features: dict[str, bool] = Field(default_factory=dict)


class SaveMcpServerRequest(ApiModel):
    workspace: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    transport: Literal["stdio", "http"]
    command: str = Field(default="", max_length=4096)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(default=None, max_length=4096)
    url: str = Field(default="", max_length=8192)
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerResponse(ApiModel):
    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str = ""
    env_keys: list[str] = Field(default_factory=list)
    header_keys: list[str] = Field(default_factory=list)


class McpConfigResponse(ApiModel):
    workspace: str
    config_path: str
    restart_required: bool = False
    servers: list[McpServerResponse] = Field(default_factory=list)


class WorkspaceEntryResponse(ApiModel):
    name: str
    path: str
    is_project: bool = False


class WorkspaceListingResponse(ApiModel):
    current: str
    parent: str | None = None
    roots: list[str] = Field(default_factory=list)
    entries: list[WorkspaceEntryResponse] = Field(default_factory=list)


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    database: Literal["ok"] = "ok"


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalResponse",
    "ConversationResponse",
    "BindTaskListRequest",
    "CreateConversationRequest",
    "CreateRunRequest",
    "CreateRunResponse",
    "CreateTaskListRequest",
    "CreateTaskRequest",
    "HealthResponse",
    "MessageResponse",
    "McpConfigResponse",
    "McpServerResponse",
    "RunResponse",
    "RuntimeConfigResponse",
    "SaveMcpServerRequest",
    "TaskActivityResponse",
    "TaskDataResponse",
    "TaskListResponse",
    "TaskResourceResponse",
    "UpdateConversationRequest",
    "UpdateTaskListRequest",
    "UpdateTaskRequest",
    "WorkspaceEntryResponse",
    "WorkspaceListingResponse",
]
