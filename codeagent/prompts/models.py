"""Data structures for runtime system prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from codeagent.messages import Message

PromptSection = Literal["static", "dynamic", "reminder"]


class PromptMode(str, Enum):
    """Prompt modes used by parent agents and subagents."""

    NORMAL = "normal"
    SUBAGENT = "subagent"
    SIMPLE = "simple"
    COORDINATOR = "coordinator"


@dataclass(frozen=True, slots=True)
class PromptConfig:
    """Runtime settings for prompt assembly."""

    template_dir: Path | None = None
    system_budget_chars: int = 120_000
    static_budget_chars: int = 50_000
    dynamic_budget_chars: int = 70_000
    skill_catalog_budget_chars: int = 12_000
    context_summary_budget_chars: int = 12_000
    emit_trace: bool = False


@dataclass(frozen=True, slots=True)
class PromptFragment:
    """One independently produced prompt fragment."""

    id: str
    content: str
    priority: int
    section: PromptSection
    source: str
    tags: tuple[str, ...] = ()
    budget_chars: int | None = None
    cacheable: bool = True


@dataclass(frozen=True, slots=True)
class PromptTraceItem:
    """Debug metadata for an included prompt fragment."""

    id: str
    section: PromptSection
    source: str
    chars: int
    clipped: bool = False


@dataclass(slots=True)
class PromptBuildContext:
    """Real runtime state used to assemble the prompt."""

    mode: PromptMode
    base_system_prompt: str
    model: str
    workspace: Path
    tool_schemas: list[dict[str, Any]]
    selected_memory_context: str = ""
    memory_catalog: str = ""
    skill_catalog: str = ""
    context_summary: str = ""
    current_date: str = ""
    extra_reminders: list[str] = field(default_factory=list)
    config: PromptConfig = field(default_factory=PromptConfig)


@dataclass(slots=True)
class PromptAssemblyResult:
    """Final assembled prompt plus observability metadata."""

    system_prompt: str
    system_sections: list[str]
    reminder_messages: list[Message]
    fragments: list[PromptFragment]
    trace: list[PromptTraceItem]
    prompt_hash: str
    cache_hit: bool = False

    def apply_reminders(self, messages: list[Message]) -> list[Message]:
        if not self.reminder_messages:
            return messages
        return [*messages, *self.reminder_messages]
