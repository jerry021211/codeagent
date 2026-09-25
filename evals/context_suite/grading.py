"""Independent deterministic grading of deliverables and observable evidence."""
from __future__ import annotations

import json
from pathlib import Path

from evals.evidence import hashes, read_json
from evals.metrics import request_usage, summarize
from codeagent.context.manager import SUMMARIZATION_SYSTEM_PROMPT
from codeagent.context.projection import TOOL_VIEW_MARKER, WRITE_VIEW_MARKER

GRADING_VERSION = "context-v2"
REASONS = {
    "api_call_limit": "模型调用次数达到上限",
    "timeout": "运行超时",
    "iteration_limit": "Agent 轮数达到上限",
    "execution_failed": "运行未正常完成",
    "answer_missing": "没有生成 answer.json",
    "answer_invalid": "答案文件不合法或不可读取",
    "answer_mismatch": "答案字段、类型或取值不符合要求",
    "unexpected_changes": "修改了不允许修改的文件",
    "archive_evidence_missing": "未通过归档工具取得所需证据",
    "canonical_changed": "无法确认原始历史保持不变",
    "evidence_invalid": "记录不完整或格式错误",
    "checkpoint_failed": "保存检查点失败",
}


def field_equal(key, actual, expected, gold):
    # Only these format enums are case-insensitive. Paths, IDs, JSON keys,
    # field order and booleans continue to require exact matches.
    if gold.get("id") in {"S02", "S03"} and key == "format" and expected == "JSON" and isinstance(actual, str):
        return actual.casefold() == "json"
    return strict_equal(actual, expected)


def records(path: Path):
    result, errors = [], 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                errors += 1
    return result, errors


def strict_equal(a, b):
    # Python considers False == 0: that is not a valid boolean JSON answer.
    if type(a) is not type(b):
        return False
    if isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], v) for k, v in b.items())
    if isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def grade_answer(path: Path, gold: dict):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
            raise ValueError("missing, linked or oversized answer.json")
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError("duplicate JSON key")
                out[key] = value
            return out
        answer = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
        checks = {key: isinstance(answer, dict) and key in answer and field_equal(key, answer[key], value, gold)
                  for key, value in gold["expected"].items()}
        passed = isinstance(answer, dict) and answer.keys() == gold["expected"].keys() and all(checks.values())
        normalized = [key for key, ok in checks.items() if ok and not strict_equal(answer[key], gold["expected"][key])]
        return {"passed": passed, "checks": checks, "normalized_fields": normalized, "grading_version": GRADING_VERSION,
                "field_accuracy": sum(checks.values()) / len(checks), "answer": answer}
    except (OSError, ValueError) as exc:
        return {"passed": False, "checks": {}, "field_accuracy": 0.0, "error": str(exc), "grading_version": GRADING_VERSION}


