"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codeagent.agent import AgentConfig
from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextConfig
from codeagent.memory import MemoryConfig
from codeagent.planning import PlanningBackend
from codeagent.prompts import PromptConfig
from codeagent.recovery import RecoveryConfig


def _load_dotenv() -> None:
    """Load .env from cwd or parent directories without overriding env vars."""

    env_path = _find_dotenv()
    if env_path is None:
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(env_path)
        return

    load_dotenv(env_path, override=False)


def _find_dotenv() -> Path | None:
    current = Path.cwd().resolve()
    while True:
        candidate = current / ".env"
        if candidate.exists():
            return candidate
        if current == current.parent:
            return None
        current = current.parent


def _load_dotenv_fallback(env_path: Path) -> None:
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("'\"")
        if name and name not in os.environ:
            os.environ[name] = value


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    model_id: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 8000
    max_iterations: int = 50
    system_prompt: str = "You are a coding agent. Use tools to solve tasks." #初始化的prompt
    stream: bool = False
    enable_skills: bool = True
    skill_roots: tuple[Path, ...] = (Path(".skills"),)
    context_config: ContextConfig = field(default_factory=ContextConfig)
    memory_config: MemoryConfig = field(default_factory=MemoryConfig)
    prompt_config: PromptConfig = field(default_factory=PromptConfig)
    recovery_config: RecoveryConfig = field(default_factory=RecoveryConfig)
    planning_mode: PlanningBackend = PlanningBackend.AUTO

    @classmethod
    def from_env(cls) -> "EnvironmentConfig":
        _load_dotenv()
        model_id = _required_env("MODEL_ID")
        context_mode = os.getenv("CONTEXT_COMPACT_MODE", "model")
        summarization_model = (
            _required_env("SUMMARIZATION_MODEL_ID")
            if context_mode == "model"
            else ""
        )
        return cls(
            model_id=model_id,
            api_key=_first_optional_env("API_KEY", "ANTHROPIC_API_KEY"),
            base_url=_first_optional_env("BASE_URL", "ANTHROPIC_BASE_URL"),
            max_tokens=_int_env("MAX_TOKENS", 8000),
            max_iterations=_int_env("MAX_ITERATIONS", 50),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT",
                "You are a coding agent. Use tools to solve tasks.",
            ),
            stream=_bool_env("STREAMING", False),
            enable_skills=_bool_env("ENABLE_SKILLS", True),
            skill_roots=_path_list_env("SKILLS_DIR", (Path(".skills"),)),
            context_config=ContextConfig(
                mode=context_mode,
                summarization_model=summarization_model,
                summarization_api_key=_optional_env("SUMMARIZATION_API_KEY"),
                tool_result_budget_chars=_int_env(
                    "CONTEXT_TOOL_RESULT_BUDGET_CHARS", 200_000
                ),
                single_tool_output_max_chars=_int_env(
                    "CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS", 80_000
                ),
                compact_threshold_chars=_int_env(
                    "CONTEXT_COMPACT_THRESHOLD_CHARS", 300_000
                ),
                summary_max_chars=_int_env("CONTEXT_SUMMARY_MAX_CHARS", 12_000),
                transcript_dir=Path(
                    os.getenv("CONTEXT_TRANSCRIPT_DIR", ".transcripts")
                ),
                tool_output_dir=Path(
                    os.getenv(
                        "CONTEXT_TOOL_OUTPUT_DIR",
                        ".task_outputs/tool-results",
                    )
                ),
                reactive_retries=_int_env("CONTEXT_REACTIVE_RETRIES", 1),
            ),
            memory_config=MemoryConfig(
                enabled=_bool_env("ENABLE_MEMORY", True),
                memory_dir=Path(os.getenv("MEMORY_DIR", ".memory")),
                max_items_in_prompt=_int_env("MEMORY_MAX_ITEMS_IN_PROMPT", 50),
                max_loaded_items=_int_env("MEMORY_MAX_LOADED_ITEMS", 5),
                session_budget_chars=_int_env("MEMORY_SESSION_BUDGET_CHARS", 60_000),
                max_memory_bytes=_int_env("MEMORY_MAX_MEMORY_BYTES", 50_000),
                selection_mode=os.getenv("MEMORY_SELECTION_MODE", "llm"),
                auto_extract=_bool_env("MEMORY_AUTO_EXTRACT", False),
                extract_recent_messages=_int_env("MEMORY_EXTRACT_RECENT_MESSAGES", 12),
                consolidate_threshold=_int_env("MEMORY_CONSOLIDATE_THRESHOLD", 30),
                consolidate_mode=os.getenv("MEMORY_CONSOLIDATE_MODE", "simple"),
                allow_subagent_write=_bool_env("MEMORY_ALLOW_SUBAGENT_WRITE", False),
            ),
            prompt_config=PromptConfig(
                template_dir=_optional_path_env("PROMPT_TEMPLATE_DIR"),
                system_budget_chars=_int_env("SYSTEM_PROMPT_BUDGET_CHARS", 120_000),
                static_budget_chars=_int_env(
                    "SYSTEM_PROMPT_STATIC_BUDGET_CHARS", 50_000
                ),
                dynamic_budget_chars=_int_env(
                    "SYSTEM_PROMPT_DYNAMIC_BUDGET_CHARS", 70_000
                ),
                skill_catalog_budget_chars=_int_env(
                    "SKILL_CATALOG_BUDGET_CHARS", 12_000
                ),
                emit_trace=_bool_env("PROMPT_TRACE", False),
            ),
            recovery_config=RecoveryConfig(
                enabled=_bool_env("RECOVERY_ENABLED", True),
                max_retries=_int_env("RECOVERY_MAX_RETRIES", 10),
                base_delay_ms=_int_env("RECOVERY_BASE_DELAY_MS", 500),
                max_delay_ms=_int_env("RECOVERY_MAX_DELAY_MS", 32_000),
                jitter_ratio=_float_env("RECOVERY_JITTER_RATIO", 0.25),
                max_continuations=_int_env("RECOVERY_MAX_CONTINUATIONS", 3),
                escalated_max_tokens=_int_env(
                    "RECOVERY_ESCALATED_MAX_TOKENS", 64_000
                ),
                overload_fallback_after=_int_env(
                    "RECOVERY_OVERLOAD_FALLBACK_AFTER", 3
                ),
                fallback_model=os.getenv("FALLBACK_MODEL_ID", ""),
                side_query_max_retries=_int_env("RECOVERY_SIDE_QUERY_MAX_RETRIES", 2),
                trace=_bool_env("RECOVERY_TRACE", False),
            ),
            planning_mode=PlanningBackend.parse(
                os.getenv("CODEAGENT_PLANNING_MODE", "auto")
            ),
        )

    def to_agent_config(
        self,
        *,
        planning_backend: PlanningBackend | None = None,
    ) -> AgentConfig:
        return AgentConfig(
            model=self.model_id,
            system_prompt=self.system_prompt,
            max_tokens=self.max_tokens,
            max_iterations=self.max_iterations,
            planning_backend=planning_backend or self.planning_mode,
        )

    def create_anthropic_client(
        self,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        event_emitter: Any | None = None,
        usage_tracker: Any | None = None,
        call_kind: str = "main",
    ) -> AnthropicModelClient:
        return AnthropicModelClient(
            api_key=self.api_key,
            base_url=self.base_url,
            stream=self.stream if stream is None else stream,
            on_text=on_text,
            event_emitter=event_emitter,
            usage_tracker=usage_tracker,
            call_kind=call_kind,
        )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value or None


def _optional_path_env(name: str) -> Path | None:
    value = _optional_env(name)
    return Path(value) if value is not None else None


def _first_optional_env(*names: str) -> str | None:
    for name in names:
        value = _optional_env(name)
        if value is not None:
            return value
    return None


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {value}") from exc


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float, got: {value}") from exc


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if not value:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean, got: {value}")


def _path_list_env(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    value = os.getenv(name)
    if not value:
        return default
    paths = [Path(part.strip()) for part in value.split(os.pathsep) if part.strip()]
    return tuple(paths) if paths else default
