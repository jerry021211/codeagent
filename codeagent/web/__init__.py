"""Local web-runtime persistence contracts."""

from codeagent.web.models import (
    ApprovalRecord,
    CheckpointRecord,
    ConversationRecord,
    MessageRecord,
    ModelCallRecord,
    RunRecord,
)
from codeagent.web.storage import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    InvalidStateTransitionError,
    RecordNotFoundError,
    Repository,
    SQLiteEventSink,
    SQLiteRepository,
    StorageConflictError,
    StorageError,
)
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler


def create_app(**kwargs):
    """Lazily import FastAPI so core-only installations remain importable."""

    from codeagent.web.api import create_app as build_app

    return build_app(**kwargs)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ApprovalRecord",
    "CheckpointRecord",
    "ConversationRecord",
    "InvalidStateTransitionError",
    "MessageRecord",
    "ModelCallRecord",
    "RecordNotFoundError",
    "Repository",
    "RunRecord",
    "SQLiteEventSink",
    "SQLiteRepository",
    "StorageConflictError",
    "StorageError",
    "TERMINAL_RUN_STATUSES",
    "RunScheduler",
    "WebAgentFactory",
    "create_app",
]
