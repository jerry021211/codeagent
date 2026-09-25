"""Independent C01 grader. Run in a new Python process, never in the Agent."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import tempfile

FAIL_TO_PASS = ["default_read", "first_line", "middle_range", "last_line", "large_limit"]
PASS_TO_PASS = ["beyond_end", "empty", "missing", "directory", "negative_offset", "zero_offset"]


def verify(workspace: Path) -> dict:
    workspace = workspace.resolve()
    sys.path.insert(0, str(workspace))
    module = importlib.import_module("codeagent.tools.read")
    origin = Path(module.__file__).resolve()
    expected_origin = workspace / "codeagent/tools/read.py"
    if origin != expected_origin:
        raise RuntimeError("Verifier imported code outside the candidate workspace")
    checks = []
    with tempfile.TemporaryDirectory(prefix="c01-verifier-") as temporary:
        root = Path(temporary)
        sample = root / "sample.txt"
        sample.write_text("alpha\nbeta\n中文\ndelta\nepsilon\n", encoding="utf-8")
        empty = root / "empty.txt"
        empty.write_text("", encoding="utf-8")
        tool = module.ReadFileTool()
        cases = [
            ("default_read", lambda: tool.run(str(sample)), "1\talpha\n2\tbeta\n3\t中文\n4\tdelta\n5\tepsilon"),
            ("first_line", lambda: tool.run(str(sample), offset=1, limit=1), "1\talpha\n... (5 lines total, showing 1-1)"),
            ("middle_range", lambda: tool.run(str(sample), offset=3, limit=2), "3\t中文\n4\tdelta\n... (5 lines total, showing 3-4)"),
            ("last_line", lambda: tool.run(str(sample), offset=5, limit=1), "5\tepsilon"),
            ("large_limit", lambda: tool.run(str(sample), offset=4, limit=100), "4\tdelta\n5\tepsilon"),
            ("beyond_end", lambda: tool.run(str(sample), offset=20), "(empty file)"),
            ("empty", lambda: tool.run(str(empty)), "(empty file)"),
            ("missing", lambda: tool.run(str(root / "missing.txt")).startswith("Error:"), True),
            ("directory", lambda: "is a directory" in tool.run(str(root)), True),
            ("negative_offset", lambda: tool.run(str(sample), offset=-1, limit=1), "1\talpha\n... (5 lines total, showing 1-1)"),
            ("zero_offset", lambda: tool.run(str(sample), offset=0, limit=1), "1\talpha\n... (5 lines total, showing 1-1)"),
        ]
        for name, call, expected in cases:
            try:
                actual = call()
                passed = actual == expected
                error = None
            except Exception as exc:
                actual, passed, error = None, False, f"{type(exc).__name__}: {exc}"
            checks.append({"name": name, "group": "fail_to_pass" if name in FAIL_TO_PASS else "pass_to_pass", "passed": passed, "expected": expected, "actual": actual, "error": error})
    return {"grader_version": "c01-behavior-v1", "imported_module": str(origin), "checks": checks, "passed": all(c["passed"] for c in checks)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.workspace)
    except Exception as exc:
        # Broken candidate syntax/imports are task failures, not invalid trials.
        result = {"passed": False, "checks": [], "candidate_error": f"{type(exc).__name__}: {exc}"}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 2 if result.get("invalid_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