def grade_trial(trial: Path, execution: dict):
    spec, gold = read_json(trial / "manifest.json"), read_json(trial / "gold.json")
    answer = grade_answer(trial / "workspace/answer.json", gold)
    final_files = hashes(trial / "workspace")
    original = spec["initial_files"]
    changed = sorted(p for p in set(original) | set(final_files) if original.get(p) != final_files.get(p))
    allowed = set(changed) <= {"answer.json"}
    events, event_errors = records(trial / "events.jsonl")
    requests, request_errors = records(trial / "model-requests.jsonl")
    responses, response_errors = records(trial / "model-responses.jsonl")
    blocked_records, blocked_errors = records(trial / "model-blocked.jsonl")
    messages, message_errors = records(trial / "messages.jsonl")
    canonical = messages[-1]["messages"] if messages else []
    calls = {}
    recalls = []
    for message in canonical:
        for block in message["content"] if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_use":
                calls[block["id"]] = block["name"]
            if block.get("type") == "tool_result" and calls.get(block.get("tool_use_id")) in {"load_tool_output", "load_context_history"}:
                recalls.append({"tool": calls[block["tool_use_id"]], "content": block.get("content", ""), "is_error": block.get("is_error", False)})
    archive_evidence = any(r["tool"] == "load_tool_output" and not r["is_error"] and all(m in str(r["content"]) for m in gold["fact_markers"]) for r in recalls)
    archive_ok = archive_evidence or not gold["requires_archive_evidence"]
    def is_summary(request):
        system = request.get("system")
        return isinstance(system, str) and system.startswith((
            SUMMARIZATION_SYSTEM_PROMPT.split("{summary_char_budget}", 1)[0],
            "你是编程助手的上下文摘要器，只生成供后续继续工作的结构化 Markdown 摘要。",
        ))
    main = [r for r in requests if not is_summary(r)]
    summaries = [r for r in requests if is_summary(r)]
    first_messages = main[0].get("messages", []) if main else []
    summary_blocks = [m for m in first_messages if "<context_summary" in str(m.get("content", ""))]
    tail = [m for m in first_messages if m not in summary_blocks]
    state = read_json(trial / "seed-state.json") if (trial / "seed-state.json").exists() else {}
    markers = [{"marker": m, "in_first_summary_wrapper": m in str(summary_blocks), "in_first_retained_view": m in str(tail),
                "in_seed_runtime_state": m in str(state), "in_recalled_output": m in str(recalls)} for m in gold["fact_markers"]]
    metrics = summarize(events, offline=spec["profile"]["mode"] == "offline")
    metrics["logical_usage_complete"] = metrics["usage_complete"]
    metrics.update(request_usage(requests, responses, offline=spec["profile"]["mode"] == "offline"))
    metrics["usage_completeness_basis"] = "sdk_forwarded_requests"
    context_events = [e for e in events if e["type"].startswith("context.")]
    projected = [e["payload"] for e in events if e["type"] == "context.request_projected"]
    parse_errors = event_errors + request_errors + response_errors + message_errors + blocked_errors
    if parse_errors:
        metrics["usage_complete"] = False
    legacy_blocked = {e.get("payload", {}).get("call_id") for e in events
                      if e["type"] == "model.failed" and e.get("payload", {}).get("error_type") == "EvaluationCallLimit"}
    blocked_count = len(blocked_records) if (trial / "model-blocked.jsonl").exists() else len(legacy_blocked)
    summary_ids = {r["request_index"] for r in summaries}
    truncated_summaries = sum(r.get("request_index") in summary_ids and r.get("stop_reason") == "max_tokens" for r in responses)
    successful_summaries = sum(e["type"] == "context.compacted" for e in events)
    fact_recall_index = next((i for i, recall in enumerate(recalls, 1)
                              if not recall["is_error"] and all(m in str(recall["content"]) for m in gold["fact_markers"])), None)
    metrics.update(
        api_requests=len(requests), summary_api_requests=len(summaries), main_api_requests=len(main),
        response_records=len(responses), evidence_parse_errors=parse_errors,
        locally_blocked_calls=blocked_count, summary_successes=successful_summaries,
        summary_output_limit_hits=truncated_summaries,
        first_recall_with_fact_markers=fact_recall_index,
        recalls_after_fact_markers=len(recalls) - fact_recall_index if fact_recall_index is not None else 0,
        first_main_request_chars=len(json.dumps({k: v for k, v in main[0].items() if k != "request_index"}, ensure_ascii=False)) if main else None,
        max_projected_request_chars=max((p.get("request_chars", 0) for p in projected), default=None),
        summary_revisions=max((p.get("summary_revision", 0) for p in projected), default=0),
        first_projection_placeholders=str(first_messages).count(TOOL_VIEW_MARKER) + str(first_messages).count(WRITE_VIEW_MARKER),
        recall_calls=len(recalls), archive_evidence_observed=archive_evidence,
        duration_ms=execution.get("worker_wall_ms"),
    )
    failures = []
    stop = str(execution.get("stop_reason", ""))
    if execution.get("execution_status") != "completed":
        if blocked_count or "EvaluationCallLimit" in str(execution) or "budget_exceeded:evaluation_api_calls" in stop:
            failures.append("api_call_limit")
        elif execution.get("execution_status") == "timeout":
            failures.append("timeout")
        elif stop.startswith("max_iterations"):
            failures.append("iteration_limit")
        else:
            failures.append("execution_failed")
    if not answer["passed"]:
        failures.append("answer_missing" if not (trial / "workspace/answer.json").exists() else
                        "answer_invalid" if "error" in answer else "answer_mismatch")
    for condition, code in ((not allowed, "unexpected_changes"), (not archive_ok, "archive_evidence_missing"),
                            (execution.get("canonical_prefix_unchanged") is not True, "canonical_changed"),
                            (bool(parse_errors), "evidence_invalid"), (bool(execution.get("checkpoint_error")), "checkpoint_failed")):
        if condition:
            failures.append(code)
    warnings = []
    if truncated_summaries:
        warnings.append(f"{truncated_summaries} 次摘要达到输出上限，未完整返回")
    if fact_recall_index is not None and fact_recall_index < len(recalls):
        warnings.append(f"第 {fact_recall_index} 次回读已出现全部核查标记，此后仍回读 {len(recalls) - fact_recall_index} 次（仅为字面证据，供排查）")
    return {"task_success": not failures, "grading_version": GRADING_VERSION,
            "failure_codes": failures, "failure_reasons": [REASONS[c] for c in failures], "diagnostic_notes": warnings,
            "quality_measurement": spec["profile"]["mode"] == "live",
            "case_id": spec["case_id"], "variant": spec["variant"], "repeat": spec["repeat"],
            "scale": spec["scale"], "execution": execution, "answer_grade": answer, "allowed_changes": allowed,
            "changed_files": changed, "archive_check_passed": archive_ok, "metrics": metrics,
            "fact_exposure_audit": markers, "context_events": context_events,
            "attribution_note": "Literal presence is a review aid, not causal attribution or a semantic summary-quality score."}
