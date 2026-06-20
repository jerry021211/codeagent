"""Long-term memory tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.memory import MEMORY_TYPES, MemoryStore
from codeagent.tools.base import ToolDefinition

REMEMBER_TOOL_NAME = "remember"
SEARCH_MEMORY_TOOL_NAME = "search_memory"
LOAD_MEMORY_TOOL_NAME = "load_memory"


@dataclass(slots=True)
class RememberTool:
    """Persist a durable memory for future turns."""

    store: MemoryStore
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=REMEMBER_TOOL_NAME,
            description=(
                "Store a durable memory for future conversations. Use only for "
                "stable user preferences, project conventions, important "
                "decisions, or reusable facts. Do not store secrets or temporary "
                "task status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short unique memory name.",
                    },
                    "type": {
                        "type": "string",
                        "enum": list(MEMORY_TYPES),
                        "description": "Memory category.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line summary shown in the memory catalog.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full memory content to load later.",
                    },
                },
                "required": ["name", "type", "description", "content"],
            },
        ),
        init=False,
    )

    def run(
        self,
        name: str,
        description: str,
        content: str,
        type: str = "project",
    ) -> str:
        record = self.store.remember(
            name=name,
            description=description,
            content=content,
            memory_type=type,
            source="tool",
        )
        return (
            f"[memory saved] {record.name} "
            f"[{record.memory_type}]: {record.description}"
        )


@dataclass(slots=True)
class SearchMemoryTool:
    """Search memory summaries without loading full content."""

    store: MemoryStore
    max_items: int = 5
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=SEARCH_MEMORY_TOOL_NAME,
            description=(
                "Search long-term memories by keyword. Returns concise "
                "summaries; use load_memory to read a selected memory in full."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords related to the needed memory.",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Maximum number of matching memories to return.",
                    },
                },
                "required": ["query"],
            },
        ),
        init=False,
    )

    def run(self, query: str, max_items: int | None = None) -> str:
        limit = max(1, min(int(max_items or self.max_items), 20))
        records = self.store.search(query, max_items=limit)
        if not records:
            return "No matching memories."
        lines = ["Matching memories:"]
        for record in records:
            lines.append(
                f"- {record.name} [{record.memory_type}]: {record.description}"
            )
        return "\n".join(lines)


@dataclass(slots=True)
class LoadMemoryTool:
    """Load one full memory entry."""

    store: MemoryStore
    max_chars: int = 50_000
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name=LOAD_MEMORY_TOOL_NAME,
            description="Load the full content of one long-term memory by exact name.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact memory name from the catalog or search results.",
                    }
                },
                "required": ["name"],
            },
        ),
        init=False,
    )

    def run(self, name: str) -> str:
        try:
            record = self.store.load(name)
        except KeyError:
            available = ", ".join(item.name for item in self.store.list_memories())
            if available:
                return f"Memory not found: {name}. Available memories: {available}"
            return f"Memory not found: {name}. No memories are available."
        return (
            f"[memory loaded] {record.name} [{record.memory_type}]\n"
            f"{record.description}\n\n{record.clipped_content(self.max_chars)}"
        )
