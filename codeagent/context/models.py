"""Context compaction configuration and runtime state."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codeagent.messages import ToolUse
from codeagent.tools.base import normalize_tool_output


@dataclass(slots=True)
class ContextConfig:
    mode: str = "model"
    summarization_model: str = ""
    summarization_api_key: str | None = None
    tool_result_budget_chars: int = 200_000
    single_tool_output_max_chars: int = 80_000
    compact_threshold_chars: int = 300_000
    summary_max_chars: int = 4_000
    transcript_dir: Path = Path(".transcripts")
    tool_output_dir: Path = Path(".task_outputs/tool-results")
    reactive_retries: int = 1
    persisted_preview_chars: int = 2_000
    recency_messages: int = 12
    recency_rounds: int = 2
    min_fold_messages: int = 4
    # Accepted for older SDK/env configurations; counts no longer trigger summaries.
    message_trigger_min_fold: int = 16
    round_trigger_min_fold: int = 8
    max_fold_messages: int = 200
    max_fold_rounds: int = 12
    summary_input_max_chars: int = 120_000
    max_request_chars: int = 600_000
    context_window_tokens: int = 0
    summary_context_window_tokens: int = 0
    failure_cooldown_seconds: float = 90.0
    tool_projection_enabled: bool = True
    investigation_keep_rounds: int = 2
    command_keep_rounds: int = 1
    write_keep_rounds: int = 2
    tool_clear_min_chars: int = 8_000
    write_clear_min_chars: int = 8_000
    summary_timeout_seconds: float = 45.0
    summary_text_preview_chars: int = 4_000
    summary_argument_preview_chars: int = 2_000
    model_context_windows: dict[str, int] = field(default_factory=dict)
    near_context_ratio: float = 0.8

    def __post_init__(self) -> None:
        if self.mode not in {"model", "off"}:
            raise ValueError("CONTEXT_COMPACT_MODE must be 'model' or 'off'")
        for name in (
            "tool_result_budget_chars", "single_tool_output_max_chars", "compact_threshold_chars",
            "summary_max_chars", "recency_messages", "recency_rounds", "min_fold_messages",
            "message_trigger_min_fold", "round_trigger_min_fold", "max_fold_messages", "max_fold_rounds",
            "summary_input_max_chars", "max_request_chars", "tool_clear_min_chars", "write_clear_min_chars",
            "summary_text_preview_chars", "summary_argument_preview_chars",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("reactive_retries", "persisted_preview_chars", "context_window_tokens",
                     "summary_context_window_tokens", "investigation_keep_rounds",
                     "command_keep_rounds", "write_keep_rounds"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("failure_cooldown_seconds", "summary_timeout_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                    or value < 0 or (name == "summary_timeout_seconds" and value == 0)):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'summary_timeout_seconds' else 'non-negative'}")
        if self.max_fold_messages < self.min_fold_messages:
            raise ValueError("max_fold_messages must be >= min_fold_messages")
        if not isinstance(self.model_context_windows, dict) or any(
            not isinstance(model, str) or not model.strip() or type(window) is not int or window <= 0
            for model, window in self.model_context_windows.items()
        ):
            raise ValueError("model_context_windows must map model names to positive token windows")
        if (isinstance(self.near_context_ratio, bool) or not isinstance(self.near_context_ratio, (float, int))
                or not 0 < self.near_context_ratio <= 1):
            raise ValueError("near_context_ratio must be in (0, 1]")

    def window_for_model(self, model: str) -> int:
        return self.model_context_windows.get(model, self.context_window_tokens)


@dataclass(slots=True)
class RuntimeState:
    user_goal: str = ""
    history_generation: int = 0
    tool_schema_hash: str = ""
    last_prompt_mode: str | None = None
    # These fields are committed with canonical messages in the existing checkpoint.
    summary_text: str = ""
    compacted_message_count: int = 0
    compacted_prefix_hash: str = ""
    summary_revision: int = 0
    summary_transcript: str = ""
    summary_source_count: int = 0
    summary_source_hash: str = ""
    summary_retry_after_epoch: float = 0.0
    summary_failure_scope: str = ""
    current_turn_start: int = -1
    latest_request_prompt_tokens: int = 0
    peak_request_prompt_tokens: int = 0
    accumulated_input_tokens: int = 0
    accumulated_output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    latest_request_model: str = ""
    latest_request_estimated: bool = True
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

        result = normalize_tool_output(output)
        if result.status != "success" and tool_use.name != "bash":
            self.important_notes.append(f"{tool_use.name} [{result.status}]: {_shorten(output, 500)}")
            self.important_notes[:] = self.important_notes[-10:]
            return

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
                self.test_results.append(f"{command} [status={result.status}, exit_code={result.exit_code}]: {_shorten(output, 1_000)}")
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
