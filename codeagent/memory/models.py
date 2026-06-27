"""Memory data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MEMORY_TYPES = ("user", "feedback", "project", "reference")


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Runtime settings for long-term memory."""

    enabled: bool = True
    memory_dir: Path = Path(".memory")
    max_items_in_prompt: int = 50
    max_loaded_items: int = 5
    session_budget_chars: int = 60_000
    max_memory_bytes: int = 50_000
    selection_mode: str = "llm"
    auto_extract: bool = False
    extract_recent_messages: int = 12
    consolidate_threshold: int = 30
    consolidate_mode: str = "simple"
    allow_subagent_write: bool = False


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One persisted memory entry."""

    name: str
    description: str
    content: str
    memory_type: str = "project"
    source: str = "manual"
    created_at: str = ""
    updated_at: str = ""
    filename: str = ""

    def clipped_content(self, max_chars: int) -> str:
        if len(self.content) <= max_chars:
            return self.content
        return self.content[:max_chars].rstrip() + "\n\n[truncated]"
