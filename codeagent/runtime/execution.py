"""One logical execution's budgets, independent of context projection."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Callable, Iterator


class ExecutionStopped(BaseException):
    """Control flow: ordinary tool/provider error recovery must not retry it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def is_execution_failure(reason: str) -> bool:
    return reason.startswith(("recovery_failed", "max_iterations", "runtime_contract",
                              "loop_detected:", "budget_exceeded:"))


@dataclass(slots=True)
class BudgetState:
    model_calls: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    unknown_usage_calls: int = 0
    active_seconds: float = 0.0
    stop_reason: str = ""


class RunBudget:
    """Atomic reservations; parent/child active intervals are counted once."""

    def __init__(self, config: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.state = BudgetState()
        self._lock = RLock()
        self._running = 0
        self._paused = 0
        self._last_tick = clock()

    def _tick(self) -> None:
        now = self.clock()
        if self._running and not self._paused:
            self.state.active_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now

    def reset(self) -> None:
        with self._lock:
            self.state = BudgetState()
            self._last_tick = self.clock()

    @contextmanager
    def running(self) -> Iterator[None]:
        with self._lock:
            self._tick()
            self._running += 1
        try:
            yield
        finally:
            with self._lock:
                self._tick()
                self._running -= 1

    @contextmanager
    def paused(self) -> Iterator[None]:
        with self._lock:
            self._tick()
            self._paused += 1
        try:
            yield
        finally:
            with self._lock:
                self._tick()
                self._paused -= 1

    def check(self) -> None:
        with self._lock:
            self._tick()
            if self.state.active_seconds >= self.config.max_active_seconds:
                self.state.stop_reason = self.state.stop_reason or "budget_exceeded:active_time"
            if self.config.max_total_tokens and self.state.total_tokens >= self.config.max_total_tokens:
                self.state.stop_reason = self.state.stop_reason or "budget_exceeded:tokens"
            if self.state.stop_reason:
                raise ExecutionStopped(self.state.stop_reason)

    def remaining_seconds(self) -> float:
        with self._lock:
            self.check()
            return self.config.max_active_seconds - self.state.active_seconds

    def reserve(self, kind: str) -> None:
        with self._lock:
            self.check()
            name = f"{kind}_calls"
            if getattr(self.state, name) >= getattr(self.config, f"max_{name}"):
                self.state.stop_reason = f"budget_exceeded:{name}"
                raise ExecutionStopped(self.state.stop_reason)
            setattr(self.state, name, getattr(self.state, name) + 1)

    def invoke(self, client: Any, **kwargs: Any) -> Any:
        self.reserve("model")
        response = None
        try:
            response = client.create_message(**kwargs)
        finally:
            # Exactly once per actual client invocation, never from replayable events.
            usage = getattr(response, "usage", None)
            with self._lock:
                if usage is None or not usage.available or usage.estimated:
                    self.state.unknown_usage_calls += 1
                else:
                    self.state.total_tokens += usage.total_tokens
        self.check()
        return response

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._tick()
            return asdict(self.state)

    def restore(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.state = BudgetState(**payload)
            self._last_tick = self.clock()


class BudgetedClient:
    """Charge side calls and their forks to the same root execution."""

    def __init__(self, client: Any, budget: RunBudget) -> None:
        self.client = client
        self.budget = budget

    def create_message(self, **kwargs: Any) -> Any:
        return self.budget.invoke(self.client, **kwargs)

    def fork(self, **kwargs: Any) -> BudgetedClient:
        fork = getattr(self.client, "fork", None)
        return BudgetedClient(fork(**kwargs) if callable(fork) else self.client, self.budget)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)
