"""Memory maintenance and model-assisted extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeagent.memory.models import MEMORY_TYPES, MemoryConfig, MemoryRecord
from codeagent.memory.store import MemoryStore
from codeagent.messages import Message, extract_text


class MemoryManager:
    """Coordinates memory prompt exposure and optional maintenance."""

    def __init__(self, store: MemoryStore, config: MemoryConfig | None = None) -> None:
        self.store = store
        self.config = config or MemoryConfig()

    def catalog_prompt(self) -> str:
        return self.store.catalog_prompt(max_items=self.config.max_items_in_prompt)

    def after_turn(
        self,
        messages: list[Message],
        *,
        client: Any | None,
        model: str,
        max_tokens: int,
    ) -> None:
        if not self.config.enabled:
            return
        if self.config.auto_extract and client is not None:
            self.extract_from_recent_messages(
                messages,
                client=client,
                model=model,
                max_tokens=max_tokens,
            )
        self.consolidate_if_needed(client=client, model=model, max_tokens=max_tokens)

    def extract_from_recent_messages(
        self,
        messages: list[Message],
        *,
        client: Any,
        model: str,
        max_tokens: int,
    ) -> list[MemoryRecord]:
        recent = messages[-self.config.extract_recent_messages :]
        prompt = _memory_extraction_prompt(recent)
        response = client.create_message(
            model=model,
            system=(
                "Extract only durable, future-useful memories. "
                "Return strict JSON and no prose."
            ),
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=min(max_tokens, 1200),
        )
        records = self._records_from_json_text(extract_text(response.content))
        saved: list[MemoryRecord] = []
        for record in records:
            if self._looks_duplicate(record):
                continue
            saved.append(
                self.store.remember(
                    name=record.name,
                    description=record.description,
                    content=record.content,
                    memory_type=record.memory_type,
                    source="auto",
                )
            )
        return saved

    def consolidate_if_needed(
        self,
        *,
        client: Any | None,
        model: str,
        max_tokens: int,
    ) -> None:
        records = self.store.list_memories()
        if len(records) < self.config.consolidate_threshold:
            self.store.rebuild_index()
            return

        if self._lock_exists():
            self.store.rebuild_index()
            return

        self._write_lock()
        try:
            if self.config.consolidate_mode != "model" or client is None:
                self.store.rebuild_index()
                return
            consolidated = self._model_consolidate(
                records,
                client=client,
                model=model,
                max_tokens=max_tokens,
            )
            if consolidated:
                self.store.replace_all(consolidated)
            else:
                self.store.rebuild_index()
        finally:
            self._remove_lock()

    def _model_consolidate(
        self,
        records: list[MemoryRecord],
        *,
        client: Any,
        model: str,
        max_tokens: int,
    ) -> list[MemoryRecord]:
        prompt = _memory_consolidation_prompt(records)
        response = client.create_message(
            model=model,
            system=(
                "Consolidate memories without losing actionable facts. "
                "Return strict JSON and no prose."
            ),
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=min(max_tokens, 2000),
        )
        return self._records_from_json_text(extract_text(response.content))

    def _records_from_json_text(self, text: str) -> list[MemoryRecord]:
        payload = _parse_json_array(text)
        records: list[MemoryRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
            content = str(item.get("content") or "").strip()
            memory_type = str(item.get("type") or item.get("memory_type") or "project")
            if not name or not description or not content:
                continue
            records.append(
                MemoryRecord(
                    name=name,
                    description=description,
                    content=content,
                    memory_type=memory_type if memory_type in MEMORY_TYPES else "project",
                    source="auto",
                )
            )
        return records

    def _looks_duplicate(self, candidate: MemoryRecord) -> bool:
        existing = self.store.search(candidate.name, max_items=3)
        candidate_key = candidate.description.casefold()
        return any(record.description.casefold() == candidate_key for record in existing)

    def _lock_path(self) -> Path:
        return self.store.root / ".consolidate-lock"

    def _lock_exists(self) -> bool:
        return self._lock_path().exists()

    def _write_lock(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)
        self._lock_path().write_text("locked\n", encoding="utf-8")

    def _remove_lock(self) -> None:
        try:
            self._lock_path().unlink()
        except FileNotFoundError:
            pass


def _memory_extraction_prompt(messages: list[Message]) -> str:
    return (
        "Review the recent conversation and extract durable memories only.\n"
        "Good memories include stable user preferences, project conventions, "
        "important decisions, recurring feedback, and reusable facts.\n"
        "Do not store temporary task status, secrets, full command output, or "
        "large code snippets.\n"
        "Return a JSON array. Each object must have name, type, description, "
        "and content. Valid type values: user, feedback, project, reference.\n\n"
        f"Recent messages:\n{json.dumps(messages, ensure_ascii=False, default=str)}"
    )


def _memory_consolidation_prompt(records: list[MemoryRecord]) -> str:
    payload = [
        {
            "name": record.name,
            "type": record.memory_type,
            "description": record.description,
            "content": record.clipped_content(4000),
        }
        for record in records
    ]
    return (
        "Merge duplicate or overlapping memories while preserving useful facts.\n"
        "Return a JSON array using objects with name, type, description, and content.\n"
        "Keep memories short and specific.\n\n"
        f"Current memories:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_json_array(text: str) -> list[Any]:
    clean = text.strip()
    if not clean:
        return []
    if clean.startswith("```"):
        clean = clean.strip("`")
        if "\n" in clean:
            clean = clean.split("\n", 1)[1].strip()
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("[")
        end = clean.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            payload = json.loads(clean[start : end + 1])
        except json.JSONDecodeError:
            return []
    return payload if isinstance(payload, list) else []
