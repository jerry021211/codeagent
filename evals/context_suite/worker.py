"""Child process running the actual Web factory / Agent / context pipeline."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import time
from types import SimpleNamespace

from codeagent.context.history import history_hash
from codeagent.context.manager import SUMMARIZATION_SYSTEM_PROMPT
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.events.sink import RecordingEventSink
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.runtime.execution import ExecutionStopped
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from evals.agent_adapter import append_record, build_agent
from evals.evidence import read_json, write_json
from evals.tracing import run_with_trace


class OfflineContextSDK:
    """Oracle-injected plumbing test. Its scores are NEVER model quality."""
    def __init__(self, gold):
        self.messages = self
        self.gold = gold
        self.index = 0
        self.archive_path = None

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass

    def post(self, path, *, body, cast_to):
        assert path == "/v1/messages"
        return self.create(**body)

    def create(self, **kwargs):
        stop = "end_turn"
        if kwargs.get("system", "").startswith(SUMMARIZATION_SYSTEM_PROMPT.split("{summary_char_budget}", 1)[0]):
            text = "离线脚本检查点（不能用于评判摘要质量）：\n" + json.dumps(self.gold["expected"], ensure_ascii=False)
            content = [{"type": "text", "text": text}]
        else:
            self.index += 1
            if self.archive_path and self.index == 1:
                name, args = "load_tool_output", {"file_path": self.archive_path, "offset": 1201, "limit": 1}
            elif self.index == (2 if self.archive_path else 1):
                name, args = "write_file", {"file_path": "answer.json", "content": json.dumps(self.gold["expected"], ensure_ascii=False)}
            else:
                name = None
            if name:
                content = [{"type": "tool_use", "id": f"offline_{self.index}", "name": name, "input": args}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "离线链路检查完成；非模型质量结果。"}]
        return SimpleNamespace(content=content, stop_reason=stop, usage=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=Path, required=True)
    trial = parser.parse_args().trial.resolve()
    spec = read_json(trial / "manifest.json")
    seed = read_json(trial / "seed.json")
    workspace = trial / "workspace"
    import codeagent
    write_json(trial / "worker-runtime.json", {"engine_package": codeagent.__file__, "worker": __file__})
    result = {"execution_status": "setup_error"}
    agent = factory = recorder = None
    initial_hash, initial_count = None, 0
    started = time.monotonic()
    with SQLiteRepository(trial / "runtime/state.db", recover_incomplete=False) as repository:
        conversation = repository.create_conversation(title="Context evaluation", workspace=workspace)
        run = repository.create_run(conversation.id)
        repository.update_run_status(run.id, "running")
        recording = RecordingEventSink(repository)

        class Sink:
            durable = True

            def emit(self, event):
                recording.emit(event)
                append_record(trial / "events.jsonl", event.to_dict())

        emitter = EventEmitter(Sink(), context=ExecutionContext(conversation_id=conversation.id, run_id=run.id, turn_id=trial.name))
        try:
            offline = OfflineContextSDK(read_json(trial / "gold.json")) if spec["profile"]["mode"] == "offline" else None
            agent, factory, recorder = build_agent(
                spec["profile"], workspace, trial, repository, emitter, CancellationToken(),
                WaitingPermissionBroker(default_timeout=0), scripted_sdk=offline,
            )
            history = deepcopy(seed["messages"])
            for entry in seed["ingress"]:
                output = agent.context.finalize_tool_results([ToolUse(**entry["tool_use"])], [entry["raw_output"]])[0]
                history[entry["message_index"]]["content"][0]["content"] = output
                if offline is not None:
                    offline.archive_path = agent.context.state.tool_artifacts[-1]
            validate_tool_history(history)
            agent.messages = history
            # A fresh real user turn; no injected summary, cursor, previous peak or
            # facts in runtime state. Only actual ingress artifact paths exist.
            write_json(trial / "seed-state.json", serialize_runtime_state(agent.context.state))
            write_json(trial / "effective-context.json", {k: str(v) if isinstance(v, Path) else v for k, v in asdict(agent.context.config).items()})
            write_json(trial / "effective-loop-guard.json", asdict(agent.config.loop_guard))
            write_json(trial / "tool-schemas.json", agent.tools.schemas())
            append_record(trial / "initial-history.jsonl", {"messages": history})
            initial_hash = history_hash(history)
            initial_count = len(history)
            outcome = run_with_trace(agent, seed["prompt"], trial=trial, spec=spec)
            result.update(execution_status="completed" if outcome.stop_reason == "end_turn" else "agent_failed",
                          stop_reason=outcome.stop_reason, iterations=outcome.iterations, final_text=outcome.final_text,
                          canonical_prefix_unchanged=history_hash(agent.messages[:initial_count]) == initial_hash)
        except ExecutionStopped as exc:
            result.update(execution_status="agent_failed", stop_reason=exc.reason, error_type=type(exc).__name__,
                          canonical_prefix_unchanged=initial_hash is not None and history_hash(agent.messages[:initial_count]) == initial_hash)
        except Exception as exc:
            result.update(execution_status="error", error_type=type(exc).__name__, error=str(exc))
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            if agent is not None:
                state = serialize_runtime_state(agent.context.state)
                append_record(trial / "messages.jsonl", {"messages": agent.messages})
                write_json(trial / "final-state.json", state)
                try:
                    repository.finish_run_with_checkpoint(run.id, status="completed" if result["execution_status"] == "completed" else "failed", messages=agent.messages, todos=[], context=state)
                except Exception as exc:
                    result["checkpoint_error"] = f"{type(exc).__name__}: {exc}"
            if recorder is not None:
                result["api_calls"] = recorder.count
                recorder.close()
            if factory is not None:
                factory.close()
            append_record(trial / "worker-result.jsonl", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
