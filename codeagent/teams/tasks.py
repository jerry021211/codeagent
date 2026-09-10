"""Small execution checks shared by Team planning and scheduling."""

from collections.abc import Mapping
from typing import Any


def validate_task_execution(metadata: Mapping[str, Any]) -> None:
    """Validate declared capabilities, not the meaning of a natural-language task."""
    if not isinstance(metadata, Mapping):
        raise ValueError("Team Task metadata must be an object")
    kind = metadata.get("kind", "analysis")
    if kind not in ("analysis", "code"):
        raise ValueError(f"Unsupported Team Task kind: {kind}")
    scopes = metadata.get("write_scopes", [])
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) or not scope.strip() for scope in scopes
    ):
        raise ValueError("Team write_scopes must be an array of non-empty paths")
    if kind == "analysis" and scopes:
        raise ValueError(
            "Read-only analysis Tasks cannot declare write_scopes. "
            "Creating or editing repository files (including documentation) requires "
            "a code Task and a user-approved Team Plan."
        )
    plan_required = metadata.get("plan_required", False)
    if not isinstance(plan_required, bool):
        raise ValueError(
            "Team plan_required must be a JSON boolean (true or false), not a string or number"
        )
    if kind == "analysis" and plan_required:
        raise ValueError(
            "Read-only analysis Tasks cannot require an Attempt Plan. "
            "Use plan_required=false and submit a report; file changes require a code Task."
        )
    risk = metadata.get("risk_level", "low")
    if risk not in ("low", "medium", "high"):
        raise ValueError("Team risk_level must be low, medium, or high")


def requires_attempt_plan(metadata: Mapping[str, Any]) -> bool:
    """Derive the write-approval requirement from validated Task metadata."""
    validate_task_execution(metadata)
    return metadata.get("kind", "analysis") == "code" and (
        metadata.get("plan_required", False) or metadata.get("risk_level") == "high"
    )
