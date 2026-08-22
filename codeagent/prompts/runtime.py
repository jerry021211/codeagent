"""Runtime system prompt assembly."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from codeagent.prompts.models import (
    PromptAssemblyResult,
    PromptConfig,
    PromptFragment,
    PromptMode,
    PromptSection,
    PromptTraceItem,
)
from codeagent.runtime_platform import RuntimePlatform, current_runtime_platform
from codeagent.tools import (
    LOAD_MEMORY_TOOL_NAME,
    REMEMBER_TOOL_NAME,
    SEARCH_MEMORY_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
)

DYNAMIC_BOUNDARY = "<SYSTEM_PROMPT_DYNAMIC_BOUNDARY />"


class PromptRuntime:
    """Build the system prompt from the Agent's current capabilities and state."""

    def __init__(
        self,
        *,
        workspace: Path | str,
        config: PromptConfig | None = None,
        runtime_platform: RuntimePlatform | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.config = config or PromptConfig()
        self.runtime_platform = runtime_platform or current_runtime_platform()
        self.builtin_template_dir = Path(__file__).with_name("templates")

    def assemble(
        self,
        *,
        mode: PromptMode,
        base_system_prompt: str,
        tool_schemas: list[dict],
        selected_memory_context: str = "",
        memory_catalog: str = "",
        skill_catalog: str = "",
    ) -> PromptAssemblyResult:
        fragments = self._build_fragments(
            mode=mode,
            base_system_prompt=base_system_prompt,
            tool_schemas=tool_schemas,
            selected_memory_context=selected_memory_context,
            memory_catalog=memory_catalog,
            skill_catalog=skill_catalog,
        )
        return self._assemble(fragments)

    def memory_turn_context(self, selected_memory_context: str) -> str:
        return "\n\n".join(
            [
                self._load_template("memory_context"),
                "<selected_memories>",
                selected_memory_context,
                "</selected_memories>",
            ]
        )

    def _build_fragments(
        self,
        *,
        mode: PromptMode,
        base_system_prompt: str,
        tool_schemas: list[dict],
        selected_memory_context: str,
        memory_catalog: str,
        skill_catalog: str,
    ) -> list[PromptFragment]:
        fragments: list[PromptFragment] = []
        tool_names = [str(schema["name"]) for schema in tool_schemas]
        tools = set(tool_names)

        if mode == PromptMode.SUBAGENT:
            identity = self._load_template("subagent")
            identity_source = "templates/subagent.md"
        else:
            identity = base_system_prompt.strip() or self._load_template("identity")
            identity_source = "config.system_prompt"
        self._add(
            fragments,
            "base.identity",
            identity,
            section="static",
            source=identity_source,
        )
        self._add_template(
            fragments,
            "base.execution",
            "execution",
            section="static",
        )

        if tool_names:
            self._add(
                fragments,
                "tools.available",
                "\n".join(
                    [
                        self._load_template("tools"),
                        "",
                        "Registered tools:",
                        ", ".join(tool_names),
                    ]
                ),
                source="templates/tools.md",
            )
        if "TaskCreate" in tools:
            self._add_template(fragments, "tools.tasks", "tasks")
        if "todo_write" in tools:
            self._add_template(fragments, "tools.todo", "todo")
        if mode == PromptMode.NORMAL and SUBAGENT_TOOL_NAME in tools:
            self._add_template(fragments, "tools.subagent", "subagent_tool")

        if skill_catalog:
            self._add(
                fragments,
                "skills.catalog",
                "\n\n".join([skill_catalog, self._load_template("skill")]),
                source="skill_catalog",
                budget_chars=self.config.skill_catalog_budget_chars,
            )

        memory_tools = {
            SEARCH_MEMORY_TOOL_NAME,
            LOAD_MEMORY_TOOL_NAME,
            REMEMBER_TOOL_NAME,
        }
        if tools & memory_tools:
            self._add_template(fragments, "memory.guidance", "memory")
        if memory_catalog and not selected_memory_context:
            self._add(
                fragments,
                "memory.catalog",
                memory_catalog,
                source="memory_catalog",
            )
        if selected_memory_context:
            self._add(
                fragments,
                "memory.selected",
                self.memory_turn_context(selected_memory_context),
                source="memory_manager",
            )

        runtime_facts = [
            self._load_template("workspace_reminder").format(
                workspace=self.workspace
            )
        ]
        if "bash" in tools:
            runtime_facts.append(self.runtime_platform.prompt_reminder())
        runtime_facts.append(
            self._load_template("date_reminder").format(
                current_date=date.today().isoformat()
            )
        )
        self._add(
            fragments,
            "runtime.reminder",
            "\n".join(runtime_facts),
            source="runtime",
        )
        return fragments

    def _add_template(
        self,
        fragments: list[PromptFragment],
        fragment_id: str,
        template_name: str,
        *,
        section: PromptSection = "dynamic",
    ) -> None:
        self._add(
            fragments,
            fragment_id,
            self._load_template(template_name),
            section=section,
            source=f"templates/{template_name}.md",
        )

    @staticmethod
    def _add(
        fragments: list[PromptFragment],
        fragment_id: str,
        content: str,
        *,
        section: PromptSection = "dynamic",
        source: str,
        budget_chars: int | None = None,
    ) -> None:
        if content.strip():
            fragments.append(
                PromptFragment(
                    id=fragment_id,
                    content=content,
                    section=section,
                    source=source,
                    budget_chars=budget_chars,
                )
            )

    def _assemble(self, fragments: list[PromptFragment]) -> PromptAssemblyResult:
        static, static_trace = _build_section(
            fragments,
            "static",
            self.config.static_budget_chars,
        )
        dynamic, dynamic_trace = _build_section(
            fragments,
            "dynamic",
            self.config.dynamic_budget_chars,
        )

        sections = [static]
        if dynamic:
            sections.extend([DYNAMIC_BOUNDARY, dynamic])
        system_prompt, _ = _clip(
            "\n\n".join(section for section in sections if section),
            self.config.system_budget_chars,
        )
        return PromptAssemblyResult(
            system_prompt=system_prompt,
            trace=static_trace + dynamic_trace,
            prompt_hash=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12],
        )

    def _load_template(self, name: str) -> str:
        filename = f"{name}.md"
        if self.config.template_dir is not None:
            directory = self.config.template_dir
            if not directory.is_absolute():
                directory = self.workspace / directory
            path = directory / filename
            if path.exists():
                return path.read_text(encoding="utf-8").strip()

        project_template = self.workspace / ".prompts" / filename
        if project_template.exists():
            return project_template.read_text(encoding="utf-8").strip()
        return (self.builtin_template_dir / filename).read_text(encoding="utf-8").strip()


def _build_section(
    fragments: list[PromptFragment],
    section: PromptSection,
    budget: int,
) -> tuple[str, list[PromptTraceItem]]:
    parts: list[str] = []
    trace: list[PromptTraceItem] = []
    remaining = max(0, budget)
    for fragment in fragments:
        if fragment.section != section or remaining <= 0:
            continue
        limit = remaining
        if fragment.budget_chars is not None:
            limit = min(limit, fragment.budget_chars)
        content, clipped = _clip(fragment.content.strip(), limit)
        if not content:
            continue
        parts.append(content)
        trace.append(
            PromptTraceItem(
                id=fragment.id,
                section=fragment.section,
                source=fragment.source,
                chars=len(content),
                clipped=clipped,
            )
        )
        remaining -= len(content)
    return "\n\n".join(parts), trace


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(value)
    if len(value) <= limit:
        return value, False
    suffix = "\n[truncated]"
    if limit <= len(suffix):
        return value[:limit], True
    return value[: limit - len(suffix)].rstrip() + suffix, True
