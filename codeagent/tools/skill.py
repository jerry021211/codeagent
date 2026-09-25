"""Skill loading tool."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.skills import SkillLoader
from codeagent.tools.base import ToolDefinition

LOAD_SKILL_TOOL_NAME = "load_skill"


@dataclass(slots=True)
class LoadSkillTool:
    """Load full instructions for a registered skill."""

    loader: SkillLoader
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=LOAD_SKILL_TOOL_NAME,
            description=(
                "按目录中的准确名称用 load_skill(name) 加载技能全文，再应用其专业流程。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "可用技能目录中的准确名称",
                    }
                },
                "required": ["name"],
            },
        ),
        init=False,
    )

    def run(self, name: str) -> str:
        try:
            loaded = self.loader.load(name)
        except KeyError:
            available = ", ".join(skill.name for skill in self.loader.list_skills())
            if not available:
                return f"Skill not found: {name}. No skills are available."
            return f"Skill not found: {name}. Available skills: {available}"

        return f"[skill loaded] {loaded.metadata.name}\n\n{loaded.content}"
