"""Tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

ToolHandler = Callable[..., str]


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
