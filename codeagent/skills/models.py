"""Skill loading data models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    when_to_use: str
    path: Path


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    metadata: SkillMetadata
    content: str
