"""Versioned C01 fixture and independent seed/gold validation."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from evals.evidence import read_json, write_json
from evals.verifier import FAIL_TO_PASS, PASS_TO_PASS

ASSETS = Path(__file__).parent / "assets" / "c01"
TARGET = "codeagent/tools/read.py"
BROKEN = "start = max(0, offset)"
CORRECT = "start = max(0, offset - 1)"
PROMPT = "read_file 在指定 offset 时返回的内容与行号不对应。请修复 codeagent/tools/read.py，使 offset 按 1 开始计数，并保持 limit、越界、空文件和错误路径行为正确。仅修改该文件。本次环境提供文件工具，不提供 shell；可阅读 README.md 中的行为示例，独立验收会在你结束后运行。"


def prepare(workspace: Path, *, gold: bool = False) -> None:
    shutil.copytree(ASSETS / "fixture", workspace)
    if gold:
        path = workspace / TARGET
        text = path.read_text(encoding="utf-8")
        if text.count(BROKEN) != 1:
            raise ValueError("Fixture seed drifted")
        path.write_text(text.replace(BROKEN, CORRECT), encoding="utf-8")


def grade(workspace: Path, output: Path, *, timeout: float = 30) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "SYSTEMDRIVE"}}
    command = [sys.executable, "-I", "-B", str(Path(__file__).with_name("verifier.py")), "--workspace", str(workspace.resolve()), "--output", str(output.resolve())]
    try:
        process = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        output.with_suffix(".stdout.txt").write_text(process.stdout, encoding="utf-8")
        output.with_suffix(".stderr.txt").write_text(process.stderr, encoding="utf-8")
        if process.returncode not in {0, 2} or not output.exists():
            result = {"passed": False, "checks": [], "invalid_reason": "verifier_process_failure", "returncode": process.returncode}
        else:
            result = read_json(output)
    except subprocess.TimeoutExpired:
        result = {"passed": False, "checks": [], "invalid_reason": "verifier_timeout"}
    write_json(output, result)
    return result


def validate(root: Path) -> dict:
    prepare(root / "seed")
    prepare(root / "gold", gold=True)
    seed = grade(root / "seed", root / "seed-grader.json")
    gold = grade(root / "gold", root / "gold-grader.json")
    outcomes = {check["name"]: check["passed"] for check in seed.get("checks", [])}
    valid = (not seed.get("invalid_reason") and gold.get("passed", False)
             and all(outcomes.get(name) is False for name in FAIL_TO_PASS)
             and all(outcomes.get(name) is True for name in PASS_TO_PASS))
    result = {"valid": valid, "task_id": "C01-read-offset", "seed_passed": seed.get("passed", False), "gold_passed": gold.get("passed", False), "empty_patch_passed": seed.get("passed", False), "fail_to_pass": FAIL_TO_PASS, "pass_to_pass": PASS_TO_PASS}
    write_json(root / "validation.json", result)
    return result
