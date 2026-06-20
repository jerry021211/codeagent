"""Markdown-backed long-term memory store."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from codeagent.memory.models import MEMORY_TYPES, MemoryRecord

INDEX_FILE_NAME = "MEMORY.md"
FRONTMATTER_RE = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.S)
SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class MemoryStore:
    """Persist and retrieve memory entries from a local markdown directory."""

    def __init__(
        self,
        root: Path | str = ".memory",
        *,
        max_memory_bytes: int = 50_000,
    ) -> None:
        self.root = Path(root)
        self.max_memory_bytes = max_memory_bytes

    def list_memories(self) -> list[MemoryRecord]:
        if not self.root.exists():
            return []
        records: list[MemoryRecord] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == INDEX_FILE_NAME:
                continue
            try:
                records.append(self._read_record(path))
            except ValueError:
                continue
        return sorted(records, key=lambda item: (item.memory_type, item.name.lower()))

    def remember(
        self,
        *,
        name: str,
        description: str,
        content: str,
        memory_type: str = "project",
        source: str = "manual",
    ) -> MemoryRecord:
        clean_name = self._clean_required("name", name)
        clean_description = self._clean_required("description", description)
        clean_content = self._clip_content(self._clean_required("content", content))
        clean_type = self._clean_type(memory_type)
        clean_source = source.strip() or "manual"
        now = _now_iso()

        existing = self._load_by_slug(self._slug(clean_name))
        created_at = existing.created_at if existing is not None else now
        record = MemoryRecord(
            name=clean_name,
            description=clean_description,
            content=clean_content,
            memory_type=clean_type,
            source=clean_source,
            created_at=created_at,
            updated_at=now,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._path_for_name(clean_name).write_text(
            self._serialize(record),
            encoding="utf-8",
        )
        self.rebuild_index()
        return record

    def load(self, name: str) -> MemoryRecord:
        slug = self._slug(self._clean_required("name", name))
        record = self._load_by_slug(slug)
        if record is None:
            raise KeyError(name)
        return record

    def search(self, query: str, *, max_items: int = 5) -> list[MemoryRecord]:
        records = self.list_memories()
        if not records:
            return []

        terms = _terms(query)
        if not terms:
            return records[:max_items]

        scored: list[tuple[int, MemoryRecord]] = []
        for record in records:
            haystack = " ".join(
                [record.name, record.description, record.memory_type, record.content]
            ).casefold()
            score = 0
            for term in terms:
                score += haystack.count(term)
                if term in record.name.casefold():
                    score += 3
                if term in record.description.casefold():
                    score += 2
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        return [record for _, record in scored[:max_items]]

    def catalog_prompt(self, *, max_items: int = 50) -> str:
        records = self.list_memories()[:max_items]
        if not records:
            return ""
        lines = ["Available memories:"]
        for record in records:
            lines.append(
                f"- {record.name} [{record.memory_type}]: {record.description}"
            )
        return "\n".join(lines)

    def rebuild_index(self) -> None:
        records = self.list_memories()
        self.root.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Memory Index",
            "",
            "This file is generated from markdown memory entries.",
            "",
        ]
        if not records:
            lines.append("_No memories stored yet._")
        else:
            for record in records:
                lines.append(
                    f"- **{record.name}** (`{record.memory_type}`): "
                    f"{record.description}"
                )
        (self.root / INDEX_FILE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def replace_all(self, records: list[MemoryRecord]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("*.md"):
            if path.name != INDEX_FILE_NAME:
                path.unlink()
        for record in records:
            self.remember(
                name=record.name,
                description=record.description,
                content=record.content,
                memory_type=record.memory_type,
                source=record.source,
            )
        self.rebuild_index()

    def _load_by_slug(self, slug: str) -> MemoryRecord | None:
        path = self.root / f"{slug}.md"
        if not path.exists() or not self._is_inside_root(path):
            return None
        return self._read_record(path)

    def _read_record(self, path: Path) -> MemoryRecord:
        if not self._is_inside_root(path):
            raise ValueError(f"Memory path escapes root: {path}")
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw)
        if not match:
            raise ValueError(f"Invalid memory file: {path}")
        meta = _parse_frontmatter(match.group("meta"))
        return MemoryRecord(
            name=meta.get("name") or path.stem,
            description=meta.get("description") or "",
            memory_type=self._clean_type(meta.get("type") or "project"),
            source=meta.get("source") or "manual",
            created_at=meta.get("created_at") or "",
            updated_at=meta.get("updated_at") or "",
            content=match.group("body").strip(),
        )

    def _path_for_name(self, name: str) -> Path:
        path = self.root / f"{self._slug(name)}.md"
        if not self._is_inside_root(path):
            raise ValueError(f"Invalid memory name: {name}")
        return path

    def _is_inside_root(self, path: Path) -> bool:
        root = self.root.resolve()
        target = path.resolve()
        return target == root or root in target.parents

    def _serialize(self, record: MemoryRecord) -> str:
        meta = {
            "name": record.name,
            "description": record.description,
            "type": record.memory_type,
            "source": record.source,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        lines = ["---"]
        lines.extend(f"{key}: {_quote_meta(value)}" for key, value in meta.items())
        lines.append("---")
        lines.append(record.content.strip())
        lines.append("")
        return "\n".join(lines)

    def _slug(self, value: str) -> str:
        slug = SLUG_RE.sub("-", value.strip()).strip(".-").lower()
        if not slug:
            raise ValueError("Memory name cannot be empty.")
        return slug[:80]

    def _clip_content(self, content: str) -> str:
        if len(content.encode("utf-8")) <= self.max_memory_bytes:
            return content
        clipped = content.encode("utf-8")[: self.max_memory_bytes]
        return clipped.decode("utf-8", errors="ignore").rstrip() + "\n\n[truncated]"

    @staticmethod
    def _clean_required(field: str, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError(f"Memory {field} is required.")
        return clean

    @staticmethod
    def _clean_type(memory_type: str) -> str:
        normalized = str(memory_type or "project").strip().lower()
        if normalized not in MEMORY_TYPES:
            return "project"
        return normalized


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _quote_meta(value: str) -> str:
    return str(value).replace("\n", " ").strip()


def _parse_frontmatter(raw: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta


def _terms(query: str) -> list[str]:
    return [term.casefold() for term in re.findall(r"[\w.-]+", query) if term.strip()]
