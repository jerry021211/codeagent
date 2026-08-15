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
    features: dict[str, bool] = Field(default_factory=dict)


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
    "CreateConversationRequest",
    "CreateRunRequest",
    "CreateRunResponse",
    "HealthResponse",
    "MessageResponse",
    "RunResponse",
    "RuntimeConfigResponse",
    "UpdateConversationRequest",
    "WorkspaceEntryResponse",
    "WorkspaceListingResponse",
]
