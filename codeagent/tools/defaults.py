"""Default concrete tool set."""

from __future__ import annotations

from collections.abc import Callable

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
from codeagent.tools.task import TaskTool
from codeagent.tools.todo import TodoStore, TodoWriteTool
from codeagent.tools.write import WriteFileTool


def default_tools(
    *,
    todo_store: TodoStore | None = None,
    todo_log: Callable[[str], None] | None = None,
    skill_loader: SkillLoader | None = None,
    memory_store: MemoryStore | None = None,
    allow_memory_write: bool = True,
    memory_max_items: int = 5,
    compact_fn: Callable[[], str] | None = None,
    task_spawn_fn: Callable[[str], str] | None = None,
) -> list[Tool]:
    store = todo_store or TodoStore()
    tools: list[Tool] = [
        BashTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        TodoWriteTool(store=store, on_change=todo_log),
    ]
    if skill_loader is not None:
        tools.append(LoadSkillTool(loader=skill_loader))
    if memory_store is not None:
        tools.append(SearchMemoryTool(store=memory_store, max_items=memory_max_items))
        tools.append(LoadMemoryTool(store=memory_store))
        if allow_memory_write:
            tools.append(RememberTool(store=memory_store))
    if compact_fn is not None:
        tools.append(CompactTool(compact_fn=compact_fn))
    if task_spawn_fn is not None:
        tools.append(TaskTool(spawn_fn=task_spawn_fn))
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
    task_spawn_fn: Callable[[str], str] | None = None,
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
        task_spawn_fn=task_spawn_fn,
    ):
        registry.register(tool)
    return registry
