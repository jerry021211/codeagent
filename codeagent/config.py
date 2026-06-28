"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from codeagent.agent import AgentConfig
from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextConfig
from codeagent.memory import MemoryConfig
from codeagent.prompts import PromptConfig


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

    @classmethod
    def from_env(cls) -> "EnvironmentConfig":
        _load_dotenv()
        return cls(
            model_id=_required_env("MODEL_ID"),
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
                mode=os.getenv("CONTEXT_COMPACT_MODE", "simple"),
                max_messages=_int_env("CONTEXT_MAX_MESSAGES", 50),
                keep_head_messages=_int_env("CONTEXT_KEEP_HEAD_MESSAGES", 3),
                keep_tail_messages=_int_env("CONTEXT_KEEP_TAIL_MESSAGES", 47),
                keep_recent_tool_results=_int_env(
                    "CONTEXT_KEEP_RECENT_TOOL_RESULTS", 3
                ),
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
                max_compact_failures=_int_env("CONTEXT_MAX_COMPACT_FAILURES", 3),
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
                context_summary_budget_chars=_int_env(
                    "CONTEXT_SUMMARY_BUDGET_CHARS", 12_000
                ),
                emit_trace=_bool_env("PROMPT_TRACE", False),
            ),
        )

    def to_agent_config(self) -> AgentConfig:
        return AgentConfig(
            model=self.model_id,
            system_prompt=self.system_prompt,
            max_tokens=self.max_tokens,
            max_iterations=self.max_iterations,
        )

    def create_anthropic_client(
        self,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AnthropicModelClient:
        return AnthropicModelClient(
            api_key=self.api_key,
            base_url=self.base_url,
            stream=self.stream if stream is None else stream,
            on_text=on_text,
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
