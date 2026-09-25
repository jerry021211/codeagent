from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.context_suite.materials import prepare
from evals.context_suite.runner import ROOT, run_rules, run_suite, validate_materials
from evals.context_suite.regrade import regrade
from evals.context_suite.plan import STEPS, describe_steps, prepare_kit, run_step, step_definitions
from evals.evidence import read_json, verify_seal


def main():
    parser = argparse.ArgumentParser(description="Context material preparation, rule checks and bounded A/B/C/D replays")
    commands = parser.add_subparsers(dest="command", required=True)
    kit = commands.add_parser("prepare-kit", help="Prepare every fixed, lifecycle and manual case without model calls")
    kit.add_argument("--output", type=Path, help="New directory; defaults to a unique directory in eval-results")
    commands.add_parser("steps", help="List the recommended steps and their model-call limits")
    step = commands.add_parser("step", help="Execute exactly one named evaluation step")
    step.add_argument("name", choices=list(STEPS))
    step.add_argument("--output", type=Path, default=ROOT / "eval-results")
    step.add_argument("--preview", action="store_true", help="Show settings only; never call a model")
    lifecycle = commands.add_parser("lifecycle", help="Offline rolling-summary and fresh-process recovery checks")
    lifecycle.add_argument("--output", type=Path, default=ROOT / "eval-results")
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--output", type=Path, required=True, help="New directory; never overwrites existing materials")
    preparation.add_argument("--scale", choices=["production", "stress"], default="production")
    for name in ("validate", "rules"):
        p = commands.add_parser(name)
        p.add_argument("--output", type=Path, default=ROOT / "eval-results")
    verification = commands.add_parser("verify")
    verification.add_argument("directory", type=Path)
    regrading = commands.add_parser("regrade", help="Recheck sealed evidence using current grading rules; no model calls")
    regrading.add_argument("directory", type=Path)
    regrading.add_argument("--output", type=Path, default=ROOT / "eval-results")
    execution = commands.add_parser("run")
    execution.add_argument("--mode", choices=["offline", "live"], default="offline")
    execution.add_argument("--scale", choices=["production", "stress"], default="production")
    execution.add_argument("--output", type=Path, default=ROOT / "eval-results")
    execution.add_argument("--cases", nargs="+", choices=[f"S{i:02d}" for i in range(1, 7)])
    execution.add_argument("--variants", nargs="+", choices=list("ABCD"), default=["A", "D"])
    execution.add_argument("--repeats", type=int, default=1)
    execution.add_argument("--timeout", type=float, default=180)
    execution.add_argument("--max-iterations", type=int, default=8)
    execution.add_argument("--max-tokens", type=int, default=2048)
    execution.add_argument("--max-total-tokens", type=int, default=0,
                           help="Per-trial cumulative token budget; 0 (default) disables this limit")
    execution.add_argument("--max-api-calls", type=int, default=12)
    execution.add_argument("--max-trials", type=int, default=24)
    execution.add_argument("--max-total-api-calls", type=int, default=288)
    execution.add_argument("--context-window-tokens", type=int, default=0)
    execution.add_argument("--summary-context-window-tokens", type=int, default=0)
    execution.add_argument("--order-seed", type=int, default=20260923)
    args = parser.parse_args()
    try:
        if args.command == "steps":
            print(describe_steps())
            return 0
        if args.command == "prepare-kit":
            root = prepare_kit(args.output)
            print(json.dumps({"directory": str(root), "prepared": True, "new_model_calls": 0}, ensure_ascii=False))
            return 0
        if args.command == "step" and args.preview:
            print(json.dumps(next(s for s in step_definitions() if s["id"] == args.name), ensure_ascii=False, indent=2))
            return 0
        if args.command == "prepare":
            index = prepare(args.output, scale=args.scale)
            print(json.dumps({"directory": str(args.output.resolve()), "cases": index}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            result = verify_seal(args.directory)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["valid"] else 1
        if args.command == "regrade":
            root = regrade(args.directory, output=args.output)
            result = read_json(root / "result.json")
            print(json.dumps({"directory": str(root), "regrade_completed": True, "all_passed": result["all_passed"], "new_model_calls": 0}, ensure_ascii=False))
            return 0
        if args.command == "step":
            root = run_step(args.name, output=args.output)
        elif args.command == "lifecycle":
            from evals.context_suite.lifecycle import run_lifecycle
            root = run_lifecycle(args.output)
        elif args.command == "run":
            root = run_suite(**{k: v for k, v in vars(args).items() if k != "command"})
        else:
            root = (run_rules if args.command == "rules" else validate_materials)(args.output)
        result = read_json(root / "result.json")
        passed = result.get("valid", result.get("all_passed", result.get("passed", False)))
        print(json.dumps({"directory": str(root), "valid": passed,
                          "quality_measurement": result.get("quality_measurement", False)}, ensure_ascii=False))
        return 0 if passed else 1
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
