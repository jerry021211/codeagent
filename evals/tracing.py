"""Inherit configured tracing for live trials; keep scripted runs local."""
from __future__ import annotations

import os
from pathlib import Path

from codeagent.tracing import trace_run
from evals.evidence import write_json

_ALIASES = (
    ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"),
    ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
    ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"),
    ("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT"),
)
_OPTIONAL = (
    "LANGSMITH_WORKSPACE_ID", "LANGSMITH_TRACING_SAMPLING_RATE",
    "LANGSMITH_TRACE_MAX_STRING_CHARS", "LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS",
)


def configure_tracing(env: dict, *, mode: str, dotenv: dict | None = None) -> dict:
    """Return a child environment. Process settings take precedence across aliases."""
    result = dict(env)
    if mode != "live":
        result.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
        return result
    values = dotenv or {}
    for current, legacy in _ALIASES:
        value = env.get(current) or env.get(legacy) or values.get(current) or values.get(legacy)
        if value is not None:
            result[current] = value
            # Keep legacy and current switches consistent, including explicit false.
            result[legacy] = value
    for key in _OPTIONAL:
        if not result.get(key) and values.get(key):
            result[key] = values[key]
    return result


def tracing_settings(env: dict) -> dict:
    """Only non-sensitive settings are written to the experiment manifest."""
    enabled = any(str(env.get(k, "")).strip().lower() in {"1", "true", "yes", "on"}
                  for k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"))
    return {"enabled": enabled, "project": env.get("LANGSMITH_PROJECT") or env.get("LANGCHAIN_PROJECT") or "default"}


def flush_traces():
    from langsmith.run_trees import get_cached_client
    get_cached_client().flush(timeout=5.0)


def run_with_trace(agent, prompt: str, *, trial: Path, spec: dict):
    """Nest existing Agent/LLM/tool spans under one searchable evaluation root."""
    mode = spec["profile"]["mode"]
    if mode != "live":
        # Also protect direct worker invocations that bypass the controller.
        os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    settings = tracing_settings(dict(os.environ))
    enabled = mode == "live" and settings["enabled"]
    case = spec.get("case_id", spec.get("task_id", "C01"))
    name = f'eval.context.{case}.{spec["variant"]}.r{spec["repeat"]}' if "variant" in spec else f"eval.{case}"
    status = {**settings, "enabled": enabled, "trace_name": name, "root_created": False,
              "flush_status": "pending" if enabled else "disabled", "delivery_confirmed": False}
    path = trial / "tracing.json"
    write_json(path, status)
    try:
        if not enabled:
            return agent.run(prompt)
        metadata = {"evaluation": True, "suite_id": trial.parent.name, "trial_id": trial.name,
                    "case_id": case, "mode": mode, "engine_sha256": spec.get("engine_sha256"),
                    **{key: spec[key] for key in ("variant", "repeat", "scale") if key in spec}}
        with trace_run(name, inputs={"prompt": prompt}, metadata=metadata) as handle:
            run = getattr(handle, "_run", None)
            status["root_created"] = run is not None
            if run is not None:
                status["run_id"] = str(run.id)
            write_json(path, status)
            outcome = agent.run(prompt)
            handle.end(outputs={"stop_reason": outcome.stop_reason, "iterations": outcome.iterations})
            return outcome
    finally:
        if enabled:
            try:
                flush_traces()
                # SDK queue draining is not confirmation of server ingestion.
                status["flush_status"] = "returned"
            except Exception as exc:
                status["flush_status"] = "error"
                status["flush_error_type"] = type(exc).__name__
        write_json(path, status)
