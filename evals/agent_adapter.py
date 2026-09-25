"""Reuse the Web composition root with an explicit file-only C01 profile."""
from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.config import EnvironmentConfig
from codeagent.context import ContextConfig
from codeagent.events.redaction import _is_sensitive_key, _SECRET_PATTERNS
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.memory import MemoryConfig
from codeagent.recovery import RecoveryConfig
from codeagent.runtime import RuntimeDataPaths
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools import tool_schema_hash
from codeagent.web.factory import WebAgentFactory
from evals.task import TARGET, BROKEN, CORRECT


def public_trace(value):
    """Keep request/response evidence without provider reasoning blocks or secrets."""
    if isinstance(value, list):
        return [public_trace(x) for x in value if not isinstance(x, dict) or x.get("type") not in {"thinking", "redacted_thinking"}]
    if isinstance(value, dict):
        return {k: public_trace(v) for k, v in value.items() if k not in {"thinking", "reasoning_content", "signature"}}
    return value


def append_record(path: Path, value):
    # Event payloads deliberately cap collections at 200 items/depth 8. Request
    # evidence must preserve the entire public structure, including long histories.
    secrets = tuple(v for k, v in os.environ.items() if len(v) >= 8 and _is_sensitive_key(k))

    def sanitize(item):
        if isinstance(item, dict):
            return {str(k): "[REDACTED]" if _is_sensitive_key(str(k)) else sanitize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [sanitize(v) for v in item]
        if hasattr(item, "model_dump"):
            return sanitize(item.model_dump(exclude_none=True))
        if item is None or isinstance(item, (bool, int, float)):
            return item
        text = str(item)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", text)
        return text

    sanitized = sanitize(public_trace(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sanitized, ensure_ascii=False, default=str) + "\n")
        handle.flush()


class ScriptedSDK:
    """Deterministic wiring smoke test; never a model-quality measurement."""

    def __init__(self):
        self.messages = self
        self.index = 0

    def create(self, **kwargs):
        self.index += 1
        if self.index == 1:
            content = [{"type": "tool_use", "id": "offline-read", "name": "read_file", "input": {"file_path": TARGET}}]
        elif self.index == 2:
            content = [{"type": "tool_use", "id": "offline-edit", "name": "edit_file", "input": {"file_path": TARGET, "old_string": BROKEN, "new_string": CORRECT}}]
        else:
            content = [{"type": "text", "text": "Offline scripted edit completed; external verifier determines correctness."}]
        return SimpleNamespace(content=content, stop_reason="tool_use" if self.index < 3 else "end_turn", usage=None)

    def close(self):
        pass

    def with_options(self, **kwargs):
        return self


class EvaluationCallLimit(ExecutionStopped):
    pass


class EvidenceSDK:
    def __init__(self, sdk, root: Path, *, max_calls: int | None = None, shared=None):
        self.sdk, self.root = sdk, root
        self.messages = self
        self.shared = shared if shared is not None else {"count": 0, "max_calls": max_calls, "clients": [sdk], "closed": False}

    @property
    def count(self):
        return self.shared["count"]

    def with_options(self, **kwargs):
        return EvidenceSDK(self.sdk.with_options(**kwargs), self.root, shared=self.shared)

    def for_client(self, sdk):
        """Record a separately authenticated summary client under the same cap."""
        self.shared["clients"].append(sdk)
        return EvidenceSDK(sdk, self.root, shared=self.shared)

    def create(self, **kwargs):
        return self._record_call(kwargs, lambda: self.sdk.messages.create(**kwargs))

    def post(self, path, *, body, cast_to, **kwargs):
        return self._record_call(body, lambda: self.sdk.post(path, body=body, cast_to=cast_to, **kwargs))

    def _record_call(self, payload, invoke):
        if self.shared["max_calls"] is not None and self.count >= self.shared["max_calls"]:
            append_record(self.root / "model-blocked.jsonl", {"reason": "evaluation_api_call_cap", "sent_requests": self.count,
                                                            "max_calls": self.shared["max_calls"]})
            raise EvaluationCallLimit("budget_exceeded:evaluation_api_calls")
        self.shared["count"] += 1
        index = self.count
        append_record(self.root / "model-requests.jsonl", {"request_index": index, **payload})
        try:
            response = invoke()
        except Exception as exc:
            append_record(self.root / "model-responses.jsonl", {"request_index": index, "error_type": type(exc).__name__, "error": str(exc)})
            raise
        content = [x.model_dump(exclude_none=True) if hasattr(x, "model_dump") else x for x in response.content]
        usage = getattr(response, "usage", None)
        append_record(self.root / "model-responses.jsonl", {"request_index": index, "provider_response_id": getattr(response, "id", None), "model": getattr(response, "model", None), "stop_reason": response.stop_reason, "content": content, "usage": usage.model_dump() if hasattr(usage, "model_dump") else usage})
        return response

    def close(self):
        if not self.shared["closed"]:
            self.shared["closed"] = True
            for sdk in self.shared["clients"]:
                sdk.close()


@dataclass(slots=True)
class EvaluationModelClient(AnthropicModelClient):
    summary_sdk_client: object | None = None

    def fork(self, **kwargs):
        client = AnthropicModelClient.fork(self, **kwargs)
        client.base_url = self.base_url
        if client.call_kind == "context_summary" and self.summary_sdk_client is not None:
            client.sdk_client = client._client = self.summary_sdk_client
        return client


def build_agent(profile: dict, workspace: Path, trial: Path, repository, emitter, cancellation, broker, *, scripted_sdk=None):
    env = EnvironmentConfig(
        model_id=profile["model"], api_key=os.getenv("API_KEY") if profile["mode"] == "live" else "offline-not-a-secret",
        base_url=os.getenv("BASE_URL") or None, stream=False,
        max_iterations=profile["max_iterations"], max_tokens=profile["max_tokens"], enable_skills=False,
        memory_config=MemoryConfig(enabled=False, auto_extract=False),
        loop_guard_config=LoopGuardConfig(max_total_tokens=profile.get("max_total_tokens", 300_000)),
        context_config=ContextConfig(**{**profile.get("context", {}), "summarization_model": profile["summary_model"]}),
        recovery_config=RecoveryConfig(max_retries=1, side_query_max_retries=0, max_continuations=0, escalated_max_tokens=profile["max_tokens"], fallback_model=""),
        data_dir=trial / "runtime", mcp_config_path=trial / "disabled-mcp.json", team_runtime_enabled=False,
    )
    factory = WebAgentFactory(env, workspace, repository, data_paths=RuntimeDataPaths(trial / "runtime"))
    agent = factory.create(event_emitter=emitter, cancellation=cancellation, permission_broker=broker)
    agent.client._client.close()
    if profile["mode"] == "offline":
        sdk = scripted_sdk if scripted_sdk is not None else ScriptedSDK()
    else:
        from anthropic import Anthropic
        sdk = Anthropic(api_key=env.api_key, base_url=env.base_url, max_retries=0, timeout=60.0)
    recorder = EvidenceSDK(sdk, trial, max_calls=profile.get("max_api_calls"))
    summary_recorder = None
    summary_key = os.getenv("SUMMARIZATION_API_KEY") if profile["mode"] == "live" else None
    if summary_key and summary_key != env.api_key:
        summary_recorder = recorder.for_client(Anthropic(api_key=summary_key, base_url=env.base_url, max_retries=0, timeout=60.0))
    # Keep config.summarization_api_key unset so Agent forks this recorded client
    # instead of constructing an unrecorded SDK with an independent call counter.
    agent.client = EvaluationModelClient(sdk_client=recorder, summary_sdk_client=summary_recorder,
                                        base_url=env.base_url, event_emitter=emitter, usage_tracker=agent.usage_tracker, stream=False)
    agent.context.summary_credentials_scope = hashlib.sha256(json.dumps([env.base_url, summary_key or env.api_key]).encode()).hexdigest()
    agent.allow_subagents = False
    agent.subagent_environment_factory = None
    if profile.get("suite") == "context-v1":
        allowed = {"read_file", "write_file", "edit_file", "grep", "glob", "load_tool_output", "load_context_history"}
        agent.tools = agent.tools.copy_without({s["name"] for s in agent.tools.schemas()} - allowed)
    else:
        agent.tools = agent.tools.copy_without({"bash", "ask_user", "subagent", "compact", "load_tool_output"})
    agent.context.state.tool_schema_hash = tool_schema_hash(agent.tools.schemas())

    def limit_edits(tool):
        if tool.name in {"write_file", "edit_file"}:
            allowed_paths = profile.get("allowed_writes", [TARGET])
            if factory.workspace_guard.resolve(tool.input.get("file_path", "")) not in {workspace / p for p in allowed_paths}:
                return "Blocked: evaluation permits modifying only " + ", ".join(allowed_paths)
        return None
    agent.hooks.register("PreToolUse", limit_edits, first=True)
    return agent, factory, recorder
