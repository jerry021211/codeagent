"""Lead-side entry point for first-phase Agent Team planning.

The tool in this module does not execute teammates.  It only turns an existing
Task DAG into an immutable Team Plan revision that is waiting for user
approval.  Scheduling remains a Runtime responsibility.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from codeagent.tasks import TaskStatus
from codeagent.teams.models import TeamPlanStatus, TeamRunState
from codeagent.teams.tasks import validate_task_execution
from codeagent.tools.base import ToolDefinition
from codeagent.worktrees import DirtyWorkspaceConfirmationRequired


_TERMINAL_TEAM_STATES = {
    TeamRunState.COMPLETED,
    TeamRunState.CLOSED_WITH_UNMERGED_CANDIDATES,
    TeamRunState.FAILED,
    TeamRunState.CANCELLED,
}


@dataclass(slots=True)
class LeadTeamPlanTool:
    """Submit or revise one Team Plan without starting executable work."""

    repository: Any
    worktrees: Any
    conversation_id: str
    root_run_id: str
    task_list_id: str
    memory_access: Any | None = None
    definition: ToolDefinition = field(
        default=ToolDefinition(
            name="TeamPlanSubmit",
            description=(
                "Submit one immutable Team Plan revision after creating its Task DAG. "
                "This tool is available only in explicit Team planning mode. It submits "
                "the revision for user approval; it never starts Attempts, creates code "
                "Worktrees, enables writes, or integrates code. Each plan task must "
                "reference an existing pending Task whose metadata already declares "
                "kind, write_scopes, risk_level, plan_required, and validation_commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "baseCommit": {
                        "type": "string",
                        "description": "One explicit Git commit shared by every Team task.",
                    },
                    "plan": {
                        "type": "object",
                        "description": (
                            "The proposed Team Plan. tasks must be a non-empty array of "
                            "objects with task_id, kind, write_scopes, and risk_level."
                        ),
                        "properties": {
                            "shared_context": {
                                "type": "string",
                                "maxLength": 8000,
                                "description": "Optional shared interface decisions, not other Tasks' full descriptions.",
                            },
                            "tasks": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task_id": {"type": "string"},
                                        "kind": {
                                            "type": "string",
                                            "enum": ["analysis", "code"],
                                        },
                                        "write_scopes": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "risk_level": {
                                            "type": "string",
                                            "enum": ["low", "medium", "high"],
                                        },
                                    },
                                    "required": [
                                        "task_id",
                                        "kind",
                                        "write_scopes",
                                        "risk_level",
                                    ],
                                },
                            }
                        },
                        "required": ["tasks"],
                    },
                    "teammateCount": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "maxTeammates": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "tokenBudget": {"type": ["integer", "null"], "minimum": 1},
                    "modelCallBudget": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                    },
                    "deadlineAt": {"type": ["string", "null"]},
                },
                "required": ["baseCommit", "plan", "teammateCount"],
            },
        ),
        init=False,
    )

    def run(
        self,
        baseCommit: str,
        plan: Mapping[str, Any],
        teammateCount: int,
        maxTeammates: int = 3,
        tokenBudget: int | None = None,
        modelCallBudget: int | None = None,
        deadlineAt: str | None = None,
    ) -> str:
        teammate_count = int(teammateCount)
        max_teammates = int(maxTeammates)
        if not 1 <= teammate_count <= 8:
            raise ValueError("teammateCount must be between 1 and 8")
        if not 1 <= max_teammates <= 8:
            raise ValueError("maxTeammates must be between 1 and 8")
        if teammate_count > max_teammates:
            raise ValueError("teammateCount cannot exceed maxTeammates")

        conversation = self.repository.get_conversation(self.conversation_id)
        manager = self.worktrees.for_workspace(conversation.workspace)
        inspection = manager.inspect_baseline(baseCommit)
        if inspection.source_dirty:
            raise DirtyWorkspaceConfirmationRequired(
                "The source workspace is dirty. TeamPlanSubmit cannot confirm a dirty "
                "baseline on the user's behalf. Clean it or use the explicit user "
                "confirmation flow before approving a Team Plan."
            )

        plan_payload = self._validated_plan(plan, inspection.base_commit)
        plan_payload["teammate_count"] = teammate_count
        creation_guard = (
            self.memory_access.creating_team(conversation.workspace)
            if self.memory_access is not None
            else nullcontext()
        )
        with creation_guard:
            team, created = self._resolve_team(
                base_commit=inspection.base_commit,
                max_teammates=max_teammates,
                token_budget=tokenBudget,
                model_call_budget=modelCallBudget,
                deadline_at=deadlineAt,
            )
        revisions = self.repository.list_team_plan_revisions(team.id)
        latest = revisions[-1] if revisions else None
        if team.state is TeamRunState.WAITING_APPROVAL:
            if latest is None or latest.plan != plan_payload:
                raise ValueError(
                    "This TeamRun already has a different pending Team Plan. The user "
                    "must approve or reject it before another revision is submitted."
                )
            submitted = latest
        elif team.state is TeamRunState.PLANNING:
            if latest is not None and latest.status is TeamPlanStatus.DRAFT:
                if latest.plan != plan_payload:
                    raise ValueError(
                        "The TeamRun has a different unfinished DRAFT revision"
                    )
                submitted = latest
            else:
                submitted = self.repository.create_team_plan_revision(
                    team.id,
                    plan=plan_payload,
                    created_by=team.lead_agent_id,
                    command_id=f"lead-create-plan:{team.id}:{len(revisions) + 1}",
                )
            submitted = self.repository.submit_team_plan_revision(
                team.id,
                submitted.revision,
                command_id=f"lead-submit-plan:{team.id}:{submitted.revision}",
            )
        else:
            raise ValueError(
                f"The current conversation already has an active TeamRun in "
                f"{team.state.value}; its plan cannot be replaced."
            )

        manager.confirm_baseline(
            team.id,
            confirmed_by="runtime",
            allow_dirty=False,
            command_id=f"lead-confirm-clean-base:{team.id}:{inspection.status_hash}",
        )
        current = self.repository.get_team_run(team.id)
        return json.dumps(
            {
                "team_run_id": team.id,
                "created": created,
                "state": current.state.value,
                "plan_revision": submitted.revision,
                "plan_status": submitted.status.value,
                "base_commit": inspection.base_commit,
                "task_ids": [item["task_id"] for item in plan_payload["tasks"]],
                "teammate_count": teammate_count,
                "next_action": "Wait for the user to approve or reject this Team Plan.",
                "execution_started": False,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _resolve_team(
        self,
        *,
        base_commit: str,
        max_teammates: int,
        token_budget: int | None,
        model_call_budget: int | None,
        deadline_at: str | None,
    ) -> tuple[Any, bool]:
        active = [
            item
            for item in self.repository.list_team_runs()
            if item.conversation_id == self.conversation_id
            and item.state not in _TERMINAL_TEAM_STATES
        ]
        if len(active) > 1:
            raise ValueError("The conversation has multiple active TeamRuns")
        if active:
            team = active[0]
            if team.task_list_id != self.task_list_id:
                raise ValueError("The active TeamRun is bound to a different Task list")
            if team.base_commit != base_commit:
                raise ValueError(
                    "Changing base_commit requires a new user-approved Team design; "
                    "the existing TeamRun cannot be revised in place."
                )
            return team, False
        return (
            self.repository.create_team_run(
                conversation_id=self.conversation_id,
                root_run_id=self.root_run_id,
                task_list_id=self.task_list_id,
                base_commit=base_commit,
                max_teammates=max_teammates,
                token_budget=token_budget,
                model_call_budget=model_call_budget,
                deadline_at=deadline_at,
                metadata={
                    "manual_integration_only": True,
                    "source_dirty_at_creation": False,
                    "created_via": "lead_tool",
                },
            ),
            True,
        )

    def _validated_plan(
        self, plan: Mapping[str, Any], base_commit: str
    ) -> dict[str, Any]:
        if not isinstance(plan, Mapping) or not plan:
            raise ValueError("Team Plan cannot be empty")
        shared_context = plan.get("shared_context", "")
        if not isinstance(shared_context, str) or len(shared_context) > 8000:
            raise ValueError("Team shared_context must be text of at most 8000 characters")
        raw_tasks = plan.get("tasks")
        if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
            raise ValueError("Team Plan tasks must be a non-empty array")
        if not raw_tasks:
            raise ValueError("Team Plan must include at least one Task")

        normalized_tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_tasks:
            if not isinstance(raw, Mapping):
                raise ValueError("Every Team Plan task must be an object")
            task_id = str(raw.get("task_id") or "").strip()
            if not task_id or task_id in seen:
                raise ValueError("Team Plan task_ids must be non-empty and unique")
            seen.add(task_id)
            resource = self.repository.get_task_resource(self.task_list_id, task_id)
            if resource.task.status is not TaskStatus.PENDING:
                raise ValueError(f"Team Task {task_id} must still be pending")
            metadata = resource.task.metadata
            validate_task_execution(metadata)
            validate_task_execution({**metadata, **raw})
            if "plan_required" in raw and raw["plan_required"] != metadata.get("plan_required", False):
                raise ValueError(
                    f"Team Task {task_id} plan_required differs from its Task metadata"
                )
            required_metadata = {
                "kind",
                "write_scopes",
                "risk_level",
                "plan_required",
                "validation_commands",
            }
            if str(raw.get("kind") or metadata.get("kind") or "analysis").lower() == "code":
                missing = sorted(required_metadata - set(metadata))
                if missing:
                    raise ValueError(
                        f"Code Task {task_id} metadata is missing: "
                        + ", ".join(missing)
                    )
            kind = str(raw.get("kind") or metadata.get("kind") or "analysis").lower()
            if kind not in {"analysis", "code"}:
                raise ValueError(f"Unsupported Team Task kind: {kind}")
            metadata_kind = str(metadata.get("kind") or "analysis").lower()
            scopes = _normalize_scopes(raw.get("write_scopes", []))
            metadata_scopes = _normalize_scopes(metadata.get("write_scopes", []))
            risk = str(raw.get("risk_level") or "low").strip().lower()
            metadata_risk = str(metadata.get("risk_level") or "low").strip().lower()
            if risk not in {"low", "medium", "high"}:
                raise ValueError(f"Unsupported risk_level for Task {task_id}: {risk}")
            if kind != metadata_kind or scopes != metadata_scopes or risk != metadata_risk:
                raise ValueError(
                    f"Team Plan scope for Task {task_id} must exactly match its Task "
                    "metadata (kind, write_scopes, risk_level)."
                )
            if kind == "code":
                commands = metadata.get("validation_commands")
                if (
                    not isinstance(commands, Sequence)
                    or isinstance(commands, (str, bytes))
                    or not commands
                    or any(not str(command).strip() for command in commands)
                ):
                    raise ValueError(
                        f"Code Task {task_id} metadata must declare at least one "
                        "validation command"
                    )
            normalized = dict(raw)
            normalized.update(
                {
                    "task_id": task_id,
                    "kind": kind,
                    "write_scopes": scopes,
                    "risk_level": risk,
                    "plan_required": metadata.get("plan_required", False),
                }
            )
            normalized_tasks.append(normalized)

        payload = dict(plan)
        payload.update(
            {
                "tasks": normalized_tasks,
                "base_commit": base_commit,
                "integration_mode": "manual",
            }
        )
        return payload

def _normalize_scopes(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("write_scopes must be an array")
    result: list[str] = []
    for raw in value:
        scope = str(raw).replace("\\", "/").strip().strip("/")
        if not scope or any(part in {"", ".", ".."} for part in scope.split("/")):
            raise ValueError(f"Invalid write scope: {raw}")
        if scope not in result:
            result.append(scope)
    return result


__all__ = ["LeadTeamPlanTool"]
