"""Long-term memory tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.memory import MEMORY_TYPES, MemoryStore, MemoryWriteBlocked
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
                "保存稳定且对后续任务有用的记忆。内容需有依据，不保存密钥或临时任务进度。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "简短且唯一的记忆名称",
                    },
                    "type": {
                        "type": "string",
                        "enum": list(MEMORY_TYPES),
                        "description": "记忆类别",
                    },
                    "description": {
                        "type": "string",
                        "description": "在目录中显示的一句话摘要",
                    },
                    "content": {
                        "type": "string",
                        "description": "供后续加载的完整记忆内容",
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
        try:
            record = self.store.remember(
                name=name,
                description=description,
                content=content,
                memory_type=type,
                source="tool",
            )
        except MemoryWriteBlocked as exc:
            return f"Blocked: {exc}"
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
                "按关键词搜索长期记忆摘要；需要全文时再用 load_memory。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "与所需记忆相关的关键词",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "最多返回的匹配记忆条数",
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
            description="按准确名称加载一条长期记忆全文。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "目录或搜索结果中的准确记忆名称",
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
