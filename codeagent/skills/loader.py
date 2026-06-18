"""On-demand skill discovery and loading."""

from __future__ import annotations

from pathlib import Path

from codeagent.skills.models import LoadedSkill, SkillMetadata


class SkillLoader:
    """Scan SKILL.md files and load full skill content by registered name."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        *,
        max_skill_bytes: int = 50_000,
    ) -> None:
        self.roots = [Path(root) for root in (roots or [])]
        self.max_skill_bytes = max_skill_bytes
        self._skills: dict[str, LoadedSkill] = {}
        self.scan()

    def scan(self) -> list[SkillMetadata]:
        skills: dict[str, LoadedSkill] = {}
        for root in self.roots:
            if not root.exists() or not root.is_dir():
                continue

            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue

                manifest = directory / "SKILL.md"
                if not manifest.is_file():
                    continue

                loaded = self._load_manifest(manifest)
                if loaded.metadata.name in skills:
                    raise ValueError(f"Duplicate skill: {loaded.metadata.name}")
                skills[loaded.metadata.name] = loaded

        self._skills = skills
        return self.list_skills()

    def list_skills(self) -> list[SkillMetadata]:
        return [skill.metadata for skill in self._skills.values()]

    def catalog_prompt(self) -> str:
        skills = self.list_skills()
        if not skills:
            return ""

        lines = [
            "Available skills:",
            "Load a skill only when the user's task matches its description.",
        ]
        for skill in skills:
            lines.append(f"- {skill.name}: {skill.description}")
            if skill.when_to_use:
                lines.append(f"  Use when: {skill.when_to_use}")
        lines.append(
            "Use load_skill(name) to load full instructions before applying a matching skill."
        )
        return "\n".join(lines)

    def load(self, name: str) -> LoadedSkill:
        normalized_name = name.strip()
        if not normalized_name:
            raise KeyError(name)

        try:
            return self._skills[normalized_name]
        except KeyError:
            raise KeyError(name) from None

    def _load_manifest(self, manifest: Path) -> LoadedSkill:
        if manifest.stat().st_size > self.max_skill_bytes:
            raise ValueError(
                f"Skill is too large: {manifest} exceeds {self.max_skill_bytes} bytes"
            )

        content = manifest.read_text(encoding="utf-8")
        metadata = _metadata_from_content(content, manifest)
        return LoadedSkill(metadata=metadata, content=content)


def _metadata_from_content(content: str, manifest: Path) -> SkillMetadata:
    frontmatter, body = _split_frontmatter(content)
    name = frontmatter.get("name") or manifest.parent.name
    description = frontmatter.get("description") or _first_body_line(body)
    when_to_use = frontmatter.get("when_to_use", "")
    return SkillMetadata(
        name=name.strip(),
        description=description.strip(),
        when_to_use=when_to_use.strip(),
        path=manifest.resolve(),
    )


def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content

    meta_lines: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return _parse_frontmatter(meta_lines), "\n".join(lines[index + 1 :])
        meta_lines.append(line)

    return {}, content


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            metadata[key] = value
    return metadata


def _first_body_line(body: str) -> str:
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        return line.lstrip("#").strip()
    return "No description provided."
