"""Thread-safe cooperative cancellation primitives."""

from __future__ import annotations

import threading


class CancelledError(RuntimeError):
    """Raised when cooperative execution has been cancelled."""

    def __init__(self, reason: str = "Cancelled by user") -> None:
        self.reason = reason
        super().__init__(reason)


class ModelCallTimeout(CancelledError):
    """A runtime deadline, not a retryable provider/network error."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CancellationToken:
    """A small thread-safe cancellation signal shared across runtime layers.

    Cancellation is cooperative: callers should invoke :meth:`raise_if_cancelled`
    at safe boundaries, or use :meth:`wait` instead of an uninterruptible sleep.
    The first cancellation reason wins so every observer reports the same cause.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "Cancelled by user"
        self._reason_code: str | None = None

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self._event.is_set()

    @property
    def reason(self) -> str:
        """The stable reason associated with this token."""

        with self._lock:
            return self._reason

    def cancel(self, reason: str = "Cancelled by user", *, reason_code: str | None = None) -> None:
        """Request cancellation. Repeated calls do not replace the reason."""

        normalized_reason = str(reason).strip() or "Cancelled by user"
        with self._lock:
            if self._event.is_set():
                return
            self._reason = normalized_reason
            self._reason_code = reason_code
            self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`CancelledError` when cancellation was requested."""

        if self._event.is_set():
            if self._reason_code in {"model_response_timeout", "model_call_timeout"}:
                raise ModelCallTimeout(self._reason_code)
            raise CancelledError(self.reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation and return ``True`` if it was requested."""

        return self._event.wait(timeout)
