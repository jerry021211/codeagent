"""Data structures for runtime system prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

PromptSection = Literal["static", "dynamic"]


class PromptMode(str, Enum):
    """The two system prompt variants used by Agent."""

    NORMAL = "normal"
    SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class PromptConfig:
    """Runtime settings for prompt assembly."""

    template_dir: Path | None = None
    system_budget_chars: int = 120_000
    static_budget_chars: int = 50_000
    dynamic_budget_chars: int = 70_000
    skill_catalog_budget_chars: int = 12_000
    emit_trace: bool = False


@dataclass(frozen=True, slots=True)
class PromptFragment:
    id: str
    content: str
    section: PromptSection
    source: str
    budget_chars: int | None = None


@dataclass(frozen=True, slots=True)
class PromptTraceItem:
    id: str
    section: PromptSection
    source: str
    chars: int
    clipped: bool = False


@dataclass(slots=True)
class PromptAssemblyResult:
    system_prompt: str
    trace: list[PromptTraceItem]
    prompt_hash: str
