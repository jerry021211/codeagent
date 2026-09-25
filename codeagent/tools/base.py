"""Tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class ToolInputState:
    """Evidence about the inputs of one operation, without retaining content."""

    fingerprint: str = ""
    known: bool = False
    cwd: str = ""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Tool(Protocol):
    definition: ToolDefinition

    def run(self, **kwargs: Any) -> str:
        raise NotImplementedError


class ToolOutput(str):
    """String-compatible result carrying execution facts before text projection."""

    def __new__(
        cls, text: str, *, status: str = "success", exit_code: int | None = None,
        outcome: str = "", deterministic: bool = False, input_state: str = "",
        state_known: bool = False, result_signature: str = "",
        changed_files: tuple[str, ...] = (), process_id: int | None = None,
        process_running: bool = False, duration_seconds: float = 0.0,
        retryable: bool = False, retry_safe: bool = False,
    ):
        if status not in {"success", "error", "blocked"}:
            raise ValueError(f"Invalid tool status: {status}")
        result = super().__new__(cls, text)
        result.status = status
        result.exit_code = exit_code
        result.outcome = outcome
        result.deterministic = deterministic
        result.input_state = input_state
        result.state_known = state_known
        result.result_signature = result_signature
        result.changed_files = changed_files
        result.process_id = process_id
        result.process_running = process_running
        result.duration_seconds = duration_seconds
        result.retryable = retryable
        result.retry_safe = retry_safe
        return result


def parameter_error(message: str, signature: str) -> ToolOutput:
    return ToolOutput(
        f"Error: {message}", status="error", outcome="parameter_error",
        deterministic=True, result_signature=signature,
    )


def validate_tool_arguments(
    definition: ToolDefinition, args: dict[str, Any],
) -> ToolOutput | None:
    """Validate required fields and basic property types, not full JSON Schema."""
    if not isinstance(args, dict):
        return parameter_error("Tool arguments must be a JSON object.", "arguments:object")
    schema = definition.input_schema
    for name in schema.get("required", []):
        if name not in args:
            return parameter_error(f"Missing required parameter '{name}'; supply it.", f"required:{name}")
    types = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "object": dict, "array": list, "null": type(None),
    }
    for name, property_schema in schema.get("properties", {}).items():
        if name not in args:
            continue
        expected = property_schema.get("type")
        alternatives = expected if isinstance(expected, list) else [expected]
        supported = [kind for kind in alternatives if kind in types]
        if supported and not any(
            isinstance(args[name], types[kind])
            and not (kind in {"integer", "number"} and isinstance(args[name], bool))
            for kind in supported
        ):
            return parameter_error(
                f"Parameter '{name}' must have type {' or '.join(supported)}; correct its value.",
                f"type:{name}:{'|'.join(sorted(supported))}",
            )
    return None


def normalize_tool_output(value: str) -> ToolOutput:
    """Bridge legacy tools; explicit facts always override text heuristics."""
    if isinstance(value, ToolOutput):
        return value
    text = str(value)
    if text.startswith(("Blocked:", "Permission denied")):
        status = "blocked"
    elif text.startswith(("Error:", "Error running command:", "Unknown tool:", "Invalid regex:")):
        status = "error"
    else:
        status = "success"
    return ToolOutput(text, status=status)
