"""FastAPI transport for the local CodeAgent coding cockpit.

This module contains HTTP/SSE concerns only.  The scheduler owns execution and
the repository owns persistence, so either can be replaced by tests or future
adapters without changing the public API.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # Keep the core/CLI package importable without optional web dependencies.
    from fastapi import FastAPI, Header, HTTPException, Query, Request, status
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

    from codeagent.web.schemas import (
        ApprovalDecisionRequest,
        ApprovalResponse,
        AttemptPlanDecisionRequest,
        AttemptResumeRequest,
        BindTaskListRequest,
        CandidateReviewRequest,
        CandidateUserApprovalRequest,
        ConversationResponse,
        CreateConversationRequest,
        CreateRunRequest,
        CreateRunResponse,
        CreateTeamPlanRevisionRequest,
        CreateTeamRunRequest,
        CreateTaskListRequest,
        CreateTaskRequest,
        HealthResponse,
        McpConfigResponse,
        McpServerResponse,
        ManualIntegrationRequest,
        MessageResponse,
        RunResponse,
        RuntimeConfigResponse,
        SaveMcpServerRequest,
        TaskActivityResponse,
        TaskListResponse,
        TaskResourceResponse,
        TeamCancelRequest,
        TeamDecisionRequest,
        UpdateConversationRequest,
        UpdateTaskListRequest,
        UpdateTaskRequest,
        WorkspaceListingResponse,
        WorktreeDispositionRequest,
    )
except ImportError as exc:  # pragma: no cover - exercised only in a core-only install.
    FastAPI = None  # type: ignore[assignment,misc]
    _WEB_IMPORT_ERROR: ImportError | None = exc
else:
    _WEB_IMPORT_ERROR = None

from codeagent.web.models import (
    ApprovalRecord,
    ConversationRecord,
    MessageRecord,
    RunRecord,
)
from codeagent.mcp import delete_mcp_server, load_mcp_document, save_mcp_server
from codeagent.memory import MemoryAccessController
from codeagent.web.storage import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    InvalidStateTransitionError,
    RecordNotFoundError,
    SQLiteRepository,
    StorageConflictError,
)
from codeagent.web.workspaces import WorkspaceCatalog
from codeagent.tasks import TaskActivityRecord, TaskListRecord, TaskResource
from codeagent.runtime import RuntimeDataPaths, TeamSupervisor
from codeagent.teams import ManualIntegrationVerifier
from codeagent.worktrees import WorktreeError, WorktreeManagerRegistry


_SESSION_COOKIE = "codeagent_session"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HEARTBEAT_SECONDS = 10.0


def create_app(
    *,
    repository: SQLiteRepository | None = None,
    scheduler: Any | None = None,
    workspace: str | Path | None = None,
    env: Any | None = None,
    static_dir: str | Path | None = None,
    team_supervisor: Any | None = None,
) -> Any:
    """Build the local-only application with injectable runtime boundaries.

    When ``repository`` and ``scheduler`` are supplied no model configuration
    is loaded, making transport tests deterministic and credential-free.
    """

    if _WEB_IMPORT_ERROR is not None:
        raise RuntimeError(
            "The web UI dependencies are not installed. "
            "Install the project with `pip install -e .[web]`."
        ) from _WEB_IMPORT_ERROR

    workspace_path = Path(workspace or Path.cwd()).expanduser().resolve()
    workspace_catalog = WorkspaceCatalog(workspace_path)
    owns_repository = repository is None
    runtime_env = env
    repo = repository
    try:
        if scheduler is None:
            from codeagent.config import EnvironmentConfig
            from codeagent.web.factory import WebAgentFactory
            from codeagent.web.scheduler import RunScheduler

            runtime_env = runtime_env or EnvironmentConfig.from_env()
        configured_data_dir = getattr(runtime_env, "data_dir", None)
        data_paths = (
            RuntimeDataPaths(configured_data_dir)
            if configured_data_dir is not None
            else RuntimeDataPaths.default()
        )
        repo = repo or SQLiteRepository.for_workspace(
            workspace_path,
            data_dir=data_paths.root,
        )
        repo.bind_unassigned_workspaces(str(workspace_path))
        memory_access = MemoryAccessController(
            repo.has_active_team_run_for_workspace
        )
        if scheduler is None:
            scheduler = RunScheduler(
                repo,
                WebAgentFactory(
                    runtime_env,
                    workspace_path,
                    repo,
                    data_paths=data_paths,
                    memory_access=memory_access,
                ),
            )
    except (ImportError, RuntimeError) as exc:
        if owns_repository and repo is not None:
            repo.close()
        raise RuntimeError(
            "Unable to initialize the web runtime. Check MODEL_ID/API_KEY "
            "and install the optional web dependencies."
        ) from exc
    assert repo is not None
    assert scheduler is not None

    static_root = _static_root(static_dir)
    session_token = secrets.token_urlsafe(32)
    team_enabled = bool(getattr(runtime_env, "team_runtime_enabled", False))
    team_write_enabled = bool(getattr(runtime_env, "team_write_enabled", False))
    configured_worktree_root = getattr(
        runtime_env, "team_worktree_root", Path(".codeagent-worktrees")
    )
    worktrees = WorktreeManagerRegistry(
        repo, data_paths.worktree_root(configured_worktree_root)
    )
    configure_team = getattr(scheduler, "configure_team_runtime", None)
    if callable(configure_team):
        configure_team(worktrees)
    if team_supervisor is None and team_enabled:
        builder = getattr(scheduler, "create_team_agent", None)
        lead_runner = getattr(scheduler, "run_team_lead_cycle", None)
        if callable(builder):
            team_supervisor = TeamSupervisor(
                repo,
                lambda session, attempt, cancellation: builder(
                    session, attempt, cancellation, worktrees
                ),
                enabled=True,
                write_enabled=team_write_enabled,
                max_workers=4,
                model_response_timeout=getattr(runtime_env, "team_model_response_timeout", 300.0),
                model_call_timeout=getattr(runtime_env, "team_model_call_timeout", 600.0),
                worktree_manager=worktrees,
                lead_runner=lead_runner if callable(lead_runner) else None,
                lead_activity_provider=getattr(scheduler, "team_lead_activities", None),
            )

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        application.state.repository = repo
        application.state.scheduler = scheduler
        application.state.workspace = workspace_path
        application.state.environment = runtime_env
        application.state.team_supervisor = team_supervisor
        application.state.team_worktrees = worktrees
        scheduler.start()
        if team_supervisor is not None:
            team_supervisor.start()
        try:
            yield
        finally:
            try:
                if team_supervisor is not None:
                    team_supervisor.stop()
            finally:
                try:
                    scheduler.stop()
                finally:
                    if owns_repository:
                        repo.close()

    app = FastAPI(
        title="CodeAgent Coding Cockpit",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    # State is also populated immediately for ASGI users that do not run
    # lifespan (useful for introspection); execution still starts in lifespan.
    app.state.repository = repo
    app.state.scheduler = scheduler
    app.state.workspace = workspace_path
    app.state.environment = runtime_env
    app.state.team_supervisor = team_supervisor
    app.state.team_worktrees = worktrees

    @app.middleware("http")
    async def local_security(request: Request, call_next: Any) -> Response:
        host = (request.url.hostname or "").casefold()
        if host not in _LOOPBACK_HOSTS:
            response: Response = JSONResponse(
                {"detail": "CodeAgent Web only accepts loopback hosts."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        elif not _origin_is_allowed(request.headers.get("origin")):
            response = JSONResponse(
                {"detail": "Cross-origin requests are not allowed."},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        elif (
            request.url.path.startswith("/api/")
            and request.method.upper() == "GET"
            and request.cookies.get(_SESSION_COOKIE) not in {None, session_token}
        ):
            response = JSONResponse(
                {"detail": "Invalid local session cookie."},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        elif (
            request.url.path.startswith("/api/")
            and request.method.upper() in _UNSAFE_METHODS
            and request.cookies.get(_SESSION_COOKIE) != session_token
        ):
            response = JSONResponse(
                {"detail": "Missing or invalid local session cookie."},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        elif (
            request.url.path.startswith("/api/")
            and request.method.upper() in _UNSAFE_METHODS
            and not _is_json_request(request)
        ):
            response = JSONResponse(
                {"detail": "Mutation endpoints require application/json."},
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        else:
            response = await call_next(request)

        if request.cookies.get(_SESSION_COOKIE) != session_token:
            response.set_cookie(
                _SESSION_COOKIE,
                session_token,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response

    @app.exception_handler(RecordNotFoundError)
    async def not_found_handler(_request: Request, exc: RecordNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(StorageConflictError)
    @app.exception_handler(InvalidStateTransitionError)
    @app.exception_handler(WorktreeError)
    async def conflict_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    @app.exception_handler(ValueError)
    async def validation_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(RuntimeError)
    async def runtime_handler(_request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/api/health", response_model=HealthResponse)
    @app.get("/healthz", response_model=HealthResponse, include_in_schema=False)
    def health() -> HealthResponse:
        if not repo.health_check():
            raise HTTPException(status_code=503, detail="Database health check failed.")
        return HealthResponse()

    @app.get("/api/conversations", response_model=list[ConversationResponse])
    def list_conversations(
        archived: bool = Query(default=False),
        search: str | None = Query(default=None, max_length=200),
    ) -> list[ConversationResponse]:
        records = repo.list_conversations(
            include_archived=archived,
            query=search,
        )
        return [_conversation_response(repo, record) for record in records]

    @app.post(
        "/api/conversations",
        response_model=ConversationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_conversation(body: CreateConversationRequest) -> ConversationResponse:
        selected_workspace = workspace_catalog.resolve(
            body.workspace or workspace_path
        )
        record = repo.create_conversation(
            title=body.title.strip(),
            workspace=str(selected_workspace),
        )
        return _conversation_response(repo, record)

    @app.get("/api/workspaces", response_model=WorkspaceListingResponse)
    def list_workspaces(
        path: str | None = Query(default=None, max_length=4096),
    ) -> WorkspaceListingResponse:
        listing = workspace_catalog.list(path)
        return WorkspaceListingResponse(
            current=listing.current,
            parent=listing.parent,
            roots=list(listing.roots),
            entries=[entry.to_dict() for entry in listing.entries],
        )

    @app.get("/api/mcp/servers", response_model=McpConfigResponse)
    def list_mcp_servers(
        workspace: str = Query(max_length=4096),
    ) -> McpConfigResponse:
        selected = workspace_catalog.resolve(workspace)
        config_path = _mcp_config_path(runtime_env, selected)
        return _mcp_config_response(selected, config_path)

    @app.post(
        "/api/mcp/servers",
        response_model=McpConfigResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def save_mcp_server_config(body: SaveMcpServerRequest) -> McpConfigResponse:
        selected = workspace_catalog.resolve(body.workspace)
        config_path = _mcp_config_path(runtime_env, selected)
        if body.transport == "stdio":
            if not body.command.strip():
                raise ValueError("Local MCP servers require a command")
            server = {
                "type": "stdio",
                "command": body.command.strip(),
                "args": body.args,
                "env": body.env,
            }
            if body.cwd:
                server["cwd"] = body.cwd
        else:
            if not body.url.startswith(("http://", "https://")):
                raise ValueError("Remote MCP server URL must start with http:// or https://")
            server = {
                "type": "http",
                "url": body.url,
                "headers": body.headers,
            }
        save_mcp_server(config_path, body.name, server)
        reloaded = _reload_mcp_runtime(scheduler, selected)
        return _mcp_config_response(
            selected,
            config_path,
            restart_required=not reloaded,
        )

    @app.delete(
        "/api/mcp/servers/{server_name}",
        response_model=McpConfigResponse,
    )
    def remove_mcp_server(
        server_name: str,
        workspace: str = Query(max_length=4096),
    ) -> McpConfigResponse:
        selected = workspace_catalog.resolve(workspace)
        config_path = _mcp_config_path(runtime_env, selected)
        if not delete_mcp_server(config_path, server_name):
            raise RecordNotFoundError(f"MCP server not found: {server_name}")
        reloaded = _reload_mcp_runtime(scheduler, selected)
        return _mcp_config_response(
            selected,
            config_path,
            restart_required=not reloaded,
        )

    @app.get(
        "/api/conversations/{conversation_id}",
        response_model=ConversationResponse,
    )
    def get_conversation(conversation_id: str) -> ConversationResponse:
        record = _require_conversation(repo, conversation_id)
        return _conversation_response(repo, record)

    @app.patch(
        "/api/conversations/{conversation_id}",
        response_model=ConversationResponse,
    )
    def update_conversation(
        conversation_id: str,
        body: UpdateConversationRequest,
    ) -> ConversationResponse:
        changes: dict[str, Any] = {}
        if "title" in body.model_fields_set and body.title is not None:
            changes["title"] = body.title.strip()
        if "archived" in body.model_fields_set and body.archived is not None:
            changes["archived"] = body.archived
        record = repo.update_conversation(conversation_id, **changes)
        return _conversation_response(repo, record)

    @app.delete(
        "/api/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_conversation(conversation_id: str) -> Response:
        if not repo.delete_conversation(conversation_id):
            raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/conversations/{conversation_id}/messages",
        response_model=list[MessageResponse],
    )
    def list_messages(conversation_id: str) -> list[MessageResponse]:
        _require_conversation(repo, conversation_id)
        return [_message_response(item) for item in repo.list_messages(conversation_id)]

    @app.get("/api/task-lists", response_model=list[TaskListResponse])
    def list_task_lists(
        workspace: str | None = Query(default=None, max_length=4096),
        archived: bool = Query(default=False),
    ) -> list[TaskListResponse]:
        selected = workspace_catalog.resolve(workspace or workspace_path)
        return [
            _task_list_response(item)
            for item in repo.list_task_lists(
                workspace=str(selected), include_archived=archived
            )
        ]

    @app.post(
        "/api/task-lists",
        response_model=TaskListResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_list(body: CreateTaskListRequest) -> TaskListResponse:
        selected = workspace_catalog.resolve(body.workspace or workspace_path)
        return _task_list_response(
            repo.create_task_list(
                workspace=str(selected),
                name=body.name,
                scope="workspace_shared",
            )
        )

    @app.get("/api/task-lists/{task_list_id}", response_model=TaskListResponse)
    def get_task_list(task_list_id: str) -> TaskListResponse:
        return _task_list_response(_require_task_list(repo, task_list_id))

    @app.patch("/api/task-lists/{task_list_id}", response_model=TaskListResponse)
    def update_task_list(
        task_list_id: str,
        body: UpdateTaskListRequest,
    ) -> TaskListResponse:
        changes: dict[str, Any] = {"expected_revision": body.expectedRevision}
        if "name" in body.model_fields_set:
            changes["name"] = body.name
        return _task_list_response(repo.update_task_list(task_list_id, **changes))

    @app.post(
        "/api/task-lists/{task_list_id}/promote",
        response_model=TaskListResponse,
    )
    def promote_task_list(task_list_id: str) -> TaskListResponse:
        return _task_list_response(repo.update_task_list(task_list_id, promote=True))

    @app.post(
        "/api/task-lists/{task_list_id}/archive",
        response_model=TaskListResponse,
    )
    def archive_task_list(task_list_id: str) -> TaskListResponse:
        return _task_list_response(repo.update_task_list(task_list_id, archived=True))

    @app.post(
        "/api/conversations/{conversation_id}/task-list",
        response_model=ConversationResponse,
    )
    def bind_task_list(
        conversation_id: str,
        body: BindTaskListRequest,
    ) -> ConversationResponse:
        record = repo.bind_conversation_task_list(conversation_id, body.taskListId)
        return _conversation_response(repo, record)

    @app.get(
        "/api/task-lists/{task_list_id}/tasks",
        response_model=list[TaskResourceResponse],
    )
    def list_tasks(
        task_list_id: str,
        task_status: str | None = Query(default=None, alias="status"),
        owner: str | None = Query(default=None, max_length=500),
    ) -> list[TaskResourceResponse]:
        _require_task_list(repo, task_list_id)
        return [
            _task_resource_response(item)
            for item in repo.list_task_resources(
                task_list_id, status=task_status, owner=owner
            )
        ]

    @app.post(
        "/api/task-lists/{task_list_id}/tasks",
        response_model=TaskResourceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(
        task_list_id: str,
        body: CreateTaskRequest,
    ) -> TaskResourceResponse:
        _require_task_list(repo, task_list_id)
        return _task_resource_response(
            repo.create_task(
                task_list_id,
                subject=body.subject,
                description=body.description,
                active_form=body.activeForm,
                blocked_by=body.blockedBy,
                metadata=body.metadata,
            )
        )

    @app.get(
        "/api/task-lists/{task_list_id}/tasks/{task_id}",
        response_model=TaskResourceResponse,
    )
    def get_task(task_list_id: str, task_id: str) -> TaskResourceResponse:
        return _task_resource_response(repo.get_task_resource(task_list_id, task_id))

    @app.patch(
        "/api/task-lists/{task_list_id}/tasks/{task_id}",
        response_model=TaskResourceResponse,
    )
    def update_task(
        task_list_id: str,
        task_id: str,
        body: UpdateTaskRequest,
    ) -> TaskResourceResponse:
        aliases = {
            "activeForm": "active_form",
            "addBlocks": "add_blocks",
            "addBlockedBy": "add_blocked_by",
            "removeBlocks": "remove_blocks",
            "removeBlockedBy": "remove_blocked_by",
        }
        changes = {
            aliases.get(name, name): getattr(body, name)
            for name in body.model_fields_set
            if name != "expectedRevision"
        }
        return _task_resource_response(
            repo.update_task(
                task_list_id,
                task_id,
                changes=changes,
                expected_revision=body.expectedRevision,
                human_override=True,
            )
        )

    @app.get(
        "/api/task-lists/{task_list_id}/tasks/{task_id}/activity",
        response_model=list[TaskActivityResponse],
    )
    def list_task_activity(
        task_list_id: str,
        task_id: str,
    ) -> list[TaskActivityResponse]:
        repo.get_task_resource(task_list_id, task_id)
        return [
            _task_activity_response(item)
            for item in repo.list_task_activity(task_list_id, task_id=task_id)
        ]

    @app.get("/api/task-lists/{task_list_id}/events")
    async def stream_task_events(
        request: Request,
        task_list_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        _require_task_list(repo, task_list_id)
        cursor = max(after, _parse_event_sequence(last_event_id))

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(
                    repo.wait_for_task_activity,
                    task_list_id,
                    cursor,
                    _HEARTBEAT_SECONDS,
                )
                for event in events:
                    cursor = max(cursor, event.id)
                    yield _encode_sse(
                        event.id,
                        f"task.{event.event_type}",
                        _task_activity_response(event).model_dump(),
                    )
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/conversations/{conversation_id}/runs",
        response_model=CreateRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_run(
        conversation_id: str,
        body: CreateRunRequest,
    ) -> CreateRunResponse:
        _require_conversation(repo, conversation_id)
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=422, detail="Run content cannot be blank.")
        if body.useTeam and not team_enabled:
            raise HTTPException(
                status_code=409,
                detail="Agent Team is disabled by the current Runtime configuration.",
            )
        run = scheduler.submit(conversation_id, content, use_team=body.useTeam)
        return CreateRunResponse(
            run_id=run.id,
            status=run.status,
            queue_position=run.queue_position,
        )

    @app.get("/api/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: str) -> RunResponse:
        return _run_response(repo, _require_run(repo, run_id))

    @app.post("/api/runs/{run_id}/cancel", response_model=RunResponse)
    def cancel_run(run_id: str) -> RunResponse:
        _require_run(repo, run_id)
        return _run_response(repo, scheduler.cancel(run_id))

    @app.post(
        "/api/runs/{run_id}/approvals/{approval_id}",
        response_model=ApprovalResponse,
    )
    def decide_approval(
        run_id: str,
        approval_id: str,
        body: ApprovalDecisionRequest,
    ) -> ApprovalResponse:
        approval = repo.get_approval(approval_id)
        if approval is None or approval.run_id != run_id:
            raise RecordNotFoundError(f"Approval not found: {approval_id}")
        resolved = scheduler.resolve_approval(run_id, approval_id, body.decision)
        return _approval_response(resolved)

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        _require_run(repo, run_id)
        cursor = max(after, _parse_event_sequence(last_event_id))

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(
                    repo.list_events,
                    run_id,
                    after_seq=cursor,
                )
                if not events:
                    current = repo.get_run(run_id)
                    if current is None or current.status in TERMINAL_RUN_STATUSES:
                        return
                    events = await asyncio.to_thread(
                        repo.wait_for_events,
                        run_id,
                        cursor,
                        _HEARTBEAT_SECONDS,
                    )
                for event in events:
                    cursor = max(cursor, event.seq)
                    yield _encode_sse(event.seq, event.type, event.to_dict())

                run = repo.get_run(run_id)
                if run is None or run.status in TERMINAL_RUN_STATUSES:
                    # wait_for_events returns every event after the cursor; once
                    # terminal, the durable stream is therefore fully drained.
                    return
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runtime-config", response_model=RuntimeConfigResponse)
    def runtime_config() -> RuntimeConfigResponse:
        return RuntimeConfigResponse(
            model=_config_value(runtime_env, "model_id", "model"),
            workspace=str(workspace_path),
            max_tokens=_optional_int(_config_value(runtime_env, "max_tokens")),
            max_iterations=_optional_int(
                _config_value(runtime_env, "max_iterations")
            ),
            planning_backend="tasks",
            features={
                "sse": True,
                "approvals": True,
                "cancellation": True,
                "persistence": True,
                "debug_metadata": True,
                "workspace_browser": True,
                "tasks": True,
                "mcp_config": True,
                "agent_team": bool(
                    _config_value(runtime_env, "team_runtime_enabled")
                ),
                "agent_team_write": bool(
                    _config_value(runtime_env, "team_write_enabled")
                ),
            },
        )

    @app.post("/api/teams", status_code=status.HTTP_201_CREATED)
    def create_team(body: CreateTeamRunRequest) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        run = _require_run(repo, body.rootRunId)
        conversation = _require_conversation(repo, run.conversation_id)
        task_list = _require_task_list(repo, body.taskListId)
        if task_list.workspace != conversation.workspace:
            raise StorageConflictError(
                "Team Task list and root conversation must use the same workspace"
            )
        repo.validate_team_plan_tasks(task_list.id, body.plan)
        manager = worktrees.for_workspace(conversation.workspace)
        inspection = manager.inspect_baseline(body.baseCommit)
        if inspection.source_dirty and not body.allowDirty:
            raise StorageConflictError(
                "Source workspace has uncommitted changes. Confirm allowDirty=true "
                "after reviewing that those changes will not enter Team Worktrees."
            )
        with memory_access.creating_team(conversation.workspace):
            team = repo.create_team_run(
                conversation_id=conversation.id,
                root_run_id=run.id,
                task_list_id=task_list.id,
                base_commit=inspection.base_commit,
                max_teammates=body.maxTeammates,
                token_budget=body.tokenBudget,
                model_call_budget=body.modelCallBudget,
                deadline_at=body.deadlineAt,
                metadata={
                    "manual_integration_only": True,
                    "source_dirty_at_creation": inspection.source_dirty,
                },
            )
        plan_payload = dict(body.plan)
        plan_payload["base_commit"] = inspection.base_commit
        plan_payload["integration_mode"] = "manual"
        plan_payload["teammate_count"] = body.teammateCount
        plan = repo.create_team_plan_revision(
            team.id,
            plan=plan_payload,
            created_by=team.lead_agent_id,
            command_id=f"api-create-team-plan:{team.id}:1",
        )
        repo.submit_team_plan_revision(
            team.id,
            plan.revision,
            command_id=f"api-submit-team-plan:{team.id}:1",
        )
        manager.confirm_baseline(
            team.id,
            confirmed_by="user" if inspection.source_dirty else "runtime",
            allow_dirty=body.allowDirty,
            command_id=f"api-confirm-team-base:{team.id}",
        )
        return _team_snapshot(
            repo,
            team.id,
            allow_code=team_write_enabled,
        )

    @app.get("/api/teams")
    def list_teams(
        conversation_id: str | None = Query(default=None, max_length=200),
    ) -> list[dict[str, Any]]:
        _require_team_enabled(team_enabled, team_supervisor)
        teams = repo.list_team_runs()
        if conversation_id is not None:
            teams = [item for item in teams if item.conversation_id == conversation_id]
        return [
            _team_snapshot(repo, item.id, allow_code=team_write_enabled)
            for item in teams
        ]

    @app.get("/api/teams/{team_run_id}")
    def get_team(team_run_id: str) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        return _team_snapshot(repo, team_run_id, allow_code=team_write_enabled)

    @app.get("/api/teams/{team_run_id}/changes")
    def get_team_changes(
        team_run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        table: str | None = Query(default=None, max_length=80),
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        return repo.list_team_changes(team_run_id, after=after, limit=limit, table=table)

    @app.get("/api/teams/{team_run_id}/changes/{sequence}")
    def get_team_change(team_run_id: str, sequence: int) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        page = repo.list_team_changes(team_run_id, seq=sequence)
        if not page["items"]:
            raise RecordNotFoundError("Change not found in this Team")
        return page["items"][0]

    @app.post("/api/teams/{team_run_id}/plans", status_code=status.HTTP_201_CREATED)
    def create_team_plan_revision(
        team_run_id: str,
        body: CreateTeamPlanRevisionRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        team = repo.get_team_run(team_run_id)
        if team is None:
            raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
        repo.validate_team_plan_tasks(team.task_list_id, body.plan)
        plan_payload = dict(body.plan)
        plan_payload["base_commit"] = team.base_commit
        plan_payload["integration_mode"] = "manual"
        revisions = repo.list_team_plan_revisions(team_run_id)
        if revisions:
            plan_payload["teammate_count"] = revisions[-1].plan.get(
                "teammate_count", team.max_teammates
            )
        plan = repo.create_team_plan_revision(
            team_run_id,
            plan=plan_payload,
            created_by=team.lead_agent_id,
            command_id=body.commandId,
        )
        plan = repo.submit_team_plan_revision(
            team_run_id,
            plan.revision,
            command_id=f"submit:{body.commandId}",
        )
        return {
            "plan": plan.to_dict(),
            "team": _team_snapshot(
                repo, team_run_id, allow_code=team_write_enabled
            ),
        }

    @app.post("/api/teams/{team_run_id}/plans/{revision}/decision")
    def decide_team_plan(
        team_run_id: str,
        revision: int,
        body: TeamDecisionRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        if body.decision == "approve":
            worktrees.ensure_baseline_ready(team_run_id)
        repo.decide_team_plan_revision(
            team_run_id,
            revision,
            decision=body.decision,
            decided_by="user",
            reason=body.reason,
            command_id=body.commandId,
        )
        return _team_snapshot(repo, team_run_id, allow_code=team_write_enabled)

    @app.post("/api/teams/{team_run_id}/attempts/{attempt_id}/plan-decision")
    def decide_attempt_plan(
        team_run_id: str,
        attempt_id: str,
        body: AttemptPlanDecisionRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        raise HTTPException(
            status_code=409,
            detail=(
                "Attempt Plans are decided by the Root/Lead. Send guidance in the "
                "conversation or wait for the Lead Session."
            ),
        )

    @app.post("/api/teams/{team_run_id}/attempts/{attempt_id}/resume")
    def resume_team_attempt(
        team_run_id: str,
        attempt_id: str,
        body: AttemptResumeRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        attempt = repo.get_task_attempt(attempt_id)
        if attempt.team_run_id != team_run_id:
            raise RecordNotFoundError(
                f"Attempt not found in TeamRun: {attempt_id}"
            )
        assert team_supervisor is not None
        team_supervisor.resume_attempt(
            attempt_id,
            resumed_by="user",
            reason=body.reason,
            command_id=body.commandId,
            acknowledge_unknown_result=body.acknowledgeUnknownResult,
        )
        return _team_snapshot(repo, team_run_id, allow_code=team_write_enabled)

    @app.post("/api/teams/{team_run_id}/candidates/{candidate_id}/review")
    def review_candidate(
        team_run_id: str,
        candidate_id: str,
        body: CandidateReviewRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        raise HTTPException(
            status_code=409,
            detail=(
                "Candidate semantic review is performed by the Root/Lead. Only a "
                "separate high-risk user approval is accepted here."
            ),
        )

    @app.post("/api/teams/{team_run_id}/candidates/{candidate_id}/approval")
    def approve_high_risk_candidate(
        team_run_id: str,
        candidate_id: str,
        body: CandidateUserApprovalRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        candidate = repo.get_candidate(candidate_id)
        if candidate.team_run_id != team_run_id:
            raise RecordNotFoundError(
                f"Candidate not found in TeamRun: {candidate_id}"
            )
        decided = repo.decide_candidate_user_approval(
            candidate_id,
            decision=body.decision,
            decided_by="user",
            reason=body.reason,
            command_id=body.commandId,
        )
        return {
            "candidate": decided.to_dict(),
            "team": _team_snapshot(
                repo, team_run_id, allow_code=team_write_enabled
            ),
        }

    @app.post("/api/teams/{team_run_id}/cancel")
    def cancel_team(
        team_run_id: str,
        body: TeamCancelRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        for attempt in repo.list_task_attempts(team_run_id):
            if attempt.state.value in {"succeeded", "failed", "cancelled", "orphaned"}:
                continue
            team_supervisor.cancel_attempt(
                attempt.id,
                requested_by="user",
                reason=body.reason,
                command_id=f"{body.commandId}:{attempt.id}",
            )
        repo.cancel_team_run(
            team_run_id,
            cancelled_by="user",
            reason=body.reason,
            command_id=body.commandId,
        )
        return _team_snapshot(repo, team_run_id, allow_code=team_write_enabled)

    @app.post("/api/teams/{team_run_id}/manual-integration")
    def verify_manual_integration(
        team_run_id: str,
        body: ManualIntegrationRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        check = ManualIntegrationVerifier(repo).verify(
            team_run_id,
            target_ref=body.targetRef,
            verified_by="user",
            command_id=body.commandId,
        )
        return {
            "check": check,
            "team": _team_snapshot(
                repo, team_run_id, allow_code=team_write_enabled
            ),
        }

    @app.post("/api/teams/{team_run_id}/worktrees/{worktree_id}/disposition")
    def dispose_worktree(
        team_run_id: str,
        worktree_id: str,
        body: WorktreeDispositionRequest,
    ) -> dict[str, Any]:
        _require_team_enabled(team_enabled, team_supervisor)
        binding = repo.get_worktree_binding(worktree_id)
        if binding.team_run_id != team_run_id:
            raise RecordNotFoundError(
                f"Worktree not found in TeamRun: {worktree_id}"
            )
        if body.action == "cleanup":
            binding = worktrees.cleanup_retained(worktree_id)
        return binding.to_dict()

    @app.get("/api/teams/{team_run_id}/events")
    async def stream_team_events(
        request: Request,
        team_run_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        _require_team_enabled(team_enabled, team_supervisor)
        team = repo.get_team_run(team_run_id)
        if team is None:
            raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
        cursor = max(after, _parse_event_sequence(last_event_id))
        terminal = {
            "completed",
            "closed_with_unmerged_candidates",
            "failed",
            "cancelled",
        }

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(
                    repo.list_events, team.root_run_id, after_seq=cursor
                )
                if not events:
                    events = await asyncio.to_thread(
                        repo.wait_for_events,
                        team.root_run_id,
                        cursor,
                        _HEARTBEAT_SECONDS,
                    )
                for event in events:
                    cursor = max(cursor, event.seq)
                    if event.type.startswith("team.") or event.payload.get(
                        "team_run_id"
                    ) == team_run_id:
                        yield _encode_sse(event.seq, event.type, event.to_dict())
                current = repo.get_team_run(team_run_id)
                if current is None or current.state.value in terminal:
                    return
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route("/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(asset_path: str) -> Response:
        if asset_path == "api" or asset_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if static_root is None:
            return JSONResponse(
                {
                    "detail": (
                        "Frontend build not found. Run `npm install` and "
                        "`npm run build` in web/."
                    )
                },
                status_code=503,
            )

        requested = (static_root / asset_path).resolve() if asset_path else static_root
        if _is_beneath(requested, static_root) and requested.is_file():
            return FileResponse(
                requested,
                headers={"Cache-Control": _asset_cache_control(requested)},
            )
        return FileResponse(
            static_root / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    return app


def _require_conversation(
    repository: SQLiteRepository,
    conversation_id: str,
) -> ConversationRecord:
    record = repository.get_conversation(conversation_id)
    if record is None:
        raise RecordNotFoundError(f"Conversation not found: {conversation_id}")
    return record


def _require_run(repository: SQLiteRepository, run_id: str) -> RunRecord:
    record = repository.get_run(run_id)
    if record is None:
        raise RecordNotFoundError(f"Run not found: {run_id}")
    return record


def _require_task_list(
    repository: SQLiteRepository,
    task_list_id: str,
) -> TaskListRecord:
    record = repository.get_task_list(task_list_id)
    if record is None:
        raise RecordNotFoundError(f"Task list not found: {task_list_id}")
    return record


def _require_team_enabled(enabled: bool, supervisor: Any | None) -> None:
    if not enabled:
        raise RuntimeError("Agent Team is disabled by TEAM_RUNTIME_ENABLED")
    if supervisor is None:
        raise RuntimeError("Agent Team supervisor is unavailable")


def _team_snapshot(
    repository: SQLiteRepository,
    team_run_id: str,
    *,
    allow_code: bool,
) -> dict[str, Any]:
    team = repository.get_team_run(team_run_id)
    if team is None:
        raise RecordNotFoundError(f"TeamRun not found: {team_run_id}")
    attempts = repository.list_task_attempts(team_run_id)
    candidates = repository.list_candidates(team_run_id)
    worktrees = repository.list_worktree_bindings(team_run_id)
    tasks = repository.list_task_resources(team.task_list_id)
    sessions = repository.list_agent_sessions(team_run_id)
    messages = repository.list_team_messages(team_run_id)
    usage = repository.aggregate_usage(
        run_id=team.root_run_id, include_breakdown=False
    )
    return {
        "team": team.to_dict(),
        "base_confirmation": repository.get_team_base_confirmation(team_run_id),
        "plans": [
            item.to_dict()
            for item in repository.list_team_plan_revisions(team_run_id)
        ],
        "agents": [item.to_dict() for item in repository.list_team_agents(team_run_id)],
        "sessions": [
            item.to_dict() for item in sessions
        ],
        "tasks": [item.to_dict(camel_case=True) for item in tasks],
        "scheduling": [
            item.to_dict()
            for item in repository.list_task_scheduling(
                team_run_id, allow_code=allow_code
            )
        ],
        "attempts": [item.to_dict() for item in attempts],
        "attempt_plans": [
            item.to_dict()
            for attempt in attempts
            for item in repository.list_attempt_plans(attempt.id)
        ],
        "worktrees": [item.to_dict() for item in worktrees],
        "recoveries": _team_recoveries(
            repository,
            attempts=attempts,
            sessions=sessions,
            worktrees=worktrees,
            messages=messages,
        ),
        "candidates": [item.to_dict() for item in candidates],
        "validation_runs": [
            item.to_dict()
            for candidate in candidates
            for item in repository.list_validation_runs(candidate.id)
        ],
        "messages": [
            item.to_dict() for item in messages
        ],
        "integration_checks": repository.list_manual_integration_checks(team_run_id),
        "usage": usage,
        "manual_integration": {
            "required": True,
            "commands": [
                f"git cherry-pick -x {item.commit_hash}"
                for item in candidates
                if item.commit_hash and item.integrated_at is None
            ],
            "automatic_merge": False,
        },
    }


def _team_recoveries(
    repository: SQLiteRepository,
    *,
    attempts: list[Any],
    sessions: list[Any],
    worktrees: list[Any],
    messages: list[Any],
) -> list[dict[str, Any]]:
    """Build the current recovery UI from durable runtime records."""

    session_by_id = {item.id: item for item in sessions}
    worktree_by_attempt = {item.attempt_id: item for item in worktrees}
    recoverable_reasons = {
        "model_response_timeout",
        "model_call_timeout",
        "scope_violation",
        "unknown_write_result",
        "protocol_incomplete",
        "agent_iteration_limit",
        "agent_runtime_failed",
        "agent_result_ready",
        "service_restart",
    }
    summaries = {
        "model_response_timeout": "模型长时间没有有效响应；worker退出后可检查并恢复",
        "model_call_timeout": "本轮模型调用达到总时限；未执行迟到的工具调用，可检查并恢复",
        "scope_violation": "Worktree现场需要处理后重新检查",
        "unknown_write_result": "写操作结果未知，禁止自动重放",
        "protocol_incomplete": "Teammate未完成规定的Team提交动作",
        "agent_iteration_limit": "Teammate达到本次最大迭代次数",
        "agent_runtime_failed": "模型运行失败后需要人工确认继续",
        "agent_result_ready": "旧版本worker已退出但Attempt尚未完成",
        "service_restart": "服务重启中断了正在运行的Attempt",
    }
    result: list[dict[str, Any]] = []
    for attempt in attempts:
        session = session_by_id.get(attempt.session_id)
        binding = worktree_by_attempt.get(attempt.id)
        error_type = str((attempt.error or {}).get("type") or "")
        reason_code = (
            "unknown_write_result"
            if attempt.result_unknown and error_type != "service_restart"
            else str(
                (session.waiting_reason if session is not None else None)
                or (binding.frozen_reason if binding is not None else None)
                or error_type
                or ""
            )
        )
        if reason_code not in recoverable_reasons:
            continue
        if attempt.state.value not in {"waiting", "orphaned"}:
            continue
        executions = repository.list_tool_executions(attempt.id)
        tool = next(
            (
                item
                for item in reversed(executions)
                if item.result_unknown or item.status in {"failed", "scope_violation"}
            ),
            None,
        )
        scope_message = next(
            (
                item
                for item in reversed(messages)
                if item.attempt_id == attempt.id and item.type == "SCOPE_VIOLATION"
            ),
            None,
        )
        attempted = (
            str(scope_message.payload.get("attempted") or "")
            if scope_message is not None
            else ""
        )
        outside_paths = [
            item.strip() for item in attempted.split(",") if item.strip()
        ]
        blocking_checks = ["runtime_recheck_required"]
        if attempt.result_unknown:
            blocking_checks.insert(0, "unknown_result_acknowledgement_required")
        result.append(
            {
                "attempt_id": attempt.id,
                "task_id": attempt.task_id,
                "agent_id": attempt.agent_id,
                "reason_code": reason_code,
                "summary": summaries.get(reason_code, reason_code),
                "recoverable": (
                    attempt.state.value == "waiting"
                    or (
                        attempt.state.value == "orphaned"
                        and error_type == "service_restart"
                    )
                ),
                "result_unknown": attempt.result_unknown,
                "tool_name": tool.tool_name if tool is not None else None,
                "tool_call_id": tool.tool_call_id if tool is not None else None,
                "tool_executed": bool(
                    tool
                    and (
                        tool.result_unknown
                        or (
                            tool.status == "scope_violation"
                            and str(tool.error or "").startswith("Tool changed files")
                        )
                    )
                ),
                "allowed_scopes": list(binding.write_scopes) if binding else [],
                "outside_paths": outside_paths,
                "worktree_path": binding.path if binding else None,
                "blocking_checks": blocking_checks,
            }
        )
    return result


def _conversation_response(
    repository: SQLiteRepository,
    record: ConversationRecord,
) -> ConversationResponse:
    messages = repository.list_messages(record.id)
    runs = repository.list_runs(conversation_id=record.id, limit=100)
    latest_run = runs[0] if runs else None
    active_run = next(
        (run for run in runs if run.status in ACTIVE_RUN_STATUSES),
        None,
    )
    return ConversationResponse(
        **record.to_dict(),
        last_message=_message_preview(messages[-1]) if messages else None,
        active_run_id=active_run.id if active_run else None,
        run_status=latest_run.status if latest_run else None,
    )


def _message_response(record: MessageRecord) -> MessageResponse:
    return MessageResponse(**record.to_dict(), status="complete")


def _task_list_response(record: TaskListRecord) -> TaskListResponse:
    return TaskListResponse(
        id=record.id,
        workspace=record.workspace,
        name=record.name,
        scope=record.scope.value,
        originConversationId=record.origin_conversation_id,
        revision=record.revision,
        createdAt=record.created_at,
        updatedAt=record.updated_at,
        archivedAt=record.archived_at,
    )


def _task_resource_response(record: TaskResource) -> TaskResourceResponse:
    return TaskResourceResponse(**record.to_dict(camel_case=True))


def _task_activity_response(record: TaskActivityRecord) -> TaskActivityResponse:
    return TaskActivityResponse(
        id=record.id,
        taskListId=record.task_list_id,
        taskId=record.task_id,
        eventType=record.event_type,
        conversationId=record.conversation_id,
        runId=record.run_id,
        agentId=record.agent_id,
        payload=record.payload,
        createdAt=record.created_at,
    )


def _run_response(repository: SQLiteRepository, record: RunRecord) -> RunResponse:
    usage = repository.aggregate_usage(run_id=record.id, include_breakdown=False)
    usage["available"] = bool(usage.get("available_calls")) and not bool(
        usage.get("unavailable_calls")
    )
    calls = repository.list_model_calls(run_id=record.id)
    if calls:
        usage["model"] = calls[-1].model
        usage["call_kind"] = calls[-1].call_kind
    elif isinstance(record.metadata.get("usage"), Mapping):
        usage.update(record.metadata["usage"])
        usage["available"] = True
    return RunResponse(
        id=record.id,
        conversation_id=record.conversation_id,
        status=record.status,
        queue_position=record.queue_position,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        completed_at=record.finished_at,
        cancel_requested_at=record.cancel_requested_at,
        error=_error_text(record.error),
        token_usage=usage,
    )


def _approval_response(record: ApprovalRecord) -> ApprovalResponse:
    summary = str(record.metadata.get("summary") or record.reason or record.tool_name)
    return ApprovalResponse(
        id=record.id,
        run_id=record.run_id,
        tool_name=record.tool_name,
        summary=summary,
        reason=record.reason,
        input=record.tool_input,
        status=record.status,
        decision=record.decision,
        requested_at=record.created_at,
        resolved_at=record.resolved_at,
    )


def _message_preview(record: MessageRecord) -> str:
    content = record.content
    if isinstance(content, str):
        return content[:240]
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ]
        if parts:
            return " ".join(parts)[:240]
    try:
        return json.dumps(content, ensure_ascii=False)[:240]
    except (TypeError, ValueError):
        return str(content)[:240]


def _error_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("message", "error", "detail"):
            if value.get(key):
                return str(value[key])
    return str(value)


def _config_value(config: Any, *names: str) -> Any:
    if config is None:
        return None
    for name in names:
        if isinstance(config, Mapping) and name in config:
            return config[name]
        if hasattr(config, name):
            return getattr(config, name)
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mcp_config_path(runtime_env: Any, workspace: Path) -> Path:
    configured = Path(_config_value(runtime_env, "mcp_config_path") or "mcp.json")
    path = configured if configured.is_absolute() else workspace / configured
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("MCP config must be inside the selected workspace") from exc
    return resolved


def _mcp_config_response(
    workspace: Path,
    config_path: Path,
    *,
    restart_required: bool = False,
) -> McpConfigResponse:
    payload = load_mcp_document(config_path)
    servers: list[McpServerResponse] = []
    for name, raw_value in payload.get("mcpServers", {}).items():
        raw = dict(raw_value)
        transport = "http" if raw.get("url") else "stdio"
        servers.append(
            McpServerResponse(
                name=name,
                transport=transport,
                command=str(raw.get("command", "")),
                args=[str(item) for item in raw.get("args", [])],
                cwd=str(raw["cwd"]) if raw.get("cwd") else None,
                url=str(raw.get("url", "")),
                env_keys=sorted(str(key) for key in raw.get("env", {})),
                header_keys=sorted(str(key) for key in raw.get("headers", {})),
            )
        )
    return McpConfigResponse(
        workspace=str(workspace),
        config_path=str(config_path),
        restart_required=restart_required,
        servers=servers,
    )


def _reload_mcp_runtime(scheduler: Any, workspace: Path) -> bool:
    reload_mcp = getattr(scheduler, "reload_mcp", None)
    return bool(reload_mcp(str(workspace))) if callable(reload_mcp) else False


def _origin_is_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").casefold() in _LOOPBACK_HOSTS


def _is_json_request(request: Any) -> bool:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if media_type == "application/json" or media_type.endswith("+json"):
        return True
    # Cancellation and deletion are command endpoints with no request body.
    # Requiring a synthetic `{}` would be surprising and breaks standard fetch.
    return not media_type and request.headers.get("content-length", "0") in {"", "0"}


def _parse_event_sequence(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value.strip()))
    except (TypeError, ValueError):
        return 0


def _encode_sse(sequence: int, event_type: str, payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


def _static_root(static_dir: str | Path | None) -> Path | None:
    candidate = (
        Path(static_dir)
        if static_dir is not None
        else Path(__file__).resolve().parents[2] / "web" / "dist"
    ).expanduser().resolve()
    if candidate.is_dir() and (candidate / "index.html").is_file():
        return candidate
    return None


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _asset_cache_control(path: Path) -> str:
    if path.name == "index.html":
        return "no-cache"
    if "assets" in path.parts:
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"


__all__ = ["create_app"]
