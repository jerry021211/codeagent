"""Aggregate each logical call once; absent billing data never means free."""
from __future__ import annotations

from collections import Counter

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def request_usage(requests: list[dict], responses: list[dict], *, offline=False) -> dict:
    """Check SDK-forwarded requests, not logical attempts blocked before dispatch."""
    ids = [r.get("request_index") for r in requests]
    response_ids = [r.get("request_index") for r in responses]
    well_formed = all(type(i) is int and i > 0 for i in ids + response_ids)
    if not well_formed:
        return {"usage_complete": False, "usage_missing_request_indices": ids, "request_record_ids_valid": False}
    unique = len(ids) == len(set(ids)) and len(response_ids) == len(set(response_ids))
    by_id = {r["request_index"]: r for r in responses}
    missing = []
    for index in ids:
        response = by_id.get(index, {})
        usage = response.get("usage")
        if (response.get("error_type") or not isinstance(usage, dict)
                or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens"))):
            missing.append(index)
    valid_ids = unique and set(response_ids) <= set(ids)
    return {"usage_complete": bool(ids) and not offline and valid_ids and not missing,
            "usage_missing_request_indices": missing, "request_record_ids_valid": valid_ids}


def summarize(events: list[dict], *, offline: bool = False) -> dict:
    started, completed, usage, failures = {}, set(), {}, set()
    tools = Counter()
    for event in events:
        payload = event.get("payload", {})
        call_id = payload.get("call_id")
        kind = event["type"]
        if kind == "model.started" and call_id:
            started[call_id] = payload.get("call_kind", "main")
        elif kind == "model.completed" and call_id:
            completed.add(call_id)
        elif kind == "model.failed" and call_id:
            failures.add(call_id)
        elif kind == "usage.updated" and call_id:
            usage[call_id] = payload
        if kind.startswith("tool."):
            tools[kind] += 1
    by_kind = {}
    for call_id, item in usage.items():
        kind = started.get(call_id, item.get("call_kind", "main"))
        total = by_kind.setdefault(kind, {field: 0 for field in TOKEN_FIELDS})
        for field in TOKEN_FIELDS:
            total[field] += item.get(field, 0) or 0
    available = {key for key, value in usage.items() if value.get("available", False)}
    return {
        "logical_model_calls": len(started), "model_calls_by_kind": dict(Counter(started.values())),
        "model_failed_calls": len(failures), "tool_counts": dict(tools), "usage_by_kind": by_kind,
        "usage_complete": bool(started) and not failures and set(started) <= available and set(started) <= completed,
        "usage_source": "synthetic_offline" if offline else "provider_reported",
        "cost": None, "cost_note": "No verified price table; missing usage is not zero cost.",
    }
