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
        tool_schemas: list[dict],
        selected_memory_context: str = "",
        memory_catalog: str = "",
        skill_catalog: str = "",
        tool_schema_changed: bool = False,
    ) -> PromptAssemblyResult:
        fragments = self._build_fragments(
            mode=mode,
            tool_schemas=tool_schemas,
            selected_memory_context=selected_memory_context,
            memory_catalog=memory_catalog,
            skill_catalog=skill_catalog,
            tool_schema_changed=tool_schema_changed,
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
        tool_schemas: list[dict],
        selected_memory_context: str,
        memory_catalog: str,
        skill_catalog: str,
        tool_schema_changed: bool,
    ) -> list[PromptFragment]:
        fragments: list[PromptFragment] = []
        tool_names = [str(schema["name"]) for schema in tool_schemas]
        tools = set(tool_names)

        single_agent = mode in {PromptMode.NORMAL, PromptMode.DISCUSS}
        if single_agent:
            self._add_template(fragments, "base.core", "core", section="static", required=True)

        identity_template = {
            PromptMode.TEAM_PLANNER: "team_planner",
            PromptMode.TEAM_LEAD: "team_lead_identity",
            PromptMode.TEAMMATE_PLAN: "teammate_plan",
            PromptMode.TEAMMATE_WORK: "teammate_work",
            PromptMode.TEAMMATE_ANALYSIS: "teammate_analysis",
            PromptMode.SUBAGENT: "subagent",
            PromptMode.DISCUSS: "discuss",
        }.get(mode, "identity")
        self._add(
            fragments,
            "base.identity",
            self._load_template(identity_template),
            section="static",
            source=self._template_source(identity_template),
            required=single_agent,
        )
        if mode is PromptMode.NORMAL:
            self._add_template(
                fragments,
                "base.execution",
                "execution",
                section="static",
                required=True,
            )

        project_instructions = self._project_instructions()
        if project_instructions:
            self._add(
                fragments,
                "project.instructions",
                project_instructions,
                section="static",
                source=".prompts/project.md",
                required=single_agent,
            )
        if tool_schema_changed:
            self._add_template(
                fragments,
                "tools.changed",
                "tool_change",
                required=single_agent,
            )
        if "TaskCreate" in tools and mode not in {PromptMode.TEAM_PLANNER, PromptMode.DISCUSS}:
            self._add_template(fragments, "tools.tasks", "tasks")
        if "todo_write" in tools and "TaskCreate" not in tools and mode is not PromptMode.DISCUSS:
            self._add_template(fragments, "tools.todo", "todo")
        if mode == PromptMode.NORMAL and SUBAGENT_TOOL_NAME in tools:
            self._add_template(fragments, "tools.subagent", "subagent_tool")

        if skill_catalog and "load_skill" in tools:
            self._add_template(fragments, "skills.guidance", "skill")
            self._add(
                fragments,
                "skills.catalog",
                skill_catalog,
                source="skill_catalog",
                budget_chars=self.config.skill_catalog_budget_chars,
                trim_lines=True,
            )

        memory_tools = {
            SEARCH_MEMORY_TOOL_NAME,
            LOAD_MEMORY_TOOL_NAME,
            REMEMBER_TOOL_NAME,
        }
        if tools & memory_tools:
            self._add_template(fragments, "memory.guidance", "memory")
        if REMEMBER_TOOL_NAME in tools and mode is not PromptMode.DISCUSS:
            self._add_template(fragments, "memory.write", "memory_write")
        if memory_catalog and not selected_memory_context:
            self._add(
                fragments,
                "memory.catalog",
                memory_catalog,
                source="memory_catalog",
                trim_lines=True,
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

        if single_agent:
            runtime_facts.append(
                self._load_template("mode_reminder").format(
                    mode_label="Discuss · 只读讨论" if mode is PromptMode.DISCUSS else "Code · 编码",
                    mode_rules=(
                        "当前仅允许只读探索和讨论，不执行写入。"
                        if mode is PromptMode.DISCUSS
                        else "当前未启用 Discuss 只读限制；用户要求实现或修复时，可在授权范围内执行修改，无需再次要求用户退出 Discuss。"
                    ),
                )
            )
        self._add(
            fragments,
            "runtime.reminder",
            "\n".join(runtime_facts),
            source="runtime",
            required=single_agent,
        )
        return fragments

    def mode_turn_context(self, mode: PromptMode) -> str:
        """Record an actual runtime mode change next to the new user turn."""
        if mode not in {PromptMode.NORMAL, PromptMode.DISCUSS}:
            raise ValueError("Mode turn context is only available for ordinary Agents")
        return self._load_template("mode_turn").format(
            mode_label="Discuss · 只读讨论" if mode is PromptMode.DISCUSS else "Code · 编码",
            mode_rules=(
                "Discuss 只读限制已启用；仅允许读取、搜索和讨论，不执行写入。"
                if mode is PromptMode.DISCUSS
                else "Discuss 只读限制已解除；可以在用户授权范围内编写和修改文件。不要要求用户再次切换模式。"
            ),
        )

    def _add_template(
        self,
        fragments: list[PromptFragment],
        fragment_id: str,
        template_name: str,
        *,
        section: PromptSection = "dynamic",
        required: bool = False,
    ) -> None:
        self._add(
            fragments,
            fragment_id,
            self._load_template(template_name),
            section=section,
            source=self._template_source(template_name),
            required=required,
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
        required: bool = False,
        trim_lines: bool = False,
    ) -> None:
        if content.strip():
            fragments.append(
                PromptFragment(
                    id=fragment_id,
                    content=content,
                    section=section,
                    source=source,
                    budget_chars=budget_chars,
                    required=required,
                    trim_lines=trim_lines,
                )
            )

    def _assemble(self, fragments: list[PromptFragment]) -> PromptAssemblyResult:
        ordered = sorted(fragments, key=lambda f: f.section != "static")
        limits = {
            "static": max(0, self.config.static_budget_chars),
            "dynamic": max(0, self.config.dynamic_budget_chars),
        }
        total_limit = max(0, self.config.system_budget_chars)
        selected: dict[int, str] = {
            i: f.content.strip() for i, f in enumerate(ordered) if f.required
        }

        def used(section: str | None = None) -> int:
            parts = [text for i, text in selected.items()
                     if section is None or ordered[i].section == section]
            return sum(map(len, parts)) + 2 * max(0, len(parts) - 1)

        for section, limit in limits.items():
            if used(section) > limit:
                raise ValueError(f"提示词必要片段超过 {section} 预算：{used(section)} > {limit}")
        if used() > total_limit:
            raise ValueError(f"提示词必要片段超过 system 预算：{used()} > {total_limit}")

        for i, fragment in enumerate(ordered):
            if fragment.required:
                continue
            text = fragment.content.strip()
            section_count = sum(ordered[j].section == fragment.section for j in selected)
            allowance = min(
                limits[fragment.section] - used(fragment.section) - (2 if section_count else 0),
                total_limit - used() - (2 if selected else 0),
            )
            if fragment.budget_chars is not None:
                allowance = min(allowance, fragment.budget_chars)
            if len(text) <= allowance:
                selected[i] = text
            elif fragment.trim_lines and allowance > 0:
                # Catalogs use complete lines; never split a record or a name.
                lines: list[str] = []
                for line in text.splitlines():
                    candidate = "\n".join([*lines, line])
                    if len(candidate) > allowance:
                        break
                    lines.append(line)
                if len(lines) > 1:
                    selected[i] = "\n".join(lines)
            # Other optional fragments (including XML-wrapped memories) are atomic.

        trace = []
        for i, fragment in enumerate(ordered):
            content = selected.get(i, "")
            original = fragment.content.strip()
            trace.append(PromptTraceItem(
                id=fragment.id, section=fragment.section, source=fragment.source,
                chars=len(content), clipped=bool(content) and content != original,
                included=i in selected, original_chars=len(original),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()[:12],
                dropped_reason=None if i in selected else "budget",
            ))
        system_prompt = "\n\n".join(selected[i] for i in range(len(ordered)) if i in selected)
        return PromptAssemblyResult(
            system_prompt=system_prompt, trace=trace,
            prompt_hash=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12],
        )

    def _load_template(self, name: str) -> str:
        return self._template_path(name).read_text(encoding="utf-8").strip()

    def _template_path(self, name: str) -> Path:
        filename = f"{name}.md"
        if self.config.template_dir is not None:
            directory = self.config.template_dir
            if not directory.is_absolute():
                directory = self.workspace / directory
            path = directory / filename
            if path.exists():
                return path
        return self.builtin_template_dir / filename

    def _template_source(self, name: str) -> str:
        path = self._template_path(name)
        if path.parent == self.builtin_template_dir:
            return f"templates/{path.name}"
        return str(path)

    def _project_instructions(self) -> str:
        path = self.workspace / ".prompts" / "project.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
