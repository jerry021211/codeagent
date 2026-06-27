"""Planning-only todo tool and reminder support."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from codeagent.tools.base import ToolDefinition

VALID_TODO_STATUSES = {"pending", "in_progress", "completed"}
TodoLogger = Callable[[str], None]


@dataclass(slots=True)
class TodoStore:
    """In-memory todo state for one agent process."""

    todos: list[dict[str, str]] = field(default_factory=list)
    revision: int = 0

    def replace(self, todos: list[dict[str, str]]) -> None:
        self.todos = todos
        self.revision += 1

    def clear(self) -> None:
        self.todos = []
        self.revision += 1

    def has_open_work(self) -> bool:
        return any(todo["status"] != "completed" for todo in self.todos)

    def format(self) -> str:
        if not self.todos:
            return "Current todo list is empty."

        lines = ["Current todo list:"]
        for todo in self.todos:
            marker = {
                "pending": " ",
                "in_progress": ">",
                "completed": "x",
            }[todo["status"]]
            lines.append(f"[{marker}] {todo['content']}")
        return "\n".join(lines)

    def reminder(self) -> str:
        if not self.todos:
            return (
                "<reminder>Before continuing a multi-step task, call todo_write "
                "with a concise plan. The todo_write tool is planning-only; it "
                "does not read files, run commands, or make edits.</reminder>"
            )

        if self.has_open_work():
            return (
                "<reminder>Update todo_write before continuing. Mark completed "
                "items, keep exactly one active item as in_progress, and make "
                f"sure the next step is clear.\n{self.format()}</reminder>"
            )

        return (
            "<reminder>Your todo list is complete. If the task now requires "
            "verification or a final summary, add or confirm that step with "
            "todo_write before finishing.</reminder>"
        )


@dataclass(slots=True)
class TodoWriteTool:
    """Create or update the agent's planning checklist."""

    store: TodoStore = field(default_factory=TodoStore)
    on_change: TodoLogger | None = None
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="todo_write",
            description=(
                "Create or update a planning checklist for the current task. "
                "Use this before starting multi-step work and update it as "
                "steps move through pending, in_progress, and completed. "
                "This tool only records planning state; it cannot read files, "
                "run commands, or change the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "Full replacement todo list for the current task."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Concrete task step.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                    ],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        ),
        init=False,
    )

    def run(self, todos: Any) -> str:
        normalized, error = normalize_todos(todos)
        if error:
            return error

        previous = _copy_todos(self.store.todos)
        self.store.replace(normalized)
        if self.on_change is not None:  #on_change其实是一个print函数，cli中注册的
            event = render_todo_event(previous, normalized)
            if event is not None:
                self.on_change(event)
        return f"Updated {len(normalized)} todos.\n{self.store.format()}"


def normalize_todos(todos: Any) -> tuple[list[dict[str, str]], str | None]:
    """Validate and normalize todo_write input without executing code."""

    if isinstance(todos, str):
        todos, error = _parse_todos_string(todos)
        if error:
            return [], error

    if not isinstance(todos, list):
        return [], "Error: todos must be a list"

    normalized: list[dict[str, str]] = []
    in_progress_count = 0
    for index, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return [], f"Error: todos[{index}] must be an object"

        raw_content = todo.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            return [], f"Error: todos[{index}].content must be a non-empty string"

        raw_status = todo.get("status")
        if raw_status not in VALID_TODO_STATUSES:
            return [], (
                f"Error: todos[{index}].status must be one of "
                "pending, in_progress, completed"
            )

        if raw_status == "in_progress":
            in_progress_count += 1

        normalized.append(
            {
                "content": raw_content.strip(),
                "status": str(raw_status),
            }
        )

    if in_progress_count > 1:
        return [], "Error: only one todo can be in_progress at a time"

    return normalized, None


def _parse_todos_string(value: str) -> tuple[Any, str | None]:
    try:
        return json.loads(value), None
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(value), None
    except (SyntaxError, ValueError):
        return [], "Error: todos must be a list or JSON array string"


