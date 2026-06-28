"""Prompt fragment assembly with budgets, cache, and trace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from codeagent.messages import Message
from codeagent.prompts.models import (
    PromptAssemblyResult,
    PromptBuildContext,
    PromptFragment,
    PromptSection,
    PromptTraceItem,
)

DYNAMIC_BOUNDARY = "<SYSTEM_PROMPT_DYNAMIC_BOUNDARY />"


class PromptAssembler:
    """Turn prompt fragments into a final system prompt."""

    def __init__(self) -> None:
        self._last_key = ""
        self._last_result: PromptAssemblyResult | None = None

    def assemble(
        self,
        fragments: Iterable[PromptFragment],
        context: PromptBuildContext,
    ) -> PromptAssemblyResult:
        unique = self._dedupe(fragments)
        key = self._cache_key(unique, context)
        if self._last_result is not None and key == self._last_key:
            return PromptAssemblyResult(
                system_prompt=self._last_result.system_prompt,
                system_sections=list(self._last_result.system_sections),
                reminder_messages=list(self._last_result.reminder_messages),
                fragments=list(self._last_result.fragments),
                trace=list(self._last_result.trace),
                prompt_hash=self._last_result.prompt_hash,
                cache_hit=True,
            )

        static, static_trace = self._build_section(
            unique,
            "static",
            context.config.static_budget_chars,
        )
        dynamic, dynamic_trace = self._build_section(
            unique,
            "dynamic",
            context.config.dynamic_budget_chars,
        )
        reminder, reminder_trace = self._build_section(
            unique,
            "reminder",
            context.config.system_budget_chars,
        )

        system_sections = []
        if static:
            system_sections.append(static)
        if dynamic:
            if static:
                system_sections.append(DYNAMIC_BOUNDARY)
            system_sections.append(dynamic)

        system_prompt = "\n\n".join(system_sections)
        system_prompt = _limit_text(system_prompt, context.config.system_budget_chars)
        reminder_messages = self._to_reminder_messages(reminder)
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
        result = PromptAssemblyResult(
            system_prompt=system_prompt,
            system_sections=system_sections,
            reminder_messages=reminder_messages,
            fragments=unique,
            trace=static_trace + dynamic_trace + reminder_trace,
            prompt_hash=prompt_hash,
            cache_hit=False,
        )
        self._last_key = key
        self._last_result = result
        return result

    def _dedupe(self, fragments: Iterable[PromptFragment]) -> list[PromptFragment]:
        by_id: dict[str, PromptFragment] = {}
        for fragment in fragments:
            if not fragment.content.strip():
                continue
            existing = by_id.get(fragment.id)
            if existing is None or fragment.priority >= existing.priority:
                by_id[fragment.id] = fragment
        return sorted(by_id.values(), key=lambda item: (item.section, item.priority, item.id))

    def _build_section(
        self,
        fragments: list[PromptFragment],
        section: PromptSection,
        budget: int,
    ) -> tuple[str, list[PromptTraceItem]]:
        selected = [fragment for fragment in fragments if fragment.section == section]
        selected.sort(key=lambda item: (item.priority, item.id))
        parts: list[str] = []
        trace: list[PromptTraceItem] = []
        remaining = max(0, budget)
        for fragment in selected:
            if remaining <= 0:
                break
            limit = remaining
            if fragment.budget_chars is not None:
                limit = min(limit, fragment.budget_chars)
            content, clipped = _clip(fragment.content.strip(), limit)
            if not content:
                continue
            parts.append(content)
            trace.append(
                PromptTraceItem(
                    id=fragment.id,
                    section=fragment.section,
                    source=fragment.source,
                    chars=len(content),
                    clipped=clipped,
                )
            )
            remaining -= len(content)
        return "\n\n".join(parts), trace

    def _to_reminder_messages(self, reminder: str) -> list[Message]:
        if not reminder:
            return []
        return [
            {
                "role": "user",
                "content": f"<system-reminder>\n{reminder}\n</system-reminder>",
            }
        ]

    def _cache_key(
        self,
        fragments: list[PromptFragment],
        context: PromptBuildContext,
    ) -> str:
        payload = {
            "mode": context.mode.value,
            "model": context.model,
            "workspace": str(context.workspace),
            "config": {
                "system_budget_chars": context.config.system_budget_chars,
                "static_budget_chars": context.config.static_budget_chars,
                "dynamic_budget_chars": context.config.dynamic_budget_chars,
            },
            "fragments": [
                {
                    "id": fragment.id,
                    "section": fragment.section,
                    "content": fragment.content,
                    "priority": fragment.priority,
                }
                for fragment in fragments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(value)
    if len(value) <= limit:
        return value, False
    suffix = "\n[truncated]"
    if limit <= len(suffix):
        return value[:limit], True
    return value[: limit - len(suffix)].rstrip() + suffix, True


def _limit_text(value: str, limit: int) -> str:
    clipped, _ = _clip(value, limit)
    return clipped
