"""Context compaction configuration and runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codeagent.messages import ToolUse


@dataclass(slots=True)
class ContextConfig:
    mode: str = "simple"
    max_messages: int = 50
    keep_head_messages: int = 3
    keep_tail_messages: int = 47
    keep_recent_tool_results: int = 3
    tool_result_budget_chars: int = 200_000
    single_tool_output_max_chars: int = 80_000
    compact_threshold_chars: int = 300_000
    summary_max_chars: int = 12_000
    transcript_dir: Path = Path(".transcripts")
    tool_output_dir: Path = Path(".task_outputs/tool-results")
    reactive_retries: int = 1
    max_compact_failures: int = 3
    keep_reactive_tail_messages: int = 5
    persisted_preview_chars: int = 2_000


@dataclass(slots=True)
class RuntimeState:
    user_goal: str = ""
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
        if tool_use.name == "load_skill":
            name = str(tool_use.input.get("name", "")).strip()
            if name:
                _append_unique(self.loaded_skills, name)
            return

        if tool_use.name == "task":
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

    def record_transcript(self, path: Path) -> None:
        self.transcripts.append(str(path))
        self.transcripts[:] = self.transcripts[-10:]

    def render_summary(self, todo_text: str | None = None) -> str:
        sections = [
            "<context_summary>",
            "User goal:",
            self.user_goal or "(unknown)",
            "",
            "Current todo:",
            todo_text or "(not available)",
            "",
            "Loaded skills:",
            _format_list(self.loaded_skills),
            "",
            "Files changed:",
            _format_list(self.files_changed),
            "",
            "Commands run:",
            _format_list(self.commands_run[-10:]),
            "",
            "Test results:",
            _format_list(self.test_results),
            "",
            "Subagent results:",
            _format_list(self.subagent_results),
            "",
            "Important notes:",
            _format_list(self.important_notes),
            "",
            "Saved transcripts:",
            _format_list(self.transcripts),
            "</context_summary>",
        ]
        return "\n".join(sections)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _format_list(values: list[str]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {value}" for value in values)


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
