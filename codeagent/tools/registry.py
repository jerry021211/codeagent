"""Tool registration and dispatch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from codeagent.tracing import trace_run
from codeagent.runtime.cancellation import CancelledError
from codeagent.tools.base import (
    Tool, ToolDefinition, ToolHandler, ToolInputState, ToolOutput,
    normalize_tool_output, parameter_error, validate_tool_arguments,
)


@dataclass(slots=True)
class _RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler
    input_state: Callable[[dict[str, Any]], ToolInputState] | None = None


class ToolRegistry:
    """Explicit tool schema and handler map."""

    def __init__(
        self,
        execution_wrapper: Callable[[str, dict[str, Any], ToolHandler], str]
        | None = None,
    ) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        self._execution_wrapper = execution_wrapper

    def register(self, tool: Tool) -> None:
        provider = getattr(tool, "input_state", None)
        self.register_handler(tool.definition, tool.run, provider if callable(provider) else None)

    def register_handler(
        self, definition: ToolDefinition, handler: ToolHandler,
        input_state: Callable[[dict[str, Any]], ToolInputState] | None = None,
    ) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = _RegisteredTool(definition, handler, input_state)

    def bind_runtime(
        self, cancellation_check: Callable[[], None], remaining_seconds: Callable[[], float],
    ) -> None:
        """Share this execution's cancellation and time budget with bound tools."""
        for registered in self._tools.values():
            owner = getattr(registered.handler, "__self__", None)
            bind = getattr(owner, "bind_runtime", None)
            if callable(bind):
                bind(cancellation_check=cancellation_check, remaining_seconds=remaining_seconds)

    def input_state(self, name: str, args: dict[str, Any]) -> ToolInputState:
        registered = self._tools.get(name)
        if registered is not None and registered.input_state is not None:
            return registered.input_state(args)
        owner = getattr(registered.handler, "__self__", None) if registered else None
        cwd = getattr(owner, "cwd", None)
        return ToolInputState(cwd=str(cwd if cwd is not None else Path.cwd()))

    def parameter_error(self, name: str, args: dict[str, Any]) -> ToolOutput | None:
        registered = self._tools.get(name)
        if registered is None:
            return parameter_error(
                f"Unknown tool: {name}. Choose a tool from the available tool schemas.",
                "unknown_tool",
            )
        from codeagent.tools.edit import EditFileTool

        owner = getattr(registered.handler, "__self__", None)
        if isinstance(owner, EditFileTool):
            return owner.parameter_error(args)
        return validate_tool_arguments(registered.definition, args)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.definition.to_schema() for tool in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolOutput:
        arguments = {} if args is None else args
        with trace_run(
            f"tool.{name}",
            run_type="tool",
            inputs={"name": name, "args": arguments},
        ) as tool_trace:
            invalid = self.parameter_error(name, arguments)
            if invalid is not None:
                tool_trace.end(
                    outputs={"output": str(invalid), "status": invalid.status},
                    error=str(invalid),
                )
                return invalid
            registered = self._tools[name]
            try:
                output = (
                    self._execution_wrapper(name, arguments, registered.handler)
                    if self._execution_wrapper is not None
                    else registered.handler(**arguments)
                )
            except CancelledError:
                raise
            except Exception as exc:
                output = f"Error: {type(exc).__name__}: {exc}"
                tool_trace.end(
                    outputs={"output": output, "status": "error"},
                    error=output,
                )
                return normalize_tool_output(output)
            output = normalize_tool_output(output)
            tool_trace.end(
                outputs={"output": str(output), "status": output.status, "exit_code": output.exit_code},
                error=str(output) if output.status != "success" else None,
            )
            return output

    def copy_without(self, names: Iterable[str]) -> ToolRegistry:
        excluded = set(names)
        registry = ToolRegistry(self._execution_wrapper)
        for name, registered in self._tools.items():
            if name in excluded:
                continue
            registry.register_handler(registered.definition, registered.handler, registered.input_state)
        return registry

    def with_execution_wrapper(
        self,
        wrapper: Callable[[str, dict[str, Any], ToolHandler], str],
    ) -> ToolRegistry:
        """Copy registrations into a registry with one execution boundary."""

        registry = ToolRegistry(wrapper)
        for registered in self._tools.values():
            registry.register_handler(registered.definition, registered.handler, registered.input_state)
        return registry

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def tool_schema_hash(schemas: list[dict[str, Any]]) -> str:
    """Return a stable fingerprint for the tools exposed to the model."""

    ordered = sorted(schemas, key=lambda schema: str(schema.get("name", "")))
    payload = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