def render_todo_event(
    previous: list[dict[str, str]],
    current: list[dict[str, str]],
) -> str | None:  #这个函数是对比todo列表修改后和之前的变化的
    """Render a user-facing todo state transition."""

    if previous == current:
        return None

    if current and all(todo["status"] == "completed" for todo in current):
        title = "[todo completed]"
    elif not previous and current:
        title = "[todo created]"
    else:
        title = "[todo updated]"

    lines = [title]
    changes = _todo_changes(previous, current)
    if previous and changes:
        lines.append("  changed:")
        lines.extend(f"    {change}" for change in changes)
        lines.append("")

    lines.append("  current:")
    lines.extend(_format_numbered_todos(current, indent="    "))
    return "\n".join(lines)


def render_todo_final_status(store: TodoStore) -> str | None:
    if not store.todos or not store.has_open_work():
        return None
    return "\n".join(
        [
            "[todo final]",
            "  current:",
            *_format_numbered_todos(store.todos, indent="    "),
        ]
    )


def create_todo_final_status_hook(store: TodoStore, log: TodoLogger):
    """Return a Stop hook that prints unfinished todo state once per revision."""

    last_reported_revision = -1

    def hook(messages: list[dict[str, Any]]) -> None:
        nonlocal last_reported_revision

        if store.revision == last_reported_revision:
            return None

        final_status = render_todo_final_status(store)
        if final_status is None:
            return None

        last_reported_revision = store.revision
        log(final_status)
        return None

    return hook


def _todo_changes(
    previous: list[dict[str, str]],
    current: list[dict[str, str]],
) -> list[str]:
    changes: list[str] = []
    max_len = max(len(previous), len(current))
    for index in range(max_len):
        old = previous[index] if index < len(previous) else None
        new = current[index] if index < len(current) else None

        if old is None and new is not None:
            changes.append(
                f"{index + 1}. added: {new['content']} ({new['status']})"
            )
            continue

        if old is not None and new is None:
            changes.append(
                f"{index + 1}. removed: {old['content']} ({old['status']})"
            )
            continue

        if old is None or new is None or old == new:
            continue

        if old["content"] == new["content"] and old["status"] != new["status"]:
            changes.append(
                f"{index + 1}. {new['content']}: "
                f"{old['status']} -> {new['status']}"
            )
            continue

        changes.append(
            f"{index + 1}. changed: {old['content']} ({old['status']}) "
            f"-> {new['content']} ({new['status']})"
        )
    return changes


def _format_numbered_todos(
    todos: list[dict[str, str]],
    *,
    indent: str,
) -> list[str]:
    if not todos:
        return [f"{indent}(empty)"]

    return [
        f"{indent}{index}. [{_todo_marker(todo['status'])}] {todo['content']}"
        for index, todo in enumerate(todos, start=1)
    ]


def _todo_marker(status: str) -> str:
    return {
        "pending": " ",
        "in_progress": ">",
        "completed": "x",
    }[status]


def _copy_todos(todos: list[dict[str, str]]) -> list[dict[str, str]]: #浅拷贝就够了
    return [dict(todo) for todo in todos]


def create_todo_reminder_hook(
    store: TodoStore,
    *,
    interval: int = 3,
):  #如果模型做了三步了还没解决，就提醒模型要写todolist了
    """Return a BeforeModelCall hook that nudges stale planning state."""

    last_revision = store.revision
    rounds_since_update = 0

    def hook(messages: list[dict[str, Any]]) -> str | None:
        nonlocal last_revision, rounds_since_update

        if interval <= 0:
            return None

        if store.revision != last_revision:
            last_revision = store.revision
            rounds_since_update = 0
            return None

        # Do not remind before the model has had a chance to respond once.
        if not any(message.get("role") == "assistant" for message in messages):
            return None

        rounds_since_update += 1
        if rounds_since_update < interval:
            return None

        rounds_since_update = 0
        return store.reminder()

    return hook
