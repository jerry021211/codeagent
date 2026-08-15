"""Backward-compatible retry strategy wrapper."""

from __future__ import annotations

from codeagent.recovery.classifier import classify_exception
from codeagent.recovery.models import RecoveryReason


class RecoveryStrategy:
    """Compatibility shim for older callers."""

    def should_retry(self, error: Exception) -> bool:
        return classify_exception(error) in {
            RecoveryReason.RATE_LIMIT_RETRY,
            RecoveryReason.OVERLOADED_RETRY,
            RecoveryReason.TRANSIENT_RETRY,
            RecoveryReason.UNKNOWN_RETRY,
        }
