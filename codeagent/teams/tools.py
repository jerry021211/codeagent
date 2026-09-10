"""Small Runtime-backed collaboration tools for Team roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.teams.bus import MessageBus
from codeagent.teams.candidates import CandidateService
from codeagent.teams.models import TaskAttemptRecord
from codeagent.tools.base import ToolDefinition
from codeagent.worktrees import WorktreeManager


@dataclass(slots=True)
class SubmitAttemptPlanTool:
    repository: Any
    worktrees: WorktreeManager
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_attempt_plan",
        description=(
            "Submit a new immutable execution-plan revision when the assigned "
            "Attempt requires approval, then pause for Lead review."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "planned_files": {"type": "array", "items": {"type": "string"}},
                "planned_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "planned_tests": {"type": "array", "items": {"type": "string"}},
                "write_scopes": {"type": "array", "items": {"type": "string"}},
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
            },
            "required": [
                "summary",
                "planned_files",
                "planned_commands",
                "planned_tests",
                "write_scopes",
                "risk_level",
            ],
        },
    )

    def run(
        self,
        summary: str,
        planned_files: list[str],
        planned_commands: list[str],
        planned_tests: list[str],
        write_scopes: list[str],
        risk_level: str,
    ) -> str:
        binding = self.repository.get_attempt_worktree_binding(self.attempt.id)
        if binding is None:
            return "Error: Attempt has no Worktree binding"
        self.worktrees.validate_binding(binding.id)
        suffix = uuid4().hex
        plan = self.repository.create_attempt_plan(
            self.attempt.id,
            summary=summary,
            planned_files=planned_files,
            planned_commands=planned_commands,
            planned_tests=planned_tests,
            write_scopes=write_scopes,
            risk_level=risk_level,
            created_by=self.attempt.agent_id,
            command_id=f"teammate-create-plan:{self.attempt.id}:{suffix}",
        )
        submitted = self.repository.submit_attempt_plan(
            self.attempt.id,
            plan.revision,
            command_id=f"teammate-submit-plan:{self.attempt.id}:{plan.revision}",
        )
        self.yield_callback("attempt_plan_submitted")
        return (
            f"Submitted Attempt Plan revision p{submitted.revision}; "
            "write tools remain disabled pending approval."
        )


@dataclass(slots=True)
class SubmitCandidateTool:
    service: CandidateService
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_candidate",
        description=(
            "Freeze and submit the current Worktree diff as a Candidate for Lead "
            "review. This revokes Teammate write access."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "tests_reported": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "known_risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "tests_reported", "known_risks"],
        },
    )

    def run(
        self,
        summary: str,
        tests_reported: list[str],
        known_risks: list[str],
    ) -> str:
        candidate = self.service.submit(
            self.attempt.id,
            summary=summary,
            tests_reported=tests_reported,
            known_risks=known_risks,
        )
        self.yield_callback("candidate_submitted")
        return f"Submitted frozen Candidate {candidate.id} for Lead review."


@dataclass(slots=True)
class SubmitAnalysisResultTool:
    repository: Any
    attempt: TaskAttemptRecord
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_analysis_result",
        description=(
            "Submit the final result of a read-only analysis Task. Analysis Tasks "
            "do not create a Worktree or candidate commit."
        ),
        input_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )

    def run(self, summary: str) -> str:
        completed = self.repository.complete_analysis_attempt(
            self.attempt.id,
            summary=summary,
            submitted_by=self.attempt.agent_id,
            command_id=f"analysis-result:{self.attempt.id}",
        )
        return f"Analysis result submitted; Attempt {completed.id} succeeded."


@dataclass(slots=True)
class TeamProgressTool:
    repository: Any
    attempt: TaskAttemptRecord
    definition: ToolDefinition = ToolDefinition(
        name="team_report_progress",
        description="Report concise progress to the Team Lead through durable messaging.",
        input_schema={
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["stage", "summary"],
        },
    )

    def run(self, stage: str, summary: str) -> str:
        team = self.repository.get_team_run(self.attempt.team_run_id)
        assert team is not None
        lead_generation = _active_lead_generation(self.repository, team)
        message = MessageBus(self.repository).send(
            team.id,
            sender_type="teammate",
            sender_agent_id=self.attempt.agent_id,
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=lead_generation,
            message_type="PROGRESS",
            payload={"stage": stage, "summary": summary},
            dedupe_key=f"progress:{self.attempt.id}:{uuid4().hex}",
            task_id=self.attempt.task_id,
            attempt_id=self.attempt.id,
        )
        return f"Progress message persisted: {message.id}"


@dataclass(slots=True)
class TeamQuestionTool:
    repository: Any
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_ask_lead",
        description="Ask the Team Lead a scoped question through durable messaging.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "blocking": {"type": "boolean"},
            },
            "required": ["question", "blocking"],
        },
    )

    def run(self, question: str, blocking: bool) -> str:
        team = self.repository.get_team_run(self.attempt.team_run_id)
        assert team is not None
        session = self.repository.get_agent_session(self.attempt.session_id)
        lead_generation = _active_lead_generation(self.repository, team)
        message = MessageBus(self.repository).send(
            team.id,
            sender_type="teammate",
            sender_agent_id=self.attempt.agent_id,
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=lead_generation,
            message_type="QUESTION",
            payload={
                "question": question, "blocking": bool(blocking),
                "session_id": session.id, "generation": session.generation,
            },
            dedupe_key=f"question:{self.attempt.id}:{uuid4().hex}",
            task_id=self.attempt.task_id,
            attempt_id=self.attempt.id,
        )
        if blocking:
            self.yield_callback(f"waiting_for_lead_answer:{message.id}")
        return f"Question persisted: {message.id}"


@dataclass(slots=True)
class TeamStatusTool:
    repository: Any
    team_run_id: str
    allow_code: bool
    definition: ToolDefinition = ToolDefinition(
        name="team_get_status",
        description="Read the durable state of the active TeamRun and its pending decisions.",
        input_schema={"type": "object", "properties": {}},
    )

    def run(self) -> str:
        team = self.repository.get_team_run(self.team_run_id)
        if team is None:
            return "Error: TeamRun no longer exists"
        attempts = self.repository.list_task_attempts(team.id)
        payload = {
            "team": team.to_dict(),
            "plans": [
                item.to_dict()
                for item in self.repository.list_team_plan_revisions(team.id)
            ],
            "tasks": [
                item.to_dict(camel_case=True)
                for item in self.repository.list_task_resources(team.task_list_id)
            ],
            "scheduling": [
                item.to_dict()
                for item in self.repository.list_task_scheduling(
                    team.id, allow_code=self.allow_code
                )
            ],
            "sessions": [
                item.to_dict() for item in self.repository.list_agent_sessions(team.id)
            ],
            "attempts": [item.to_dict() for item in attempts],
            "attempt_plans": [
                item.to_dict()
                for attempt in attempts
                for item in self.repository.list_attempt_plans(attempt.id)
            ],
            "candidates": [
                item.to_dict() for item in self.repository.list_candidates(team.id)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamCandidateContextTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_get_candidate_context",
        description="Read one frozen Candidate's metadata and patch for semantic review.",
        input_schema={
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        },
    )

    def run(self, candidate_id: str) -> str:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.team_run_id != self.team_run_id:
            return "Error: Candidate does not belong to this TeamRun"
        binding = self.repository.get_attempt_worktree_binding(candidate.attempt_id)
        if binding is None:
            return "Error: Candidate Worktree binding is missing"
        binding = self.worktrees.validate_binding(binding.id)
        if not self.worktrees.candidate_snapshot_matches(
            binding.id,
            expected_hash=candidate.diff_hash,
            expected_head=candidate.worktree_head,
        ):
            return "Error: Candidate changed after submission"
        artifact = Path(candidate.diff_ref).resolve()
        manager = (
            self.worktrees.for_team(self.team_run_id)
            if hasattr(self.worktrees, "for_team")
            else self.worktrees
        )
        managed_root = Path(manager.managed_root).resolve()
        try:
            artifact.relative_to(managed_root)
        except ValueError:
            return "Error: Candidate patch is outside Runtime-managed data"
        if artifact.name != f"{candidate.id}.patch" or not artifact.is_file():
            return "Error: Candidate patch reference is invalid"
        payload = candidate.to_dict()
        payload["patch"] = artifact.read_text(encoding="utf-8")
        return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamAnswerQuestionTool:
    repository: Any
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_answer_question",
        description=(
            "Answer one Teammate QUESTION. Set scope_changed=true only to report that "
            "the approved Team Plan must be replaced; the Attempt will remain paused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "answer": {"type": "string"},
                "scope_changed": {"type": "boolean"},
            },
            "required": ["message_id", "answer", "scope_changed"],
        },
    )

    def run(self, message_id: str, answer: str, scope_changed: bool) -> str:
        message = self.repository.answer_team_question(
            self.team_run_id,
            question_id=message_id, lead_agent_id=self.lead_agent_id,
            answer=answer, scope_changed=scope_changed,
        )
        if scope_changed:
            return (
                f"Answer persisted: {message.id}. Attempt remains paused because a "
                "new user-approved Team Plan is required."
            )
        return f"Answer persisted: {message.id}. Runtime resumes only the matching question wait at a safe boundary."


@dataclass(slots=True)
class TeamAttemptPlanDecisionTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_decide_attempt_plan",
        description="Approve or reject one submitted Attempt Plan as the Team Lead.",
        input_schema={
            "type": "object",
            "properties": {
                "attempt_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string"},
            },
            "required": ["attempt_id", "revision", "decision", "reason"],
        },
    )

    def run(self, attempt_id: str, revision: int, decision: str, reason: str) -> str:
        attempt = self.repository.get_task_attempt(attempt_id)
        if attempt.team_run_id != self.team_run_id:
            return "Error: Attempt does not belong to this TeamRun"
        fingerprint = None
        if decision == "approve":
            binding = self.repository.get_attempt_worktree_binding(attempt.id)
            if binding is None:
                return "Error: Code Attempt has no Worktree binding"
            fingerprint = self.worktrees.validate_binding(binding.id).fingerprint
        plan = self.repository.decide_attempt_plan(
            attempt.id,
            int(revision),
            decision=decision,
            decided_by=self.lead_agent_id,
            reason=reason,
            command_id=f"lead-attempt-plan:{attempt.id}:{revision}:{decision}",
            validated_worktree_fingerprint=fingerprint,
        )
        return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamCandidateReviewTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_review_candidate",
        description=(
            "Accept or return one frozen Candidate after semantic review. Runtime "
            "performs validation and candidate commit separately."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "rework"]},
                "reason": {"type": "string"},
            },
            "required": ["candidate_id", "decision", "reason"],
        },
    )

    def run(self, candidate_id: str, decision: str, reason: str) -> str:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.team_run_id != self.team_run_id:
            return "Error: Candidate does not belong to this TeamRun"
        reviewed = CandidateService(self.repository, self.worktrees).review(
            candidate.id,
            decision=decision,
            reviewed_by=self.lead_agent_id,
            reason=reason,
            command_id=f"lead-candidate:{candidate.id}:{candidate.revision}:{decision}",
        )
        if decision == "accept" and reviewed.user_approval_required:
            return (
                f"Candidate {reviewed.id} accepted by Lead; high-risk user approval "
                "is required before Runtime validation."
            )
        return json.dumps(reviewed.to_dict(), ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamWaitTool:
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_wait",
        description="Pause the Lead Session until a new user or Team event arrives.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )

    def run(self, reason: str) -> str:
        self.yield_callback(reason)
        return "Lead Session will pause at the next safe boundary."


def create_teammate_tools(
    repository: Any,
    worktrees: WorktreeManager | None,
    attempt: TaskAttemptRecord,
    *,
    task_kind: str = "code",
    write_enabled: bool = False,
    yield_callback: Callable[[str], None],
) -> list[Any]:
    common = [
        TeamProgressTool(repository, attempt),
        TeamQuestionTool(repository, attempt, yield_callback),
    ]
    if str(task_kind).lower() == "analysis":
        return [SubmitAnalysisResultTool(repository, attempt), *common]
    if worktrees is None:
        raise ValueError("Code Teammate tools require a Worktree manager")
    work_tool = (
        SubmitCandidateTool(
            CandidateService(repository, worktrees), attempt, yield_callback
        )
        if write_enabled
        else SubmitAttemptPlanTool(repository, worktrees, attempt, yield_callback)
    )
    return [work_tool, *common]


def create_lead_tools(
    repository: Any,
    worktrees: WorktreeManager,
    team_run_id: str,
    lead_agent_id: str,
    *,
    allow_code: bool,
    yield_callback: Callable[[str], None],
) -> list[Any]:
    return [
        TeamStatusTool(repository, team_run_id, allow_code),
        TeamCandidateContextTool(repository, worktrees, team_run_id),
        TeamAnswerQuestionTool(repository, team_run_id, lead_agent_id),
        TeamAttemptPlanDecisionTool(
            repository, worktrees, team_run_id, lead_agent_id
        ),
        TeamCandidateReviewTool(repository, worktrees, team_run_id, lead_agent_id),
        TeamWaitTool(yield_callback),
    ]


def _active_lead_generation(repository: Any, team: Any) -> int:
    sessions = repository.list_agent_sessions(team.id, role="lead")
    active = [
        session
        for session in sessions
        if session.state.value not in {"lost", "failed", "shutdown"}
    ]
    if len(active) != 1:
        raise RuntimeError("TeamRun must have exactly one active Lead Session")
    return active[0].generation


__all__ = [
    "SubmitAttemptPlanTool",
    "SubmitAnalysisResultTool",
    "SubmitCandidateTool",
    "TeamProgressTool",
    "TeamQuestionTool",
    "TeamStatusTool",
    "create_teammate_tools",
    "create_lead_tools",
]
