"""Offline-only subprocess for deterministic checkpoint lifecycle checks."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
from unittest.mock import patch

from codeagent import EnvironmentConfig, ModelResponse, RecoveryConfig
from codeagent.context import ContextCompactionError, ContextConfig
from codeagent.context.history import history_hash
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import MemoryConfig
from codeagent.messages import validate_tool_history
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken, RuntimeDataPaths
from codeagent.tools import LoadContextHistoryTool
from codeagent.web.factory import WebAgentFactory, serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from evals.context_suite.lifecycle import stage_messages
from evals.evidence import read_json, write_json


class ScriptedClient:
    """No SDK and no network. Answers are injected only to test runtime rules."""
    def __init__(self, case: Path, phase: str):
        self.case, self.phase = case, phase
        self.stage = 1
        self.truncate = False
        self.calls = []

    def fork(self, **kwargs):
        return self

    def create_message(self, **kwargs):
        summary = kwargs.get("model") == "offline-summary"
        self.calls.append(deepcopy(kwargs))
        stop = "max_tokens" if summary and self.truncate else "end_turn"
        text = (f"SCRIPTED_SUMMARY_STAGE_{self.stage}: 固定脚本摘要，仅验证规则。"
            "目标 reports/客户汇总.json；JSON；姓名、金额；UTF-8；禁止覆盖；金额两位小数；"
            "单元测试通过、集成失败、全量未执行、尚未发布；receipt-R03-2026。") if summary else "离线恢复后的续答完成。"
        if stop == "max_tokens":
            text = "截断的摘要，下一步应该"
        response = ModelResponse(stop_reason=stop, content=[{"type": "text", "text": text}])
        with (self.case / f"{self.phase}-scripted-calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "context_summary" if summary else "main", "request": kwargs,
                "response": {"stop_reason": stop, "content": response.content}, "usage": None,
                "real_model": False}, ensure_ascii=False, default=str) + "\n")
        return response


def _check(checks, key, value):
    checks[key] = bool(value)
    if not value:
        raise AssertionError(key)


def _environment(case: Path):
    return EnvironmentConfig(model_id="offline-main", api_key="offline-placeholder", stream=False,
        enable_skills=False, max_iterations=2, max_tokens=1000,
        context_config=ContextConfig(summarization_model="offline-summary", tool_projection_enabled=False,
            compact_threshold_chars=300000, max_request_chars=600000),
        memory_config=MemoryConfig(enabled=False, auto_extract=False),
        recovery_config=RecoveryConfig(max_retries=0, side_query_max_retries=0, sleep_enabled=False),
        data_dir=case / "runtime", mcp_config_path=case / "disabled-mcp.json", team_runtime_enabled=False)


def execute(case: Path, phase: str) -> dict:
    checks = {}
    result = {"phase": phase, "pid": os.getpid(), "passed": False, "checks": checks, "real_model_calls": 0}
    client = ScriptedClient(case, phase)
    agent = factory = None
    try:
        (case / "workspace").mkdir(exist_ok=True)
        with SQLiteRepository(case / "runtime/state.db", recover_incomplete=False) as repository:
            checkpoint = None
            if phase == "prepare":
                conversation = repository.create_conversation(title=f"Lifecycle {case.name}", workspace=case / "workspace")
                conversation_id = conversation.id
                write_json(case / "conversation.json", {"id": conversation_id})
            else:
                conversation_id = read_json(case / "conversation.json")["id"]
                checkpoint = repository.get_latest_checkpoint(conversation_id)
                _check(checks, "checkpoint_exists", checkpoint is not None)
            run = repository.create_run(conversation_id)
            repository.update_run_status(run.id, "running")
            emitter = EventEmitter(context=ExecutionContext(conversation_id=conversation_id, run_id=run.id))
            factory = WebAgentFactory(_environment(case), case / "workspace", repository,
                data_paths=RuntimeDataPaths(case / "runtime"))
            with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
                agent = factory.create(event_emitter=emitter, cancellation=CancellationToken(),
                    permission_broker=WaitingPermissionBroker(default_timeout=0), checkpoint=checkpoint)
            agent.allow_subagents = False
            agent.subagent_environment_factory = None
            if phase == "prepare":
                prior_cursor = 0
                for stage in range(1, 4):
                    agent.messages.extend(stage_messages(stage))
                    agent.context.begin_turn(len(agent.messages))
                    before = deepcopy(agent.messages)
                    client.stage = stage
                    agent.context.force_compact(agent.messages, client=client, event_emitter=emitter)
                    _check(checks, f"stage_{stage}_summary_committed", agent.context.state.summary_revision == stage)
                    _check(checks, f"stage_{stage}_cursor_advanced", agent.context.state.compacted_message_count > prior_cursor)
                    _check(checks, f"stage_{stage}_canonical_unchanged", agent.messages == before)
                    if stage > 1:
                        source = str(client.calls[-1]["messages"])
                        _check(checks, f"stage_{stage}_previous_summary_in_source", f"SCRIPTED_SUMMARY_STAGE_{stage - 1}" in source)
                        _check(checks, f"stage_{stage}_old_raw_history_excluded", "STAGE_1_EVIDENCE_00" not in source)
                    prior_cursor = agent.context.state.compacted_message_count
                    write_json(case / f"stage-{stage}-state.json", serialize_runtime_state(agent.context.state))
                view = agent.context.project_messages(agent.messages)
                write_json(case / "before-messages.json", agent.messages)
                write_json(case / "before-view.json", view)
                write_json(case / "before-state.json", serialize_runtime_state(agent.context.state))
                if case.name == "R02":
                    agent.messages[0]["content"] += " TAMPERED_PREFIX"
                repository.finish_run_with_checkpoint(run.id, status="completed", messages=agent.messages, todos=[],
                    context=serialize_runtime_state(agent.context.state), checkpoint_metadata={"tool_history_version": 1})
                _check(checks, "sqlite_checkpoint_saved", repository.get_latest_checkpoint(conversation_id) is not None)
            else:
                initial = read_json(case / "before-state.json")
                prepared = read_json(case / "prepare-result.json")
                _check(checks, "new_python_process", prepared["pid"] != os.getpid())
                if case.name == "R02":
                    view = agent.context.project_messages(agent.messages)
                    _check(checks, "changed_message_preserved", "TAMPERED_PREFIX" in str(view))
                    _check(checks, "stale_cursor_rejected", agent.context.state.compacted_message_count == 0)
                    _check(checks, "stale_summary_rejected", agent.context.state.summary_text == "")
                    _check(checks, "reason_history_changed", agent.context.last_compaction.get("reason") == "history_changed")
                else:
                    _check(checks, "restored_state_matches", serialize_runtime_state(agent.context.state) == initial)
                    _check(checks, "restored_messages_match", agent.messages == read_json(case / "before-messages.json"))
                    view = agent.context.project_messages(agent.messages)
                    _check(checks, "restored_request_view_matches", view == read_json(case / "before-view.json"))
                    archive = LoadContextHistoryTool(agent.context.config.transcript_dir).run(
                        agent.context.state.summary_transcript, message_offset=1, message_limit=1)
                    (case / "restored-archive-read.txt").write_text(archive, encoding="utf-8")
                    _check(checks, "earliest_linked_archive_readable", "STAGE_1_FACT" in archive)
                    agent.messages.extend(stage_messages(4))
                    agent.context.begin_turn(len(agent.messages))
                    canonical = deepcopy(agent.messages)
                    saved = asdict(agent.context.state)
                    client.stage = 4
                    if case.name == "R03":
                        client.truncate = True
                        rejected = False
                        try:
                            agent.context.force_compact(agent.messages, client=client, event_emitter=emitter)
                        except ContextCompactionError:
                            rejected = True
                        _check(checks, "truncated_summary_rejected", rejected)
                        for field in ("summary_text", "compacted_message_count", "compacted_prefix_hash",
                                      "summary_revision", "summary_transcript", "summary_source_count", "summary_source_hash"):
                            _check(checks, f"unchanged_{field}", getattr(agent.context.state, field) == saved[field])
                        _check(checks, "canonical_unchanged", agent.messages == canonical)
                        _check(checks, "failure_cooldown_recorded", agent.context.state.summary_retry_after_epoch > 0)
                    else:
                        agent.context.force_compact(agent.messages, client=client, event_emitter=emitter)
                        _check(checks, "fourth_summary_committed", agent.context.state.summary_revision == 4)
                        _check(checks, "cursor_advanced_after_resume", agent.context.state.compacted_message_count > initial["compacted_message_count"])
                        outcome = agent.run("请根据当前状态继续；这是离线规则检查。")
                        _check(checks, "agent_continued_after_restore", outcome.stop_reason == "end_turn")
                        _check(checks, "canonical_prefix_unchanged", agent.messages[:len(canonical)] == canonical)
                validate_tool_history(agent.messages)
                validate_tool_history(agent.context.project_messages(agent.messages))
                _check(checks, "valid_tool_pairs", True)
                repository.finish_run_with_checkpoint(run.id, status="completed", messages=agent.messages, todos=[],
                    context=serialize_runtime_state(agent.context.state), checkpoint_metadata={"tool_history_version": 1})
            result["passed"] = True
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        if agent is not None:
            write_json(case / f"{phase}-messages.json", agent.messages)
            write_json(case / f"{phase}-state.json", serialize_runtime_state(agent.context.state))
            result["canonical_hash"] = history_hash(agent.messages)
        if factory is not None:
            factory.close()
        result["scripted_calls"] = len(client.calls)
        write_json(case / f"{phase}-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "resume"), required=True)
    args = parser.parse_args()
    # Defense against accidental SDK/network additions to an offline rule check.
    with patch("socket.socket.connect", side_effect=RuntimeError("offline lifecycle forbids network")):
        result = execute(args.case.resolve(), args.phase)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
