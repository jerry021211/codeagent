"""Prompt providers for agent capabilities."""

from __future__ import annotations

from typing import Protocol

from codeagent.prompts.loader import PromptTemplateLoader
from codeagent.prompts.models import (
    PromptBuildContext,
    PromptFragment,
    PromptMode,
)
from codeagent.tools import (
    LOAD_MEMORY_TOOL_NAME,
    REMEMBER_TOOL_NAME,
    SEARCH_MEMORY_TOOL_NAME,
    TASK_TOOL_NAME,
)


class PromptProvider(Protocol):
    """Produces prompt fragments from real runtime state."""

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        ...


class BasePromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if context.mode == PromptMode.SUBAGENT:
            content = self.loader.load("subagent")
            source = "templates/subagent.md"
        else:
            content = context.base_system_prompt.strip() or self.loader.load("identity")
            source = "config.system_prompt"
        return [
            PromptFragment(
                id="base.identity",
                content=content,
                priority=100,
                section="static",
                source=source,
                tags=("base",),
            )
        ]


class ExecutionPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if context.mode == PromptMode.SIMPLE:
            return []
        return [
            PromptFragment(
                id="base.execution",
                content=self.loader.load("execution"),
                priority=200,
                section="static",
                source="templates/execution.md",
                tags=("base",),
            )
        ]


class ToolPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        names = _tool_names(context.tool_schemas)
        if not names:
            return []
        content = "\n".join(
            [
                self.loader.load("tools"),
                "",
                "Registered tools:",
                ", ".join(names),
            ]
        )
        return [
            PromptFragment(
                id="tools.available",
                content=content,
                priority=300,
                section="dynamic",
                source="templates/tools.md",
                tags=("tools",),
            )
        ]


class TodoPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if context.mode == PromptMode.SIMPLE or "todo_write" not in _tool_names(
            context.tool_schemas
        ):
            return []
        return [
            PromptFragment(
                id="tools.todo",
                content=self.loader.load("todo"),
                priority=500,
                section="dynamic",
                source="templates/todo.md",
                tags=("todo", "tools"),
            )
        ]


class TaskPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if context.mode in {PromptMode.SUBAGENT, PromptMode.SIMPLE}:
            return []
        if TASK_TOOL_NAME not in _tool_names(context.tool_schemas):
            return []
        return [
            PromptFragment(
                id="tools.task",
                content=self.loader.load("task"),
                priority=600,
                section="dynamic",
                source="templates/task.md",
                tags=("task", "tools"),
            )
        ]


class SkillPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if not context.skill_catalog:
            return []
        content = "\n\n".join([context.skill_catalog, self.loader.load("skill")])
        return [
            PromptFragment(
                id="skills.catalog",
                content=content,
                priority=700,
                section="dynamic",
                source="skill_catalog",
                tags=("skills",),
                budget_chars=context.config.skill_catalog_budget_chars,
            )
        ]


class MemoryPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        fragments: list[PromptFragment] = []
        names = set(_tool_names(context.tool_schemas))
        has_memory_tools = bool(
            names
            & {SEARCH_MEMORY_TOOL_NAME, LOAD_MEMORY_TOOL_NAME, REMEMBER_TOOL_NAME}
        )
        if has_memory_tools:
            fragments.append(
                PromptFragment(
                    id="memory.guidance",
                    content=self.loader.load("memory"),
                    priority=760,
                    section="dynamic",
                    source="templates/memory.md",
                    tags=("memory",),
                )
            )
        if context.selected_memory_context:
            content = "\n\n".join(
                [
                    self.loader.load("memory_context"),
                    "<selected_memories>",
                    context.selected_memory_context,
                    "</selected_memories>",
                ]
            )
            fragments.append(
                PromptFragment(
                    id="memory.selected",
                    content=content,
                    priority=800,
                    section="dynamic",
                    source="memory_manager",
                    tags=("memory",),
                )
            )
        elif context.memory_catalog:
            fragments.append(
                PromptFragment(
                    id="memory.catalog",
                    content=context.memory_catalog,
                    priority=790,
                    section="dynamic",
                    source="memory_catalog",
                    tags=("memory",),
                )
            )
        return fragments


class ContextSummaryPromptProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        if not context.context_summary:
            return []
        content = "\n\n".join(
            [
                self.loader.load("context_summary"),
                "<context_summary>",
                context.context_summary,
                "</context_summary>",
            ]
        )
        return [
            PromptFragment(
                id="context.summary",
                content=content,
                priority=850,
                section="dynamic",
                source="context_manager",
                tags=("context",),
                budget_chars=context.config.context_summary_budget_chars,
            )
        ]


class RuntimeReminderProvider:
    def __init__(self, loader: PromptTemplateLoader) -> None:
        self.loader = loader

    def fragments(self, context: PromptBuildContext) -> list[PromptFragment]:
        reminders: list[str] = []
        workspace = self.loader.load("workspace_reminder").format(
            workspace=context.workspace
        )
        reminders.append(workspace)
        if context.current_date:
            reminders.append(
                self.loader.load("date_reminder").format(
                    current_date=context.current_date
                )
            )
        reminders.extend(context.extra_reminders)
        if not reminders:
            return []
        return [
            PromptFragment(
                id="runtime.reminder",
                content="\n".join(reminders),
                priority=1000,
                section="reminder",
                source="runtime",
                tags=("runtime",),
                cacheable=False,
            )
        ]


def default_prompt_providers(
    loader: PromptTemplateLoader,
) -> list[PromptProvider]:
    return [
        BasePromptProvider(loader),
        ExecutionPromptProvider(loader),
        ToolPromptProvider(loader),
        TodoPromptProvider(loader),
        TaskPromptProvider(loader),
        SkillPromptProvider(loader),
        MemoryPromptProvider(loader),
        ContextSummaryPromptProvider(loader),
        RuntimeReminderProvider(loader),
    ]


def _tool_names(tool_schemas: list[dict]) -> list[str]:
    return [str(schema.get("name", "")) for schema in tool_schemas if schema.get("name")]
