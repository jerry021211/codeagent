"""Context compaction configuration and runtime state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codeagent.messages import ToolUse


@dataclass(slots=True)
class ContextConfig:
    mode: str = "model"
    summarization_model: str = ""
    summarization_api_key: str | None = None
    tool_result_budget_chars: int = 200_000
    single_tool_output_max_chars: int = 80_000
    compact_threshold_chars: int = 300_000
    summary_max_chars: int = 12_000
    transcript_dir: Path = Path(".transcripts")
    tool_output_dir: Path = Path(".task_outputs/tool-results")
    reactive_retries: int = 1
    persisted_preview_chars: int = 2_000

    def __post_init__(self) -> None:
        if self.mode not in {"model", "off"}:
            raise ValueError("CONTEXT_COMPACT_MODE must be 'model' or 'off'")


@dataclass(slots=True)
class RuntimeState:
    user_goal: str = ""
    history_generation: int = 0
    files_read: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    tool_artifacts: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    subagent_results: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    important_notes: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)

    def set_user_goal(self, prompt: str) -> None:
        if prompt.strip():
            self.user_goal = prompt.strip()

    def record_tool_result(self, tool_use: ToolUse, output: str) -> None:
        self.tool_call_counts[tool_use.name] = (
            self.tool_call_counts.get(tool_use.name, 0) + 1
        )

        if tool_use.name == "read_file":
            path = str(
                tool_use.input.get("file_path") or tool_use.input.get("path") or ""
            )
            offset = tool_use.input.get("offset", 1)
            limit = tool_use.input.get("limit", 2000)
            key = f"{path}|{offset}|{limit}"
            record = self.files_read.setdefault(
                key,
                {"path": path, "offset": offset, "limit": limit, "count": 0},
            )
            record["count"] += 1

        if tool_use.name == "load_skill":
            name = str(tool_use.input.get("name", "")).strip()
            if name:
                _append_unique(self.loaded_skills, name)
            return

        if tool_use.name == "subagent":
            self.subagent_results.append(_shorten(output, 1_500))
            self.subagent_results[:] = self.subagent_results[-5:]
            return

        if tool_use.name in {"write_file", "edit_file"}:
            path = (
                tool_use.input.get("file_path")
                or tool_use.input.get("path")
                or tool_use.input.get("target")
            )
            if path:
                _append_unique(self.files_changed, str(path))
            return

        if tool_use.name == "bash":
            command = str(tool_use.input.get("command", "")).strip()
            if not command:
                return
            self.commands_run.append(command)
            self.commands_run[:] = self.commands_run[-20:]
            if _looks_like_test_command(command):
                self.test_results.append(f"{command}: {_shorten(output, 1_000)}")
                self.test_results[:] = self.test_results[-10:]

    def record_tool_artifact(self, path: Path) -> None:
        _append_unique(self.tool_artifacts, str(path))

    def record_transcript(self, path: Path) -> None:
        self.transcripts.append(str(path))
        self.transcripts[:] = self.transcripts[-10:]

    def to_summary_source(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _looks_like_test_command(command: str) -> bool:
    normalized = command.casefold()
    return any(
        marker in normalized
        for marker in ("pytest", "unittest", " test", "tests", "tox", "nox")
    )


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n... ({len(value) - limit} more chars)"
