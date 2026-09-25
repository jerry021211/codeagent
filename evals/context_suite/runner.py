"""Bounded, sequential fixed-history trials with retained evidence and no shell tool."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from evals.context_suite.grading import GRADING_VERSION, grade_answer, grade_trial, records
from evals.context_suite.materials import context_settings, definitions, material
from evals.evidence import hashes, seal, snapshot, write_json
from evals.runner import ROOT, source_metadata
from evals.tracing import configure_tracing, tracing_settings


def new_directory(output: Path, label: str):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = output.resolve() / f"{stamp}-context-{label}-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def validate_materials(output: Path):
    """Prove the grader rejects absent/incorrect answers, independently of an LLM."""
    root = new_directory(output, "validate")
    checks = []
    for case in definitions():
        for scale in ("production", "stress"):
            seed, gold = material(case, scale)
            path = root / f'{case["id"]}-{scale}.json'
            write_json(path, gold["expected"])
            positive = grade_answer(path, gold)["passed"]
            mutants = []
            for key in gold["expected"]:
                value = dict(gold["expected"])
                value.pop(key)
                write_json(path, value)
                mutants.append(not grade_answer(path, gold)["passed"])
                value[key] = "deliberately incorrect"
                write_json(path, value)
                mutants.append(not grade_answer(path, gold)["passed"])
            write_json(path, {})
            empty = not grade_answer(path, gold)["passed"]
            checks.append({"case_id": case["id"], "scale": scale, "messages": len(seed["messages"]),
                           "gold_passes": positive, "mutants_rejected": all(mutants), "mutants": len(mutants), "empty_rejected": empty})
    result = {"valid": all(c["gold_passes"] and c["mutants_rejected"] and c["empty_rejected"] for c in checks), "checks": checks}
    write_json(root / "result.json", result)
    seal(root)
    return root


def run_rules(output: Path):
    root = new_directory(output, "rules")
    groups = []
    for pattern in ("test_context*.py", "test_tool_output_paging.py", "test_history_observation.py",
                    "test_summary_character_budget.py", "test_eval*.py"):
        log = root / (pattern.replace("*", "all").replace(".py", ".txt"))
        with log.open("w", encoding="utf-8") as stream:
            command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"]
            try:
                proc = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=300)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
        text = log.read_text(encoding="utf-8", errors="replace")
        count = re.search(r"Ran (\d+) tests?", text)
        skipped = re.search(r"skipped=(\d+)", text)
        groups.append({"pattern": pattern, "returncode": code, "tests": int(count[1]) if count else None,
                       "skipped": int(skipped[1]) if skipped else 0, "log": log.name})
    write_json(root / "source-manifest.json", {**source_metadata(), "codeagent_files": hashes(ROOT / "codeagent"), "test_files": hashes(ROOT / "tests")})
    write_json(root / "result.json", {"valid": all(g["returncode"] == 0 for g in groups), "groups": groups})
    lines = ["# 上下文规则检查", "", "本地固定响应和程序断言，没有真实模型调用；通过不代表摘要语义质量。", "",
             "| 测试文件组 | 测试数 | 跳过数 | 结果 | 详细日志 |", "|---|---:|---:|---|---|"]
    for group in groups:
        label = "通过" if group["returncode"] == 0 else "超时" if group["returncode"] == "timeout" else "失败"
        lines.append(f'| {group["pattern"]} | {group["tests"]} | {group["skipped"]} | {label} | [日志]({group["log"]}) |')
    lines += ["", "有失败先打开对应日志查看 FAIL/ERROR；跳过项需单独核实原因，不计作已通过。"]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    seal(root)
    return root


def credentials(mode: str):
    env = dict(os.environ)
    values = {}
    if mode == "live":
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env")
        for key in ("MODEL_ID", "SUMMARIZATION_MODEL_ID", "API_KEY", "ANTHROPIC_API_KEY", "BASE_URL", "ANTHROPIC_BASE_URL", "SUMMARIZATION_API_KEY"):
            if not env.get(key) and values.get(key):
                env[key] = values[key]
        env["API_KEY"] = env.get("API_KEY") or env.get("ANTHROPIC_API_KEY", "")
        env["BASE_URL"] = env.get("BASE_URL") or env.get("ANTHROPIC_BASE_URL", "")
        if not env.get("MODEL_ID") or not env.get("API_KEY"):
            raise ValueError("Live mode requires MODEL_ID and API_KEY (or ANTHROPIC_API_KEY).")
    return configure_tracing(env, mode=mode, dotenv=values)


def run_suite(*, output=ROOT / "eval-results", mode="offline", scale="production", cases=None, variants=("A", "D"), repeats=1,
              timeout=180.0, max_iterations=8, max_tokens=2048, max_api_calls=12, max_trials=24,
              max_total_api_calls=288, max_total_tokens=0, context_window_tokens=0, summary_context_window_tokens=0, order_seed=20260923):
    selected = [c for c in definitions() if cases is None or c["id"] in cases]
    if not selected or cases is not None and set(cases) != {c["id"] for c in selected}:
        raise ValueError("Unknown or empty case selection")
    if len(set(variants)) != len(variants) or not variants or any(v not in "ABCD" or len(v) != 1 for v in variants):
        raise ValueError("Variants must be unique A/B/C/D")
    if min(repeats, timeout, max_iterations, max_tokens, max_api_calls, max_trials, max_total_api_calls) <= 0:
        raise ValueError("Budgets must be positive")
    if type(max_total_tokens) is not int or max_total_tokens < 0:
        raise ValueError("max_total_tokens must be a non-negative integer; 0 disables this limit")
    schedule = [(c, v, r) for r in range(1, repeats + 1) for c in selected for v in variants]
    if len(schedule) > max_trials or len(schedule) * max_api_calls > max_total_api_calls:
        raise ValueError("Planned trial/call upper bound exceeds --max-trials or --max-total-api-calls. Reduce the matrix or explicitly raise its cap.")
    if mode not in {"live", "offline"} or scale not in {"production", "stress"}:
        raise ValueError("Invalid mode or scale")
    # Validate windows before creating a paid trial or a source snapshot.
    from codeagent.context import ContextConfig
    ContextConfig(context_window_tokens=context_window_tokens, summary_context_window_tokens=summary_context_window_tokens)
    random.Random(order_seed).shuffle(schedule)
    env = credentials(mode)
    tracing = tracing_settings(env)
    print(f'LangSmith tracing: {"enabled" if tracing["enabled"] else "disabled"}; project={tracing["project"]}', flush=True)
    root = new_directory(Path(output), f"{mode}-{scale}")
    engine = root / "engine-snapshot"
    frozen = snapshot(ROOT, engine)
    write_json(root / "source-manifest.json", {**source_metadata(), "engine_snapshot": frozen})
    write_json(root / "suite.json", {"version": "context-v1", "mode": mode, "scale": scale,
                                     "order_seed": order_seed, "schedule": [{"case": c["id"], "variant": v, "repeat": r} for c, v, r in schedule],
                                     "planned_api_call_cap": len(schedule) * max_api_calls,
                                     "max_total_tokens_per_trial": max_total_tokens,
                                     "planned_wall_seconds_cap": len(schedule) * timeout, "quality_measurement": mode == "live"})
    results = []
    for index, (case, variant, repeat) in enumerate(schedule, 1):
        trial = root / f'trial-{index:03d}-{case["id"]}-{variant}-r{repeat}'
        trial.mkdir()
        seed, gold = material(case, scale)
        write_json(trial / "seed.json", seed)
        write_json(trial / "gold.json", gold)
        workspace = trial / "workspace"
        for relative, text in seed["workspace_files"].items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        settings = {**context_settings(variant, scale), "context_window_tokens": context_window_tokens,
                    "summary_context_window_tokens": summary_context_window_tokens}
        model = env["MODEL_ID"] if mode == "live" else "offline-context-script"
        profile = {"suite": "context-v1", "mode": mode, "model": model,
                   "summary_model": env.get("SUMMARIZATION_MODEL_ID") or model if mode == "live" else model,
                   "context": settings, "max_iterations": max_iterations, "max_tokens": max_tokens,
                   "max_total_tokens": max_total_tokens,
                   "max_api_calls": max_api_calls, "wall_timeout_seconds": timeout, "allowed_writes": ["answer.json"],
                   "provider_host": urlsplit(env.get("BASE_URL") or "https://api.anthropic.com").hostname if mode == "live" else None,
                   "memory": False, "skills": False, "mcp": False, "team": False, "shell": False,
                   "sdk_retries": 0, "recovery_retries": 1, "summary_max_tokens": 4000,
                   "tracing": tracing,
                   "separate_summary_credentials": bool(mode == "live" and env.get("SUMMARIZATION_API_KEY") and env["SUMMARIZATION_API_KEY"] != env.get("API_KEY")),
                   "isolation": "WorkspaceGuard + restricted tools; not an OS sandbox"}
        spec = {"case_id": case["id"], "variant": variant, "repeat": repeat, "scale": scale, "profile": profile,
                "engine_sha256": frozen["sha256"], "initial_files": hashes(workspace), "seed_history_sha256": seed["history_sha256"]}
        write_json(trial / "manifest.json", spec)
        child_env = {**env, "PYTHONPATH": str(engine), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                     "CODEAGENT_DATA_DIR": str(trial / "runtime")}
        command = [sys.executable, "-P", "-B", "-m", "evals.context_suite.worker", "--trial", str(trial)]
        start = time.monotonic()
        with (trial / "stdout.txt").open("w", encoding="utf-8") as stdout, (trial / "stderr.txt").open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(command, cwd=workspace, env=child_env, stdout=stdout, stderr=stderr)
            try:
                code = proc.wait(timeout=timeout)
                rows, bad = records(trial / "worker-result.jsonl")
                execution = rows[-1] if rows and not bad else {"execution_status": "worker_crash", "returncode": code}
                if code != 0:
                    execution = {**execution, "execution_status": "worker_crash", "returncode": code}
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                execution = {"execution_status": "timeout", "deadline_seconds": timeout}
        execution["worker_wall_ms"] = round((time.monotonic() - start) * 1000, 3)
        write_json(trial / "execution.json", execution)
        result = grade_trial(trial, execution)
        result["trial_directory"] = trial.name
        write_json(trial / "result.json", result)
        results.append(result)
        # A durable partial index survives a controller interruption.
        write_json(root / "partial-results.json", results)
        print(f'{index}/{len(schedule)} {case["id"]}/{variant}/r{repeat}: {execution["execution_status"]}, passed={result["task_success"]}', flush=True)
    write_report(root, results, mode, scale)
    seal(root)
    return root


def write_report(root, results, mode, scale):
    from evals.context_suite.token_report import build_token_report, token_report_lines
    from evals.context_suite.cost_report import build_cost_report, cost_report_lines

    token_accounting = build_token_report(Path(root), results, mode)
    cost_estimates = build_cost_report(token_accounting)
    token_limits = {t["pair_identity"]["runtime_options"].get("max_total_tokens") for t in token_accounting["trials"]}
    if token_limits == {0}:
        token_limit_label = "不设上限（max_total_tokens=0）"
    elif len(token_limits) == 1 and None not in token_limits:
        token_limit_label = f"{next(iter(token_limits)):,} token"
    else:
        token_limit_label = "未统一记录或各场不同，请查看各场 manifest.json；旧报告不推定为无上限"
    groups = []
    for variant in "ABCD":
        subset = [r for r in results if r["variant"] == variant]
        if not subset:
            continue
        successes = sum(r["task_success"] for r in subset)
        calls = sum(r["metrics"]["api_requests"] for r in subset)
        input_chars = [r["metrics"]["first_main_request_chars"] for r in subset if r["metrics"]["first_main_request_chars"] is not None]
        usage_by_kind = {}
        for result in subset:
            for kind, usage in result["metrics"]["usage_by_kind"].items():
                total = usage_by_kind.setdefault(kind, {})
                for field, value in usage.items():
                    total[field] = total.get(field, 0) + value
        groups.append({"variant": variant, "trials": len(subset), "successes": successes, "success_rate": successes / len(subset),
                       "api_calls": calls, "summary_calls": sum(r["metrics"]["summary_api_requests"] for r in subset),
                       "calls_per_success_including_failures": calls / successes if successes else None,
                       "mean_first_request_chars": round(statistics.mean(input_chars)) if input_chars else None,
                       "usage_by_kind": usage_by_kind,
                       "usage_complete": all(r["metrics"]["usage_complete"] for r in subset), "cost": None})
    paired = []
    for case_id, repeat in sorted({(r["case_id"], r["repeat"]) for r in results}):
        pair = {r["variant"]: r for r in results if r["case_id"] == case_id and r["repeat"] == repeat}
        if "A" in pair and "D" in pair:
            a, d = pair["A"], pair["D"]
            paired.append({"case_id": case_id, "repeat": repeat, "A_success": a["task_success"], "D_success": d["task_success"],
                           "D_minus_A_api_calls": d["metrics"]["api_requests"] - a["metrics"]["api_requests"],
                           "D_minus_A_worker_ms": d["metrics"]["duration_ms"] - a["metrics"]["duration_ms"]})
    report = {"mode": mode, "scale": scale, "grading_version": GRADING_VERSION, "quality_measurement": mode == "live", "all_passed": all(r["task_success"] for r in results),
              "trials": len(results), "groups": groups, "paired_A_D": paired, "results": results,
              "token_accounting": token_accounting, "cost_estimates": cost_estimates}
    write_json(root / "result.json", report)
    calls_label = "模拟调用总数" if mode == "offline" else "API 请求总数"
    summary_label = "模拟摘要调用" if mode == "offline" else "摘要请求"
    lines = ["# 上下文固定历史评测", "", f"模式：{mode}；规模：{scale}。",
             f"每场累计 token 预算：{token_limit_label}。请求次数、迭代次数、超时及单次请求限制仍独立生效。",
             "离线脚本注入答案，只验证链路，不代表摘要质量。" if mode == "offline" else "合成固定历史的受限续答结果，不能外推为真实编码任务整体成功率。", "",
             f"| 组 | 成功/试验 | {calls_label} | {summary_label} | 首次主请求平均字符 | usage 完整 |",
             "|---|---:|---:|---:|---:|---|"]
    for g in groups:
        lines.append(f'| {g["variant"]} | {g["successes"]}/{g["trials"]} | {g["api_calls"]} | {g["summary_calls"]} | {g["mean_first_request_chars"]} | {g["usage_complete"]} |')
    lines += token_report_lines(token_accounting)
    lines += cost_report_lines(cost_estimates)
    lines += ["", f"评分版本：{GRADING_VERSION}。S02/S03 的 format 接受 JSON 的大小写等价写法，其余字段保持严格校验。",
              "字符数不是 token 费用；估算费用见上表，未经账单核验的 cost 保持 null。SDK 请求包含已转交 SDK 的失败请求；本地拦截单独列出。",
              "usage 完整仅核查已转交 SDK 的请求是否都有有效用量，不要求任务成功；本地拦截不计作供应商缺失用量。", "",
              "| 案例/组/重复 | 结果与原因 | 摘要尝试/成功 | 本地拦截 | 回读 | 证据目录 |", "|---|---|---:|---:|---:|---|"]
    for r in results:
        m = r["metrics"]
        outcome = "通过" if r["task_success"] else "；".join(r["failure_reasons"])
        lines.append(f'| {r["case_id"]}/{r["variant"]}/{r["repeat"]} | {outcome} | {m["summary_api_requests"]}/{m["summary_successes"]} | {m["locally_blocked_calls"]} | {m["recall_calls"]} | [{r["trial_directory"]}]({r["trial_directory"]}/result.json) |')
    notes = [(r, r.get("diagnostic_notes", [])) for r in results if r.get("diagnostic_notes")]
    if notes:
        lines += ["", "排查提示：", ""]
        for r, items in notes:
            lines.append(f'- {r["case_id"]}/{r["variant"]}/{r["repeat"]}：' + "；".join(items) + "。")
    lines += ["", "每次试验保留未压缩历史、实际请求、响应、事件、最终消息、SQLite 检查点和独立答案评分。",
              "fact_exposure_audit 区分摘要、保留尾部、初始状态及回读中的字面命中；不能单凭命中推断模型的信息来源。",
              "S05 只测状态交接和重复操作的决策；没有执行真实发布，不能据此声称副作用幂等性已验证。"]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
