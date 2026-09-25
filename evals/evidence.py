"""Evidence files are retained, versioned and checksummed; no automatic deletion."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.name != "checksums.json"}


def snapshot(source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("codeagent", "evals"):
        for path in sorted((source / name).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".txt"} and "__pycache__" not in path.parts:
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    shutil.copyfile(source / "pyproject.toml", destination / "pyproject.toml")
    files = hashes(destination)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"sha256": digest, "files": files}


def seal(root: Path) -> dict:
    values = hashes(root)
    write_json(root / "checksums.json", {"algorithm": "sha256", "files": values})
    return values


def verify_seal(root: Path) -> dict:
    expected = read_json(root / "checksums.json")["files"]
    actual = hashes(root)
    return {"valid": expected == actual, "changed": [p for p in expected if actual.get(p) != expected[p]], "added": sorted(set(actual) - set(expected))}
