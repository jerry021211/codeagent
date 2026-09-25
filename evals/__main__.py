from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.evidence import read_json, seal, verify_seal
from evals.runner import ROOT, new_experiment, run
from evals.task import validate


def main():
    parser = argparse.ArgumentParser(description="Retained C01 Agent evaluation evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    execution = commands.add_parser("run")
    execution.add_argument("--mode", choices=["offline", "live"], default="offline")
    execution.add_argument("--output", type=Path, default=ROOT / "eval-results")
    execution.add_argument("--timeout", type=float, default=300)
    execution.add_argument("--max-iterations", type=int, default=20)
    execution.add_argument("--max-tokens", type=int, default=4096)
    validation = commands.add_parser("validate")
    validation.add_argument("--output", type=Path, default=ROOT / "eval-results")
    check = commands.add_parser("verify")
    check.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        result = verify_seal(args.directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["valid"] else 1
    if args.command == "validate":
        path = new_experiment(args.output, "validate")
        result = validate(path)
        seal(path)
        print(json.dumps({"directory": str(path), **result}, ensure_ascii=False))
        return 0 if result["valid"] else 1
    path = run(mode=args.mode, output=args.output, timeout=args.timeout, max_iterations=args.max_iterations, max_tokens=args.max_tokens)
    result = read_json(path / "result.json")
    print(json.dumps({"directory": str(path), **result}, ensure_ascii=False, indent=2))
    return 0 if result.get("task_success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
