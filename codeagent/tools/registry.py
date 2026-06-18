"""Tool registration and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

from codeagent.tracing import trace_run
from codeagent.tools.base import Tool, ToolDefinition, ToolHandler


@dataclass(slots=True)
class _RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    """Explicit tool schema and handler map."""

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    def register(self, tool: Tool) -> None:
        self.register_handler(tool.definition, tool.run)

    def register_handler(
        self, definition: ToolDefinition, handler: ToolHandler
    ) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = _RegisteredTool(definition, handler)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.definition.to_schema() for tool in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any] | None = None) -> str:
        with trace_run(
            f"tool.{name}",
            run_type="tool",
            inputs={"name": name, "args": args or {}},
        ) as tool_trace:
            registered = self._tools.get(name)
            if registered is None:
                output = f"Unknown tool: {name}"
                tool_trace.end(outputs={"output": output, "status": "unknown_tool"})
                return output
            try:
                output = str(registered.handler(**(args or {})))
            except Exception as exc:
                output = f"Error: {type(exc).__name__}: {exc}"
                tool_trace.end(outputs={"output": output, "status": "error"})
                return output
            tool_trace.end(outputs={"output": output, "status": "ok"})
            return output

    def copy_without(self, names: Iterable[str]) -> ToolRegistry:
        excluded = set(names)
        registry = ToolRegistry()
        for name, registered in self._tools.items():
            if name in excluded:
                continue
            registry.register_handler(registered.definition, registered.handler)
        return registry

    def __contains__(self, name: str) -> bool:
        return name in self._tools
