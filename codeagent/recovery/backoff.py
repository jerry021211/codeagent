"""Backoff helpers for transient recovery."""

from __future__ import annotations

import random

from codeagent.recovery.models import RecoveryConfig


def retry_delay_seconds(
    attempt: int,
    config: RecoveryConfig,
    *,
    retry_after: float | None = None,
) -> float:
    """Exponential backoff with capped jitter."""

    if retry_after is not None:
        return max(0.0, retry_after)
    base_ms = min(
        config.base_delay_ms * (2 ** max(0, attempt - 1)),
        config.max_delay_ms,
    )
    jitter = random.uniform(0, base_ms * config.jitter_ratio)
    return (base_ms + jitter) / 1000
