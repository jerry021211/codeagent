"""C01 controller: freeze inputs, validate, execute, grade, retain and seal."""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from evals.evidence import hashes, read_json, seal, snapshot, write_json
from evals.metrics import summarize
from evals.task import ASSETS, TARGET, PROMPT, grade, prepare, validate
from evals.tracing import configure_tracing, tracing_settings

ROOT = Path(__file__).resolve().parents[1]


def new_experiment(output: Path, mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output.resolve() / f"{stamp}-C01-{mode}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def source_metadata():
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return proc.stdout.strip() if proc.returncode == 0 else None
    versions = {}
    for package in ("anthropic", "httpx", "mcp", "python-dotenv"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {"git_head": git("rev-parse", "HEAD"), "git_status": git("status", "--short"), "python": sys.version, "platform": platform.platform(), "dependencies": versions}


def run(*, mode="offline", output=ROOT / "eval-results", timeout=300, max_iterations=20, max_tokens=4096) -> Path:
    if timeout <= 0 or max_iterations < 1 or max_tokens < 1:
        raise ValueError("Budgets must be positive")
    experiment = new_experiment(Path(output), mode)
    engine = experiment / "engine-snapshot"
    frozen = snapshot(ROOT, engine)
    write_json(experiment / "source-manifest.json", {**source_metadata(), "engine_snapshot": frozen, "fixture_files": hashes(ASSETS / "fixture")})
    validation = validate(experiment / "validation")
    if not validation["valid"]:
        write_json(experiment / "result.json", {"invalid_reason": "fixture_validation_failed"})
        seal(experiment)
        return experiment

    child_env = dict(os.environ)
    values = {}
    # Load only selected credentials/config; never serialize secrets or dotenv.
    if mode == "live":
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env")
        for key in ("MODEL_ID", "SUMMARIZATION_MODEL_ID", "API_KEY", "ANTHROPIC_API_KEY", "BASE_URL", "ANTHROPIC_BASE_URL", "SUMMARIZATION_API_KEY"):
            if not child_env.get(key) and values.get(key):
                child_env[key] = values[key]
        child_env["API_KEY"] = child_env.get("API_KEY") or child_env.get("ANTHROPIC_API_KEY", "")
        child_env["BASE_URL"] = child_env.get("BASE_URL") or child_env.get("ANTHROPIC_BASE_URL", "")
    child_env = configure_tracing(child_env, mode=mode, dotenv=values)
    model = child_env.get("MODEL_ID", "") if mode == "live" else "offline-scripted-c01"
    profile = {
        "id": "web-task-file-only-c01-v1", "mode": mode, "model": model,
        "summary_model": child_env.get("SUMMARIZATION_MODEL_ID") or model,
        "provider_host": urlsplit(child_env.get("BASE_URL") or "https://api.anthropic.com").hostname if mode == "live" else None,
        "streaming": False, "planning_backend": "tasks", "memory_enabled": False,
        "skills_enabled": False, "mcp_enabled": False, "team_enabled": False, "subagent_enabled": False,
        "shell_enabled": False, "interactive_tools_enabled": False, "sdk_max_retries": 0,
        "recovery_max_retries": 1, "request_timeout_seconds": 60,
        "tracing": tracing_settings(child_env),
        "max_iterations": max_iterations, "max_tokens": max_tokens, "wall_timeout_seconds": timeout,
        "isolation": "WorkspaceGuard file tools; separate verifier process; not an OS sandbox",
    }
    trial = experiment / "trial-001"
    trial.mkdir()
    prepare(trial / "workspace")
    initial = hashes(trial / "workspace")
    manifest = {"task_id": "C01-read-offset", "trial_id": trial.name, "prompt": PROMPT, "profile": profile, "engine_sha256": frozen["sha256"], "initial_files": initial, "provenance": read_json(ASSETS / "provenance.json")}
    write_json(trial / "manifest.json", manifest)
    write_json(trial / "profile.json", profile)
    child_env.update(PYTHONPATH=str(engine), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", CODEAGENT_DATA_DIR=str(trial / "runtime"))
    # -P keeps the target's codeagent package from shadowing the frozen engine.
    command = [sys.executable, "-P", "-B", "-m", "evals.worker", "--trial", str(trial)]
    controller_start = time.monotonic()
    if mode == "live" and (not model or not child_env.get("API_KEY")):
        worker = {"execution_status": "setup_error", "error": "Missing MODEL_ID or API key", "duration_ms": None}
    else:
        with (trial / "stdout.txt").open("w", encoding="utf-8") as stdout, (trial / "stderr.txt").open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(command, cwd=trial / "workspace", env=child_env, stdout=stdout, stderr=stderr)
            try:
                code = proc.wait(timeout=timeout)
                result_path = trial / "worker-result.jsonl"
                worker = json.loads(result_path.read_text(encoding="utf-8").splitlines()[-1]) if result_path.exists() else {"execution_status": "worker_crash", "returncode": code, "duration_ms": None}
            except subprocess.TimeoutExpired:
                # No shell/subagents are exposed, so the worker has no tool child processes.
                proc.kill()
                proc.wait()
                worker = {"execution_status": "timeout", "duration_ms": None, "deadline_seconds": timeout}
    worker["worker_wall_ms"] = round((time.monotonic() - controller_start) * 1000, 3)
    write_json(trial / "execution.json", worker)
    final = hashes(trial / "workspace")
    changed = sorted(p for p in set(initial) | set(final) if initial.get(p) != final.get(p))
    allowed_changes = all(path == TARGET for path in changed)
    before = (ASSETS / "fixture" / TARGET).read_text(encoding="utf-8").splitlines(keepends=True)
    candidate = trial / "workspace" / TARGET
    after = candidate.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if candidate.is_file() else []
    (trial / "patch.diff").write_text("".join(difflib.unified_diff(before, after, fromfile="a/" + TARGET, tofile="b/" + TARGET)), encoding="utf-8")
    # Reconstruct the verifier input from original assets and only the allowed patch.
    verify_workspace = trial / "verification-workspace"
    prepare(verify_workspace)
    if candidate.is_file() and not candidate.is_symlink():
        shutil.copyfile(candidate, verify_workspace / TARGET)
    else:
        (verify_workspace / TARGET).write_text("raise RuntimeError('Candidate target missing')\n", encoding="utf-8")
    grading = grade(verify_workspace, trial / "grader.json")
    events_path = trial / "events.jsonl"
    events = []
    incomplete_lines = 0
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                incomplete_lines += 1
    metrics = summarize(events, offline=mode == "offline")
    invalid = "fixture_or_worker_setup_error" if worker["execution_status"] in {"setup_error", "worker_crash"} else grading.get("invalid_reason")
    result = {
        "task_id": "C01-read-offset", "mode": mode, "quality_measurement": mode == "live",
        "execution_status": worker["execution_status"], "invalid_reason": invalid,
        "task_success": not invalid and worker["execution_status"] == "completed" and grading["passed"] and allowed_changes,
        "artifact_passed": grading["passed"], "allowed_changes": allowed_changes, "changed_files": changed,
        "duration_ms": worker.get("duration_ms"), "iterations": worker.get("iterations"),
        "incomplete_event_lines": incomplete_lines, "metrics": metrics,
        "final_workspace_hashes": final,
    }
    write_json(trial / "result.json", result)
    write_json(experiment / "result.json", result)
    report = (
        f"# C01 evaluation evidence\n\nMode: **{mode}**. "
        + ("Scripted plumbing validation; NOT a model-quality score.\n\n" if mode == "offline" else "One live-model smoke trial; not an aggregate capability claim.\n\n")
        + f"- Model: `{model}`\n- Profile: `{profile['id']}`\n- Execution: `{result['execution_status']}`\n- Task passed: **{result['task_success']}**\n"
        + f"- External checks: {sum(c['passed'] for c in grading.get('checks', []))}/{len(grading.get('checks', []))}\n"
        + f"- Agent duration: {result['duration_ms']} ms\n- Logical model calls: {metrics['logical_model_calls']}\n- Usage complete: {metrics['usage_complete']}\n- Cost: unavailable (no verified pricing)\n"
        + "\nSeed/gold validation, frozen engine and fixture, task manifest, per-call requests/responses, events, resulting patch, SQLite runtime, independent grader and JSON results are retained. Provider reasoning blocks and secrets are omitted from public trace files.\n\n"
        + "The Agent has file and Task tools, no shell/subagents. This controlled profile does not measure shell/testing behavior and is not an OS sandbox. Checksums detect local changes; they are not an external timestamp or immutable signature.\n"
    )
    (experiment / "report.md").write_text(report, encoding="utf-8")
    seal(experiment)
    return experiment
