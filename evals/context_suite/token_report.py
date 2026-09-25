"""Account from retained SDK evidence, without turning missing usage into zero."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from evals.metrics import TOKEN_FIELDS, request_usage

KINDS = ("main", "context_summary")
ALL_FIELDS = (*TOKEN_FIELDS, "total_tokens")
# These fields were not required by every historical manifest. Compare recorded
# values (including one-sided presence), never invent today's defaults for them.
OPTIONAL_PAIR_FIELDS = ("wall_timeout_seconds", "sdk_retries", "recovery_retries", "summary_max_tokens", "max_total_tokens")


def _read_records(path):
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return values, all(isinstance(value, dict) for value in values)
    except (OSError, ValueError):
        return [], False


def _read_manifest(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _summary(request):
    from codeagent.context.manager import SUMMARIZATION_SYSTEM_PROMPT
    system = request.get("system")
    return isinstance(system, str) and system.startswith((
        SUMMARIZATION_SYSTEM_PROMPT.split("{summary_char_budget}", 1)[0],
        "你是编程助手的上下文摘要器，只生成供后续继续工作的结构化 Markdown 摘要。",
    ))


def _sum_known(values):
    return sum(values) if all(value is not None for value in values) else None


def _missing_identity_fields(value, prefix=""):
    if isinstance(value, dict):
        return [field for key, item in value.items()
                for field in _missing_identity_fields(item, f"{prefix}.{key}" if prefix else key)]
    return [prefix] if value is None else []


def _different_identity_fields(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        fields = []
        for key in sorted(left.keys() | right.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in left or key not in right:
                fields.append(path)
            else:
                fields.extend(_different_identity_fields(left[key], right[key], path))
        return fields
    return [] if left == right else [prefix]


def trial_tokens(root: Path, result: dict, mode: str) -> dict:
    """All four Messages fields must be explicit before a total is comparable."""
    evidence = Path(result.get("evidence_directory") or root / result["trial_directory"])
    requests, requests_ok = _read_records(evidence / "model-requests.jsonl")
    responses, responses_ok = _read_records(evidence / "model-responses.jsonl")
    manifest = _read_manifest(evidence / "manifest.json")
    valid = requests_ok and responses_ok
    audit = request_usage(requests, responses, offline=mode != "live") if valid else {}
    ids_valid = valid and audit.get("request_record_ids_valid", False)
    # Invalid/duplicate IDs must not silently overwrite one another in a dict.
    by_id = {response["request_index"]: response for response in responses} if ids_valid else {}
    all_reported = bool(audit.get("usage_complete"))
    kinds = {}
    missing = []
    for kind in KINDS:
        subset = [request for request in requests if ("context_summary" if _summary(request) else "main") == kind] if requests_ok else []
        values = {}
        for field in TOKEN_FIELDS:
            reported = []
            for request in subset:
                usage = by_id.get(request.get("request_index"), {}).get("usage")
                value = usage.get(field) if isinstance(usage, dict) else None
                if type(value) is not int or value < 0:
                    missing.append({"request_index": request.get("request_index"), "field": field})
                    value = None
                reported.append(value)
            values[field] = _sum_known(reported) if mode == "live" and ids_valid and requests else None
        complete = (mode == "live" and ids_valid and bool(requests)
                    and all(value is not None for value in values.values())
                    and all(not by_id.get(request.get("request_index"), {}).get("error_type") for request in subset))
        values["total_tokens"] = sum(values.values()) if complete else None
        kinds[kind] = {"requests": len(subset), "complete": complete, **values}
    complete = all_reported and not missing
    totals = {field: _sum_known([kinds[k][field] for k in KINDS]) for field in ALL_FIELDS}
    profile = manifest.get("profile", {})
    profile = profile if isinstance(profile, dict) else {}
    context = profile.get("context")
    common_context = ({key: value for key, value in context.items() if key not in {"mode", "tool_projection_enabled"}}
                      if isinstance(context, dict) else None)
    return {
        "case_id": result["case_id"], "variant": result["variant"], "repeat": result["repeat"],
        "task_success": result["task_success"], "trial_directory": result["trial_directory"],
        "complete": complete, "by_kind": kinds, "totals": totals,
        "missing_fields": missing, "request_records_valid": ids_valid,
        "basis": "provider_messages_four_disjoint_fields" if mode == "live" else "synthetic_offline_not_measurement",
        "pair_identity": {
            "seed_history_sha256": manifest.get("seed_history_sha256"),
            "engine_sha256": manifest.get("engine_sha256"),
            "scale": manifest.get("scale"), "model": profile.get("model"),
            "summary_model": profile.get("summary_model"),
            "provider_host": profile.get("provider_host"),
            "max_iterations": profile.get("max_iterations"), "max_tokens": profile.get("max_tokens"),
            "max_api_calls": profile.get("max_api_calls"),
            "common_context": common_context,
            "runtime_options": {key: profile[key] for key in OPTIONAL_PAIR_FIELDS if key in profile},
        },
        "unrecorded_optional_pair_fields": [key for key in OPTIONAL_PAIR_FIELDS if key not in profile],
    }


def _aggregate(trials):
    totals = {field: _sum_known([trial["totals"][field] for trial in trials]) for field in ALL_FIELDS}
    by_kind = {}
    for kind in KINDS:
        by_kind[kind] = {field: _sum_known([trial["by_kind"][kind][field] for trial in trials]) for field in ALL_FIELDS}
        by_kind[kind]["requests"] = sum(trial["by_kind"][kind]["requests"] for trial in trials)
    return {
        "trials": len(trials), "successes": sum(trial["task_success"] for trial in trials),
        "complete": all(trial["complete"] for trial in trials), "by_kind": by_kind, "totals": totals,
        "mean_per_trial": {field: value / len(trials) if value is not None else None for field, value in totals.items()},
    }


def build_token_report(root: Path, results: list[dict], mode: str) -> dict:
    trials = [trial_tokens(root, result, mode) for result in results]
    groups = [{"variant": variant, **_aggregate(subset)} for variant in "ABCD"
              if (subset := [trial for trial in trials if trial["variant"] == variant])]
    pairs = []
    for case_id, repeat in sorted({(t["case_id"], t["repeat"]) for t in trials}):
        subset = [t for t in trials if t["case_id"] == case_id and t["repeat"] == repeat and t["variant"] in {"A", "D"}]
        if not subset:
            continue
        counts = Counter(t["variant"] for t in subset)
        a = next((t for t in subset if t["variant"] == "A"), None)
        d = next((t for t in subset if t["variant"] == "D"), None)
        missing_identity = ([f"A.{field}" for field in _missing_identity_fields(a["pair_identity"])] if a else [])
        missing_identity += ([f"D.{field}" for field in _missing_identity_fields(d["pair_identity"])] if d else [])
        different_identity = _different_identity_fields(a["pair_identity"], d["pair_identity"]) if a and d else []
        unrecorded_optional = sorted(set(a["unrecorded_optional_pair_fields"]) & set(d["unrecorded_optional_pair_fields"])) if a and d else []
        reason = None
        if counts != {"A": 1, "D": 1}:
            reason = "A/D 不成对或存在重复记录"
        elif mode != "live":
            reason = "离线模拟没有真实 token 用量"
        elif not a["complete"] or not d["complete"]:
            reason = "用量字段或请求记录缺失，不能把缺失量当成 0"
        elif missing_identity:
            reason = "缺少关键对照字段，无法确认条件：" + "、".join(missing_identity)
        elif different_identity:
            reason = "原始历史、模型或运行配置不一致：" + "、".join(different_identity)
        a_total = a["totals"]["total_tokens"] if a else None
        d_total = d["totals"]["total_tokens"] if d else None
        comparable = reason is None
        reduction = (a_total - d_total) / a_total if comparable and a_total else None
        pairs.append({"case_id": case_id, "repeat": repeat,
                      "A_success": a["task_success"] if a else None, "D_success": d["task_success"] if d else None,
                      "A_total_tokens": a_total, "D_total_tokens": d_total,
                      "comparable": comparable, "reason": reason,
                      "unrecorded_optional_fields": unrecorded_optional,
                      "D_minus_A_total_tokens": d_total - a_total if comparable else None,
                      "reduction_fraction": reduction,
                      "percentage_note": "A 用量为 0，百分比无法计算" if comparable and not a_total else None})
    # Do not drop failed or incomplete trials to obtain a flattering aggregate.
    comparison = {"pairs": len(pairs), "comparable_pairs": sum(pair["comparable"] for pair in pairs),
                  "both_success_pairs": sum(pair["A_success"] is True and pair["D_success"] is True for pair in pairs),
                  "A_total_tokens": None, "D_total_tokens": None, "reduction_fraction": None}
    if pairs and all(pair["comparable"] for pair in pairs):
        a_total = sum(pair["A_total_tokens"] for pair in pairs)
        d_total = sum(pair["D_total_tokens"] for pair in pairs)
        comparison.update(A_total_tokens=a_total, D_total_tokens=d_total,
                          reduction_fraction=(a_total - d_total) / a_total if a_total else None)
    return {"version": "messages-token-report-v1", "mode": mode,
            "fields": list(TOKEN_FIELDS), "trials": trials, "groups": groups,
            "paired_A_D": pairs, "comparison": comparison}


def _number(value):
    if value is None:
        return "未知"
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


def _percentage(value):
    return "不计算" if value is None else f"{value * 100:.1f}%"


def token_report_lines(report: dict) -> list[str]:
    lines = ["", "**Token 用量（包括摘要及失败尝试）**", "",
             "按实际 SDK 返回的 Anthropic Messages 字段分列：普通输入 input_tokens、输出 output_tokens、"
             "缓存创建 cache_creation_input_tokens、缓存读取 cache_read_input_tokens。四项相加为本表总量；"
             "缓存两项与普通输入分开记录，不套用其他接口的 prompt_tokens 口径。",
             "这不是费用；不同模型和缓存类型的单价不同。任何字段缺失均显示未知，不用字符估算。"
             "未发生的摘要调用可记 0；离线模拟不显示真实 token 数。",
             "前表 usage 完整核查输入和输出记录；这里的四项用量完整还要求缓存字段明确返回，否则总量未知。", "",
             "| 组 | 调用类型 | 请求数 | 普通输入 | 输出 | 缓存创建 | 缓存读取 | 总 token |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for group in report["groups"]:
        for kind, label in (("main", "主模型"), ("context_summary", "摘要")):
            values = group["by_kind"][kind]
            lines.append(f'| {group["variant"]} | {label} | {values["requests"]} | ' + " | ".join(_number(values[field]) for field in ALL_FIELDS) + " |")
    lines += ["", "| 组 | 通过/试验 | 全部试验总 token | 平均每次试验 token | 四项用量完整 |",
              "|---|---:|---:|---:|---|"]
    for group in report["groups"]:
        lines.append(f'| {group["variant"]} | {group["successes"]}/{group["trials"]} | {_number(group["totals"]["total_tokens"])} | {_number(group["mean_per_trial"]["total_tokens"])} | {group["complete"]} |')
    if report["paired_A_D"]:
        lines += ["", "**同案例、同重复编号的 A/D 对照**", "",
                  "| 案例/重复 | A/D 通过 | A 总 token | D 总 token | D 比 A 减少 | 说明 |",
                  "|---|---|---:|---:|---:|---|"]
        for pair in report["paired_A_D"]:
            note = pair["reason"] or pair["percentage_note"] or ("两组均通过" if pair["A_success"] and pair["D_success"] else "含任务失败，减少不代表同等效果")
            if pair["unrecorded_optional_fields"]:
                note += "；双方旧记录未保存 " + "、".join(pair["unrecorded_optional_fields"]) + "，这些项未核对"
            lines.append(f'| {pair["case_id"]}/{pair["repeat"]} | {pair["A_success"]}/{pair["D_success"]} | {_number(pair["A_total_tokens"])} | {_number(pair["D_total_tokens"])} | {_percentage(pair["reduction_fraction"])} | {note} |')
        overall = report["comparison"]
        lines += ["", f'完整可比：{overall["comparable_pairs"]}/{overall["pairs"]} 对；两组均通过：{overall["both_success_pairs"]}/{overall["pairs"]} 对。'
                  f'全部 A/D 配对按总量计算的减少比例：{_percentage(overall["reduction_fraction"])}（正数减少，负数增加）。',
                  "只有全部配对记录可比时才给总体比例，不剔除失败试验；不要把少做任务造成的用量下降当作收益。"
                  "不同模型的 token 也不代表相同成本。单次结果不能说明稳定收益，固定历史续答不能代表整个编码任务。"]
    return lines
