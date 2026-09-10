"""Fail-closed tool execution boundary for one code Task Attempt."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.events import redact_payload
from codeagent.teams.models import TaskAttemptState
from codeagent.teams.tasks import validate_task_execution
from codeagent.tools import ToolHandler, ToolRegistry, WorkspaceViolationError
from codeagent.worktrees import WorktreeError, WorktreeManager


_FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_GIT_WRITE = re.compile(
    r"(?:^|[;&|]\s*)git(?:\.exe)?(?:\s+-\S+(?:\s+\S+)?)?\s+"
    r"(?:add|am|apply|branch|checkout|cherry-pick|clean|commit|merge|mv|"
    r"rebase|reset|restore|revert|rm|stash|switch|tag|worktree|push)\b",
    re.IGNORECASE,
)
_SHELL_WRITE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|del|erase|rmdir|rd|mv|move|cp|copy|mkdir|md|"
    r"touch|tee|sed\s+-i|set-content|add-content|clear-content|new-item|"
    r"remove-item|move-item|copy-item|rename-item|npm\s+install|pip\s+install)\b|"
    r"(?:^|[^>])>{1,2}(?:[^>]|$)",
    re.IGNORECASE,
)
_CWD_CHANGE = re.compile(r"(?:^|[;&|]\s*)(?:cd|chdir)\b", re.IGNORECASE)
_PARENT_TRAVERSAL = re.compile(r"(?:^|[\s'\"])(?:\.\.[\\/])")
_SHELL_PATH_TOKEN = r'(?:"[^"\r\n]+"|\'[^\'\r\n]+\'|[^\s\'";|]+)'
_SHELL_STREAM_REDIRECTION = re.compile(
    r"(?<!>)\b(?:\d+|\*)?>\s*&\s*\d+\b",
    re.IGNORECASE,
)


class TeamToolBlocked(RuntimeError):
    """Internal marker used to produce a normal blocked tool result."""


class _PreExecutionBlocked(TeamToolBlocked):
    """A rejected tool call that has not executed and can be corrected safely."""

    def __init__(
        self,
        reason_code: str,
        attempted: str,
        scopes: tuple[str, ...],
        message: str,
        suggestion: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.attempted = attempted
        self.scopes = scopes
        self.suggestion = suggestion


class TeamToolExecutionGate:
    """Validate binding, permissions, scope and repository diff for one Attempt."""

    def __init__(
        self,
        repository: Any,
        worktrees: WorktreeManager,
        attempt_id: str,
        *,
        read_only_mcp_tools: Iterable[str] = (),
        allowed_mcp_tools: Iterable[str] = (),
        trace_id: str | None = None,
        pause_callback: Callable[[], None] | None = None,
    ) -> None:
        self.repository = repository
        self.worktrees = worktrees
        self.attempt_id = attempt_id
        self.read_only_mcp_tools = frozenset(read_only_mcp_tools)
        self.allowed_mcp_tools = frozenset(allowed_mcp_tools)
        self.trace_id = trace_id
        self.pause_callback = pause_callback

    def wrap(self, registry: ToolRegistry) -> ToolRegistry:
        return registry.with_execution_wrapper(self.execute)

    def execute(self, name: str, args: dict[str, Any], handler: ToolHandler) -> str:
        attempt = self.repository.get_task_attempt(self.attempt_id)
        binding = self.repository.get_attempt_worktree_binding(self.attempt_id)
        tool_call_id = f"toolcall_{uuid4().hex}"
        is_write = self._is_write(name, args)
        risk = "high" if name.startswith("mcp__") or _is_git_write(name, args) else (
            "medium" if is_write else "low"
        )
        execution = self.repository.begin_tool_execution(
            self.attempt_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            risk=risk,
            is_write=is_write,
            input=redact_payload(dict(args)),
            worktree_id=binding.id if binding else None,
            trace_id=self.trace_id,
        )
        try:
            if binding is None:
                self._scope_violation(
                    execution.id,
                    tool_call_id,
                    attempted=name,
                    scopes=(),
                    reason="Code Attempt has no Worktree binding",
                )
                return (
                    "Blocked: Worktree binding is missing; Attempt paused and "
                    "requires Runtime recovery"
                )
            try:
                binding = self.worktrees.validate_binding(binding.id)
            except WorktreeError as exc:
                self._scope_violation(
                    execution.id,
                    tool_call_id,
                    attempted=name,
                    scopes=binding.write_scopes,
                    reason=f"Invalid Worktree binding: {exc}",
                )
                return f"Blocked: invalid Worktree binding: {exc}"
            attempt = self.repository.get_task_attempt(self.attempt_id)
            if attempt.state in {
                TaskAttemptState.CANCELLED,
                TaskAttemptState.FAILED,
                TaskAttemptState.ORPHANED,
                TaskAttemptState.SUCCEEDED,
            }:
                raise TeamToolBlocked(f"Attempt is terminal: {attempt.state.value}")
            self._verify_active_leases()
            if name in _FILE_WRITE_TOOLS:
                self._check_file_target(binding.path, binding.write_scopes, args)
            elif name == "bash":
                self._check_shell(binding.path, binding.write_scopes, args)
            elif name.startswith("mcp__"):
                self._check_mcp(name)
            if is_write and not (attempt.write_enabled and binding.write_enabled):
                raise _PreExecutionBlocked(
                    "write_permission_not_enabled",
                    name,
                    binding.write_scopes,
                    "Attempt write permission is not enabled",
                    "Wait for the required approval or use a read-only tool.",
                )

            output = str(handler(**args))
            if name == "bash" or name.startswith("mcp__") or is_write:
                outside = self._outside_scope_changes(
                    binding.id, binding.write_scopes
                )
                if outside:
                    self._scope_violation(
                        execution.id,
                        tool_call_id,
                        attempted=", ".join(outside),
                        scopes=binding.write_scopes,
                        reason="Tool changed files outside the declared write scope",
                    )
                    return (
                        "Blocked: scope violation detected after execution; "
                        "Attempt paused and Worktree frozen. Outside scope: "
                        + ", ".join(outside)
                    )
            status = "failed" if output.startswith("Error:") else "completed"
            self.repository.finish_tool_execution(
                execution.id,
                status=status,
                output_ref=_output_digest(output),
                error=output if status == "failed" else None,
            )
            return output
        except _PreExecutionBlocked as exc:
            output = _pre_execution_block_output(exc)
            self.repository.finish_tool_execution(
                execution.id, status="blocked", error=output
            )
            return output
        except (TeamToolBlocked, WorkspaceViolationError, ValueError) as exc:
            self.repository.finish_tool_execution(
                execution.id, status="blocked", error=str(exc)
            )
            return f"Blocked: {exc}"
        except BaseException as exc:
            if is_write:
                reason = f"{type(exc).__name__}: {exc}"
                self.repository.freeze_attempt_for_unknown_write(
                    self.attempt_id,
                    execution_id=execution.id,
                    tool_call_id=tool_call_id,
                    reason=reason,
                )
                if self.pause_callback is not None:
                    self.pause_callback()
                return json.dumps(
                    {
                        "status": "recovery_required",
                        "executed": True,
                        "reason_code": "unknown_write_result",
                        "message": reason,
                        "result_unknown": True,
                        "retryable": False,
                        "suggestion": (
                            "Do not replay this write. Wait for the user to inspect "
                            "the Worktree and resume the Attempt."
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            self.repository.finish_tool_execution(
                execution.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _verify_active_leases(self) -> None:
        attempt = self.repository.get_task_attempt(self.attempt_id)
        leases = self.repository.list_resource_leases(
            attempt.team_run_id, attempt_id=self.attempt_id
        )
        if not leases or any(item.state != "active" for item in leases):
            raise TeamToolBlocked("Attempt resource lease is not active")

    def _check_file_target(
        self,
        worktree_path: str,
        scopes: tuple[str, ...],
        args: dict[str, Any],
    ) -> None:
        raw = args.get("file_path") or args.get("path")
        if not raw:
            raise _PreExecutionBlocked(
                "write_path_missing",
                "",
                scopes,
                "File write tool did not provide a path",
                "Provide one explicit path inside an allowed write scope.",
            )
        root = Path(worktree_path).resolve()
        candidate = Path(str(raw)).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise _PreExecutionBlocked(
                "path_escapes_worktree",
                str(raw),
                scopes,
                "File path escapes the bound Worktree",
                "Use a path inside the current Teammate Worktree.",
            ) from exc
        if not self._path_allowed(relative, scopes):
            raise _PreExecutionBlocked(
                "path_outside_write_scope",
                relative,
                scopes,
                "File path is outside declared write scope",
                "Write only inside one of the declared allowed scopes.",
            )

    def _check_shell(
        self,
        worktree_path: str,
        scopes: tuple[str, ...],
        args: dict[str, Any],
    ) -> None:
        command = str(args.get("command") or "")
        if _CWD_CHANGE.search(command):
            raise _PreExecutionBlocked(
                "shell_cwd_change_forbidden",
                command,
                scopes,
                "Team shell cwd is fixed to the bound Worktree",
                "Remove cd/chdir and use paths relative to the bound Worktree.",
            )
        if _PARENT_TRAVERSAL.search(command):
            raise _PreExecutionBlocked(
                "path_escapes_worktree",
                command,
                scopes,
                "Parent path traversal is not allowed in Team shell",
                "Use a path inside the current Teammate Worktree.",
            )
        if _GIT_WRITE.search(command):
            raise _PreExecutionBlocked(
                "git_write_forbidden",
                command,
                scopes,
                "Teammates cannot run Git write, commit, merge, or integration commands",
                "Leave Candidate commits and integration operations to Runtime.",
            )
        if _shell_has_explicit_write(command):
            targets = _explicit_shell_write_targets(command)
            if not targets:
                raise _PreExecutionBlocked(
                    "shell_write_path_unresolved",
                    command,
                    scopes,
                    "Shell write path cannot be resolved safely before execution",
                    "Use write_file/edit_file or an explicit literal path.",
                )
            directory_targets = set(_explicit_shell_directory_targets(command))
            for target in targets:
                self._check_shell_target(
                    worktree_path,
                    scopes,
                    target,
                    allow_scope_parent=target in directory_targets,
                )

    def _check_shell_target(
        self,
        worktree_path: str,
        scopes: tuple[str, ...],
        target: str,
        *,
        allow_scope_parent: bool = False,
    ) -> None:
        if not target or any(marker in target for marker in ("$", "*", "?")):
            raise _PreExecutionBlocked(
                "shell_write_path_unresolved",
                target,
                scopes,
                "Dynamic shell write paths are not allowed in Team mode",
                "Use one explicit literal path inside an allowed write scope.",
            )
        root = Path(worktree_path).resolve()
        candidate = Path(target).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise _PreExecutionBlocked(
                "path_escapes_worktree",
                target,
                scopes,
                "Shell write path escapes the bound Worktree",
                "Use a shell path inside the current Teammate Worktree.",
            ) from exc
        if self._path_allowed(relative, scopes):
            return
        if allow_scope_parent and self._path_is_scope_parent(relative, scopes):
            return
        raise _PreExecutionBlocked(
            "path_outside_write_scope",
            relative,
            scopes,
            "Shell write path is outside declared write scope",
            "Write only inside one of the declared allowed scopes.",
        )

    def _check_mcp(self, name: str) -> None:
        if name not in self.read_only_mcp_tools and name not in self.allowed_mcp_tools:
            binding = self.repository.get_attempt_worktree_binding(self.attempt_id)
            raise _PreExecutionBlocked(
                "mcp_tool_not_approved",
                name,
                binding.write_scopes if binding is not None else (),
                "Opaque MCP tool is not approved for this Attempt",
                "Use an approved MCP tool or a built-in scoped file tool.",
            )

    def _outside_scope_changes(
        self, worktree_id: str, scopes: tuple[str, ...]
    ) -> list[str]:
        paths = self.worktrees.changed_paths(worktree_id)
        return [path for path in paths if not self._path_allowed(path, scopes)]

    def _path_allowed(self, path: str, scopes: tuple[str, ...]) -> bool:
        normalized = _path_key(path)
        if not scopes:
            return self._has_repository_lease()
        for scope in scopes:
            allowed = _scope_root(scope)
            if normalized == allowed or normalized.startswith(allowed + "/"):
                return True
        return False

    def _path_is_scope_parent(self, path: str, scopes: tuple[str, ...]) -> bool:
        normalized = _path_key(path)
        return any(_scope_root(scope).startswith(normalized + "/") for scope in scopes)

    def _has_repository_lease(self) -> bool:
        attempt = self.repository.get_task_attempt(self.attempt_id)
        return any(
            item.state == "active" and item.resource_kind == "repository"
            for item in self.repository.list_resource_leases(
                attempt.team_run_id, attempt_id=self.attempt_id
            )
        )

    def _scope_violation(
        self,
        execution_id: str,
        tool_call_id: str,
        *,
        attempted: str,
        scopes: tuple[str, ...],
        reason: str,
    ) -> None:
        self.repository.freeze_attempt_for_scope_violation(
            self.attempt_id,
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            attempted=attempted,
            allowed_scopes=scopes,
            reason=reason,
        )
        if self.pause_callback is not None:
            self.pause_callback()

    def _is_write(self, name: str, args: dict[str, Any]) -> bool:
        if name in _FILE_WRITE_TOOLS:
            return True
        if name.startswith("mcp__"):
            return name not in self.read_only_mcp_tools
        if name != "bash":
            return False
        command = str(args.get("command") or "")
        return _shell_has_explicit_write(command) or not _shell_is_definitely_read_only(
            command
        )


class ReadOnlyTeamToolExecutionGate:
    """Allow only demonstrably read-only repository tools for Lead/analysis roles."""

    def __init__(
        self,
        repository: Any,
        *,
        attempt_id: str | None = None,
        read_only_mcp_tools: Iterable[str] = (),
        trace_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.attempt_id = attempt_id
        self.read_only_mcp_tools = frozenset(read_only_mcp_tools)
        self.trace_id = trace_id

    def wrap(self, registry: ToolRegistry) -> ToolRegistry:
        return registry.with_execution_wrapper(self.execute)

    def execute(self, name: str, args: dict[str, Any], handler: ToolHandler) -> str:
        blocked_reason = self._blocked_reason(name, args)
        execution = None
        if self.attempt_id is not None:
            execution = self.repository.begin_tool_execution(
                self.attempt_id,
                tool_call_id=f"toolcall_{uuid4().hex}",
                tool_name=name,
                risk="low" if blocked_reason is None else "medium",
                is_write=blocked_reason is not None,
                input=redact_payload(dict(args)),
                worktree_id=None,
                trace_id=self.trace_id,
            )
        if blocked_reason is not None:
            if execution is not None:
                self.repository.finish_tool_execution(
                    execution.id, status="blocked", error=blocked_reason
                )
            return f"Blocked: {blocked_reason}"
        try:
            output = str(handler(**args))
        except BaseException as exc:
            if execution is not None:
                self.repository.finish_tool_execution(
                    execution.id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if execution is not None:
            self.repository.finish_tool_execution(
                execution.id,
                status="failed" if output.startswith("Error:") else "completed",
                output_ref=_output_digest(output),
                error=output if output.startswith("Error:") else None,
            )
        return output

    def _blocked_reason(self, name: str, args: dict[str, Any]) -> str | None:
        if name in _FILE_WRITE_TOOLS:
            return "This Team role has read-only repository access"
        if name == "bash" and not _shell_is_definitely_read_only(
            str(args.get("command") or "")
        ):
            return "Team read-only shell accepts only an explicit read-only command"
        if name.startswith("mcp__") and name not in self.read_only_mcp_tools:
            return "MCP tool is not declared read-only for this Team role"
        return None


class ActiveTeamRootToolExecutionGate:
    """Freeze an already-running Root Agent as soon as it creates a TeamRun."""

    _TEAM_CONTROL_WRITES = frozenset(
        {"TaskCreate", "TaskUpdate", "TeamPlanSubmit", "subagent"}
    )

    def __init__(self, repository: Any, conversation_id: str) -> None:
        self.repository = repository
        self.conversation_id = conversation_id

    def wrap(self, registry: ToolRegistry) -> ToolRegistry:
        return registry.with_execution_wrapper(self.execute)

    def execute(self, name: str, args: dict[str, Any], handler: ToolHandler) -> str:
        team = self.repository.get_active_team_run_for_conversation(
            self.conversation_id
        )
        if team is None:
            return str(handler(**args))
        if name in self._TEAM_CONTROL_WRITES:
            return (
                "Blocked: Root repository and Task mutation is disabled while "
                "an Agent Team is active"
            )
        read_only_gate = ReadOnlyTeamToolExecutionGate(
            self.repository,
            read_only_mcp_tools=team.metadata.get("read_only_mcp_tools", ()),
        )
        reason = read_only_gate._blocked_reason(name, args)
        if reason is not None:
            return f"Blocked: Root/Lead is read-only while an Agent Team is active: {reason}"
        return str(handler(**args))


class TeamPlannerToolExecutionGate:
    """Keep Team planning read-only and freeze Task edits after plan submission."""

    def __init__(
        self,
        repository: Any,
        conversation_id: str,
        task_list_id: str,
    ) -> None:
        self.repository = repository
        self.conversation_id = conversation_id
        self.task_list_id = task_list_id

    def wrap(self, registry: ToolRegistry) -> ToolRegistry:
        return registry.with_execution_wrapper(self.execute)

    def execute(self, name: str, args: dict[str, Any], handler: ToolHandler) -> str:
        team = self.repository.get_active_team_run_for_conversation(
            self.conversation_id
        )
        if team is not None and getattr(team.state, "value", team.state) != "planning":
            return ActiveTeamRootToolExecutionGate(
                self.repository, self.conversation_id
            ).execute(name, args, handler)

        if name == "TaskUpdate":
            reason = self._task_update_blocked_reason(args)
            if reason is not None:
                return f"Blocked: {reason}"

        if name in {"TaskCreate", "TaskUpdate"}:
            metadata = {}
            if name == "TaskUpdate":
                task = self.repository.get_task_resource(
                    self.task_list_id, str(args["taskId"])
                )
                metadata = dict(task.task.metadata)
            incoming = args.get("metadata", {})
            if not isinstance(incoming, Mapping):
                raise ValueError("Team Task metadata must be an object")
            for key, value in incoming.items():
                if name == "TaskUpdate" and value is None:
                    # Match TaskUpdate's metadata deletion semantics.
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            validate_task_execution(metadata)

        reason = ReadOnlyTeamToolExecutionGate(self.repository)._blocked_reason(
            name, args
        )
        if reason is not None:
            return f"Blocked: Team planning is read-only: {reason}"
        return str(handler(**args))

    def _task_update_blocked_reason(self, args: dict[str, Any]) -> str | None:
        if "status" in args or "owner" in args:
            return "Team Planner cannot claim, complete, or assign a Task"
        task_id = str(args.get("taskId") or "").strip()
        if not task_id:
            return "TaskUpdate requires taskId"
        task = self.repository.get_task_resource(self.task_list_id, task_id).task
        if getattr(task.status, "value", task.status) != "pending":
            return "Team Planner can update only pending Tasks"
        return None


def _is_git_write(name: str, args: dict[str, Any]) -> bool:
    return name == "bash" and bool(_GIT_WRITE.search(str(args.get("command") or "")))


def _output_digest(output: str) -> str:
    return "sha256:" + hashlib.sha256(output.encode("utf-8")).hexdigest()


def _pre_execution_block_output(error: _PreExecutionBlocked) -> str:
    return json.dumps(
        {
            "status": "blocked",
            "executed": False,
            "reason_code": error.reason_code,
            "message": str(error),
            "attempted": error.attempted,
            "allowed_scopes": list(error.scopes),
            "retryable": True,
            "suggestion": error.suggestion,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _without_stream_redirections(command: str) -> str:
    return _SHELL_STREAM_REDIRECTION.sub("", command)


def _shell_has_explicit_write(command: str) -> bool:
    return bool(_SHELL_WRITE.search(_without_stream_redirections(command)))


def _shell_is_definitely_read_only(command: str) -> bool:
    if not command.strip() or _CWD_CHANGE.search(command) or _GIT_WRITE.search(command):
        return False
    segments = [item.strip() for item in re.split(r"(?:&&|\|\||;|\|)", command)]
    allowed = re.compile(
        r"^(?:git(?:\.exe)?\s+(?:status|diff|log|show|rev-parse|branch\s+--show-current)\b|"
        r"rg\b|grep\b|findstr\b|select-string\b|get-content\b|get-childitem\b|"
        r"dir\b|ls\b|pwd\b|test-path\b)",
        re.IGNORECASE,
    )
    return bool(segments) and all(allowed.search(item) for item in segments)


def _explicit_shell_write_targets(command: str) -> tuple[str, ...]:
    command = _without_stream_redirections(command)
    targets: list[str] = []
    patterns = (
        rf"(?:set-content|add-content|clear-content|new-item|remove-item)\b"
        rf"[^;|]*?-(?:literalpath|path)\s+(?P<target>{_SHELL_PATH_TOKEN})",
        rf"(?:set-content|add-content|clear-content|new-item|remove-item)\s+"
        rf"(?P<target>(?!-){_SHELL_PATH_TOKEN})",
        rf">{{1,2}}\s*(?P<target>{_SHELL_PATH_TOKEN})",
        r"(?:^|[;&|]\s*)(?:touch|mkdir|rmdir|rm|del|erase)\s+"
        rf"(?:-[^\s]+\s+)*(?P<target>{_SHELL_PATH_TOKEN})",
    )
    for pattern in patterns:
        targets.extend(
            _strip_shell_quotes(match.group("target"))
            for match in re.finditer(pattern, command, re.IGNORECASE)
        )
    return tuple(dict.fromkeys(targets))


def _explicit_shell_directory_targets(command: str) -> tuple[str, ...]:
    targets = [
        _strip_shell_quotes(match.group("target"))
        for match in re.finditer(
            rf"new-item\b(?=[^;|]*-(?:itemtype|type)\s+"
            rf"(?:directory|container)\b)[^;|]*?-(?:literalpath|path)\s+"
            rf"(?P<target>{_SHELL_PATH_TOKEN})",
            command,
            re.IGNORECASE,
        )
    ]
    targets.extend(
        _strip_shell_quotes(match.group("target"))
        for match in re.finditer(
            rf"(?:^|[;&|]\s*)(?:mkdir|md)\s+(?:-[^\s]+\s+)*"
            rf"(?P<target>{_SHELL_PATH_TOKEN})",
            command,
            re.IGNORECASE,
        )
    )
    return tuple(dict.fromkeys(targets))


def _strip_shell_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _path_key(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def _scope_root(scope: str) -> str:
    normalized = _path_key(scope)
    if normalized.endswith("/**"):
        return normalized[:-3].rstrip("/")
    return normalized


__all__ = [
    "ActiveTeamRootToolExecutionGate",
    "ReadOnlyTeamToolExecutionGate",
    "TeamPlannerToolExecutionGate",
    "TeamToolExecutionGate",
]
