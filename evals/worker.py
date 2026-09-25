"""One Agent per child process. Deadline enforcement is owned by the parent."""
from __future__ import annotations

import argparse
from pathlib import Path
import time

from codeagent.events import EventEmitter, ExecutionContext
from codeagent.events.sink import RecordingEventSink
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.runtime.execution import ExecutionStopped
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from evals.agent_adapter import append_record, build_agent, public_trace
from evals.evidence import read_json, write_json
from evals.tracing import run_with_trace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=Path, required=True)
    args = parser.parse_args()
    trial = args.trial.resolve()
    workspace = trial / "workspace"
    spec = read_json(trial / "manifest.json")
    import codeagent
    write_json(trial / "worker-runtime.json", {"engine_package": codeagent.__file__, "adapter": __file__})
    started = None
    agent = factory = recorder = None
    result = {"execution_status": "setup_error", "iterations": None, "duration_ms": None}
    with SQLiteRepository(trial / "runtime/state.db", recover_incomplete=False) as repository:
        conversation = repository.create_conversation(title="C01 evaluation", workspace=workspace)
        run = repository.create_run(conversation.id)
        repository.update_run_status(run.id, "running")
        recording = RecordingEventSink(repository)

        class Sink:
            durable = True
            def emit(self, event):
                recording.emit(event)
                persisted = repository.get_event(event.id)
                append_record(trial / "events.jsonl", persisted.to_dict())

        emitter = EventEmitter(Sink(), context=ExecutionContext(conversation_id=conversation.id, run_id=run.id, turn_id=trial.name))
        broker = WaitingPermissionBroker(default_timeout=0)
        try:
            agent, factory, recorder = build_agent(spec["profile"], workspace, trial, repository, emitter, CancellationToken(), broker)
            write_json(trial / "tool-schemas.json", agent.tools.schemas())
            started = time.monotonic()
            outcome = run_with_trace(agent, spec["prompt"], trial=trial, spec=spec)
            result.update(execution_status="completed" if outcome.stop_reason == "end_turn" else "agent_failed", stop_reason=outcome.stop_reason, iterations=outcome.iterations, usage_tracker=outcome.usage.to_dict(), final_text=outcome.final_text)
        except ExecutionStopped as exc:
            result.update(execution_status="agent_failed", stop_reason=exc.reason, error_type=type(exc).__name__)
        except Exception as exc:
            result.update(execution_status="error" if started is not None else "setup_error", error_type=type(exc).__name__, error=str(exc))
        finally:
            if started is not None:
                result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            if agent is not None:
                append_record(trial / "messages.jsonl", {"messages": public_trace(agent.messages)})
                try:
                    repository.finish_run_with_checkpoint(run.id, status="completed" if result["execution_status"] == "completed" else "failed", messages=agent.messages, todos=[], context=serialize_runtime_state(agent.context.state))
                except Exception as exc:
                    result["checkpoint_error"] = f"{type(exc).__name__}: {exc}"
            if recorder is not None:
                recorder.close()
            if factory is not None:
                factory.close()
            append_record(trial / "worker-result.jsonl", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
