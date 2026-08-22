"""Event sinks and emitters shared by CLI and Web adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import Lock
from typing import Any, Protocol

from codeagent.events.models import ExecutionContext, RunEvent
from codeagent.events.redaction import redact_payload


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None: ...


class NullEventSink:
    def emit(self, event: RunEvent) -> None:
        return None


class CallbackEventSink:
    def __init__(self, callback: Callable[[RunEvent], None]) -> None:
        self.callback = callback

    def emit(self, event: RunEvent) -> None:
        self.callback(event)


class RecordingEventSink:
    """Persist events and derive model-call records from the same stream."""

    durable = True

    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._model_calls: set[str] = set()
        self._prompt_metadata: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = Lock()

    def emit(self, event: RunEvent) -> None:
        persisted = self.repository.append_event(event)
        if persisted.type == "prompt.assembled":
            self._remember_prompt_metadata(persisted)
        if persisted.type in {"model.started", "model.completed", "model.failed"}:
            self._record_model_event(persisted)

    def _remember_prompt_metadata(self, event: RunEvent) -> None:
        observed_fields = {
            "iteration": event.iteration,
            "prompt_hash": event.payload.get("prompt_hash"),
            "history_generation": event.payload.get("history_generation"),
            "history_rewritten": event.payload.get("history_rewritten"),
            "rewrite_reason": event.payload.get("rewrite_reason"),
            "generation_reason": event.payload.get("generation_reason"),
            "previous_message_count": event.payload.get("previous_message_count"),
            "current_message_count": event.payload.get("current_message_count"),
            "message_count_delta": event.payload.get("message_count_delta"),
            "common_prefix_messages": event.payload.get("common_prefix_messages"),
            "previous_suffix_messages": event.payload.get(
                "previous_suffix_messages"
            ),
            "current_suffix_messages": event.payload.get("current_suffix_messages"),
            "previous_history_hash": event.payload.get("previous_history_hash"),
            "current_history_hash": event.payload.get("current_history_hash"),
        }
        with self._lock:
            self._prompt_metadata[(event.run_id, event.agent_id)] = observed_fields

    def _record_model_event(self, event: RunEvent) -> None:
        payload = event.payload
        call_id = str(payload.get("call_id") or "")
        if not call_id:
            return
        try:
            with self._lock:
                if event.type == "model.started":
                    if call_id in self._model_calls:
                        return
                    call_kind = str(payload.get("call_kind") or "main")
                    self.repository.create_model_call(
                        event.run_id,
                        model=str(payload.get("model") or ""),
                        call_kind=call_kind,
                        agent_id=event.agent_id,
                        parent_agent_id=event.parent_agent_id,
                        metadata=self._model_call_metadata(event, call_kind),
                        model_call_id=call_id,
                        started_at=event.occurred_at,
                    )
                    self._model_calls.add(call_id)
                    return
                if call_id not in self._model_calls:
                    call_kind = str(payload.get("call_kind") or "main")
                    self.repository.create_model_call(
                        event.run_id,
                        model=str(payload.get("model") or ""),
                        call_kind=call_kind,
                        agent_id=event.agent_id,
                        parent_agent_id=event.parent_agent_id,
                        metadata=self._model_call_metadata(event, call_kind),
                        model_call_id=call_id,
                        started_at=event.occurred_at,
                    )
                    self._model_calls.add(call_id)
                self.repository.complete_model_call(
                    call_id,
                    usage=payload.get("usage"),
                    status="failed" if event.type == "model.failed" else "completed",
                    error=payload.get("error"),
                    duration_ms=_optional_int(payload.get("duration_ms")),
                    completed_at=event.occurred_at,
                )
        except Exception:
            # Model-call analytics are derived; the replayable event remains the
            # source of truth and must not make execution fail.
            return

    def _model_call_metadata(self, event: RunEvent, call_kind: str) -> dict[str, Any]:
        if call_kind != "main":
            return {}
        return dict(self._prompt_metadata.get((event.run_id, event.agent_id), {}))


class CompositeEventSink:
    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = sinks
        self.durable = any(getattr(sink, "durable", False) for sink in sinks)

    def emit(self, event: RunEvent) -> None:
        for sink in self.sinks:
            sink.emit(event)


class EventSequencer:
    """Allocate monotonically increasing sequence numbers for one run."""

    def __init__(self, start: int = 0) -> None:
        self._value = start
        self._lock = Lock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


class EventEmitter:
    """Attach execution identity, ordering, redaction, and failure isolation."""

    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        context: ExecutionContext | None = None,
        sequencer: EventSequencer | None = None,
    ) -> None:
        self.sink = sink or NullEventSink()
        self.context = context or ExecutionContext()
        self.sequencer = sequencer or EventSequencer()

    def emit(
        self,
        event_type: str,
        payload: dict | None = None,
        *,
        iteration: int | None = None,
    ) -> RunEvent:
        event = RunEvent(
            type=event_type,
            payload=redact_payload(payload or {}),
            seq=self.sequencer.next(),
            conversation_id=self.context.conversation_id,
            run_id=self.context.run_id,
            turn_id=self.context.turn_id,
            agent_id=self.context.agent_id,
            parent_agent_id=self.context.parent_agent_id,
            iteration=iteration,
        )
        try:
            self.sink.emit(event)
        except Exception as exc:
            # The CLI treats observability as best effort. A durable Web run
            # must fail closed when its event journal cannot be written.
            if getattr(self.sink, "durable", False):
                raise RuntimeError("Unable to persist run event") from exc
        return event

    def child(self, *, agent_id: str) -> "EventEmitter":
        return EventEmitter(
            self.sink,
            context=self.context.child(agent_id=agent_id),
            sequencer=self.sequencer,
        )

    def with_context(self, context: ExecutionContext) -> "EventEmitter":
        return EventEmitter(self.sink, context=context, sequencer=self.sequencer)

    def with_agent(self, *, agent_id: str, parent_agent_id: str | None) -> "EventEmitter":
        return EventEmitter(
            self.sink,
            context=replace(
                self.context,
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
            ),
            sequencer=self.sequencer,
        )


def _optional_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
