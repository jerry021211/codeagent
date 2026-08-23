"""Default concrete tool set."""

from __future__ import annotations

from collections.abc import Callable

from codeagent.planning import PlanningBackend
from codeagent.tools.bash import BashTool
from codeagent.tools.base import Tool
from codeagent.tools.compact import CompactTool
from codeagent.tools.edit import EditFileTool
from codeagent.tools.glob_tool import GlobTool
from codeagent.tools.grep import GrepTool
from codeagent.memory import MemoryStore
from codeagent.skills import SkillLoader
from codeagent.tools.memory import LoadMemoryTool, RememberTool, SearchMemoryTool
from codeagent.tools.read import ReadFileTool
from codeagent.tools.registry import ToolRegistry
from codeagent.tools.skill import LoadSkillTool
from codeagent.tools.tasks import TaskService, create_task_tools
from codeagent.tools.todo import TodoStore, TodoWriteTool
from codeagent.tools.write import WriteFileTool
from codeagent.tools.workspace import WorkspaceGuard


def default_tools(
    *,
    todo_store: TodoStore | None = None,
    todo_log: Callable[[str], None] | None = None,
    skill_loader: SkillLoader | None = None,
    memory_store: MemoryStore | None = None,
    allow_memory_write: bool = True,
    memory_max_items: int = 5,
    compact_fn: Callable[[], str] | None = None,
    workspace_guard: WorkspaceGuard | None = None,
    changed_files: set[str] | None = None,
    planning_backend: PlanningBackend | str = PlanningBackend.TODO,
    task_service: TaskService | None = None,
    task_list_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    agent_id: str = "agent_root",
) -> list[Tool]:
    backend = PlanningBackend.parse(planning_backend)
    if backend is PlanningBackend.AUTO:
        raise ValueError("Resolve AUTO planning backend before creating tools")
    file_changes = changed_files if changed_files is not None else set()
    tools: list[Tool] = [
        BashTool(workspace_guard=workspace_guard),
        ReadFileTool(workspace_guard=workspace_guard),
        WriteFileTool(
            workspace_guard=workspace_guard,
            changed_files=file_changes,
        ),
        EditFileTool(
            workspace_guard=workspace_guard,
            changed_files=file_changes,
        ),
        GlobTool(workspace_guard=workspace_guard),
        GrepTool(workspace_guard=workspace_guard),
    ]
    if backend is PlanningBackend.TODO:
        tools.append(TodoWriteTool(store=todo_store or TodoStore(), on_change=todo_log))
    else:
        if task_service is None or not task_list_id:
            raise ValueError("Task planning requires task_service and task_list_id")
        tools.extend(
            create_task_tools(
                task_service,
                task_list_id,
                conversation_id=conversation_id,
                run_id=run_id,
                agent_id=agent_id,
            )
        )
    if skill_loader is not None:
        tools.append(LoadSkillTool(loader=skill_loader))
    if memory_store is not None:
        tools.append(SearchMemoryTool(store=memory_store, max_items=memory_max_items))
        tools.append(LoadMemoryTool(store=memory_store))
        if allow_memory_write:
            tools.append(RememberTool(store=memory_store))
    if compact_fn is not None:
        tools.append(CompactTool(compact_fn=compact_fn))
    return tools


def create_default_registry(
    *,
    todo_store: TodoStore | None = None,
    todo_log: Callable[[str], None] | None = None,
    skill_loader: SkillLoader | None = None,
    memory_store: MemoryStore | None = None,
    allow_memory_write: bool = True,
    memory_max_items: int = 5,
    compact_fn: Callable[[], str] | None = None,
    workspace_guard: WorkspaceGuard | None = None,
    changed_files: set[str] | None = None,
    planning_backend: PlanningBackend | str = PlanningBackend.TODO,
    task_service: TaskService | None = None,
    task_list_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    agent_id: str = "agent_root",
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in default_tools(
        todo_store=todo_store,
        todo_log=todo_log,
        skill_loader=skill_loader,
        memory_store=memory_store,
        allow_memory_write=allow_memory_write,
        memory_max_items=memory_max_items,
        compact_fn=compact_fn,
        workspace_guard=workspace_guard,
        changed_files=changed_files,
        planning_backend=planning_backend,
        task_service=task_service,
        task_list_id=task_list_id,
        conversation_id=conversation_id,
        run_id=run_id,
        agent_id=agent_id,
    ):
        registry.register(tool)
    return registry
