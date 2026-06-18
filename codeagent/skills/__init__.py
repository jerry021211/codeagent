"""Skill loading extension point."""

from codeagent.skills.loader import SkillLoader
from codeagent.skills.models import LoadedSkill, SkillMetadata

__all__ = ["LoadedSkill", "SkillLoader", "SkillMetadata"]
