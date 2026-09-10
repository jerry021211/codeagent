"""In-process execution deadlines; activity is not a periodic keepalive."""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from codeagent.runtime.cancellation import CancellationToken

MODEL_TIMEOUT_REASONS = frozenset({"model_response_timeout", "model_call_timeout"})


@dataclass(slots=True)
class _Operation:
    kind: str
    deadline: float
    last_response: float
    receiving: bool = False
    close: Callable[[], None] | None = None


class ExecutionActivity:
    """One worker's bounded waits, polled by the existing supervisor.

    Nested side calls cannot extend outer deadlines. Elapsed time is monotonic;
    UTC heartbeat timestamps are only for storage/display. No thread is created.
    """

    def __init__(
        self,
        cancellation: CancellationToken,
        *,
        response_timeout: float = 300.0,
        model_timeout: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(
            not math.isfinite(value) or value <= 0
            for value in (response_timeout, model_timeout)
        ):
            raise ValueError("Model timeouts must be positive")
        self.cancellation = cancellation
        self.response_timeout = response_timeout
        self.model_timeout = model_timeout
        self.clock = clock
        self.on_activity: Callable[[str], None] | None = None
        self._lock = threading.RLock()
        self._operations: list[_Operation] = []
        self._last_activity = clock()
        self._last_report = float("-inf")
        self._last_label = ""

    @contextmanager
    def operation(self, kind: str, deadline: float) -> Iterator[None]:
        operation = _Operation(kind, deadline, self.clock())
        with self._lock:
            self._operations.append(operation)
        try:
            self.touch()
            self.check()
            yield
            self.check()
        finally:
            with self._lock:
                self._operations.remove(operation)
            self.touch()

    @contextmanager
    def model_request(self) -> Iterator[None]:
        with self._lock:
            covered = bool(self._operations and self._operations[-1].kind == "model")
        if covered:
            self.check()
            yield
            self.check()
        else:
            # Direct calls such as context compression still have a deadline.
            with self.operation("model", self.clock() + self.model_timeout):
                yield

    def touch(self, *, response: bool = False) -> None:
        now = self.clock()
        with self._lock:
            self._last_activity = now
            current = self._operations[-1] if self._operations else None
            if response and current is not None and current.kind == "model":
                current.last_response = now
                current.receiving = True
            label = "executing"
            if current is not None:
                label = {
                    "model": "model_receiving"
                    if current.receiving
                    else "model_waiting",
                    "tool": "tool_executing",
                    "retry": "model_retry_wait",
                    "approval": "permission_waiting",
                }[current.kind]
            report = label != self._last_label or now - self._last_report >= 10
            if report:
                self._last_label, self._last_report = label, now
        if (
            report
            and self.on_activity is not None
            and not self.cancellation.is_cancelled
        ):
            self.on_activity(label)

    def timeout_reason(self, heartbeat_timeout: float | None = None) -> str | None:
        now = self.clock()
        with self._lock:
            for operation in self._operations:
                if operation.kind in {"model", "retry"} and now >= operation.deadline:
                    return "model_call_timeout"
            if self._operations:
                current = self._operations[-1]
                if (
                    current.kind == "model"
                    and now - current.last_response >= self.response_timeout
                ):
                    return "model_response_timeout"
                if current.kind == "tool" and now >= current.deadline:
                    return "worker_heartbeat_timeout"
                return None
            if (
                heartbeat_timeout is not None
                and now - self._last_activity >= heartbeat_timeout
            ):
                return "worker_heartbeat_timeout"
        return None

    def check(self) -> None:
        self.cancellation.raise_if_cancelled()
        reason = self.timeout_reason()
        if reason is not None:
            self.cancellation.cancel(reason, reason_code=reason)
            self.cancellation.raise_if_cancelled()

    def request_timeout(self) -> float:
        """Also bound blocking SDK I/O, including non-streaming requests."""
        self.check()
        with self._lock:
            remaining = min(op.deadline for op in self._operations) - self.clock()
        return max(0.001, min(self.response_timeout, remaining))

    def set_request_closer(self, close: Callable[[], None] | None) -> None:
        with self._lock:
            self._operations[-1].close = close

    def interrupt_request(self) -> None:
        """Close only this response, never the shared SDK/HTTP client."""
        with self._lock:
            closers = [op.close for op in self._operations if op.close is not None]
            for op in self._operations:
                op.close = None
        for close in closers:
            try:
                close()
            except Exception:
                # Cancellation stays set; resources remain held until exit.
                logging.getLogger(__name__).warning(
                    "Response stream close failed", exc_info=True
                )
