"""Regrade sealed evidence without invoking a model or modifying the source."""
from __future__ import annotations

from pathlib import Path
import shutil

from evals.context_suite.grading import GRADING_VERSION, grade_trial
from evals.context_suite.runner import ROOT, new_directory, write_report
from evals.evidence import file_hash, hashes, read_json, seal, verify_seal, write_json


def regrade(source: Path, *, output: Path = ROOT / "eval-results") -> Path:
    source, output = source.resolve(), output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Regrade output must be outside the sealed source experiment.")
    if not (source / "checksums.json").is_file() or not verify_seal(source)["valid"]:
        raise ValueError("Source evidence checksum verification failed; source was not regraded.")
    suite = read_json(source / "suite.json")
    trials = sorted(p for p in source.glob("trial-*") if p.is_dir())
    if not trials or any(p.is_symlink() or not p.resolve().is_relative_to(source) for p in trials):
        raise ValueError("Invalid source trial directories")
    root = new_directory(output, "regrade")
    results, changes = [], []
    # Preserve the grader used for this reinterpretation, separately from the
    # original engine snapshot which remains untouched.
    for relative in ("evals/context_suite/grading.py", "evals/context_suite/runner.py", "evals/context_suite/regrade.py", "evals/context_suite/token_report.py", "evals/context_suite/cost_report.py", "evals/context_suite/pricing.json", "evals/metrics.py", "evals/evidence.py"):
        target = root / "grader-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for trial in trials:
        result = grade_trial(trial, read_json(trial / "execution.json"))
        result.update(trial_directory=trial.name, evidence_directory=str(trial))
        old = read_json(trial / "result.json")
        changes.append({"trial": trial.name, "old_success": old["task_success"], "new_success": result["task_success"],
                        "old_usage_complete": old["metrics"]["usage_complete"], "new_usage_complete": result["metrics"]["usage_complete"]})
        write_json(root / trial.name / "result.json", result)
        results.append(result)
    write_report(root, results, suite["mode"], suite["scale"])
    source_verified = verify_seal(source)["valid"]
    provenance = {"grading_version": GRADING_VERSION, "source_directory": str(source),
                  "source_checksums_sha256": file_hash(source / "checksums.json"),
                  "source_unchanged": source_verified, "new_model_calls": 0,
                  "grader_files": hashes(root / "grader-snapshot"), "changes": changes}
    write_json(root / "regrade-source.json", provenance)
    overall = read_json(root / "result.json")
    overall.update(regraded=True, new_model_calls=0, source_directory=str(source), source_unchanged=source_verified)
    write_json(root / "result.json", overall)
    report = root / "report.md"
    report.write_text("本报告使用新评分规则重新检查已有记录，**没有新增模型调用**。原实验与原评分保持不变。"
                      "这是事后评分修正，应与后续预先使用新规则的实验区分。\n\n"
                      f"原始证据目录：{source}\n\n" + report.read_text(encoding="utf-8"), encoding="utf-8")
    seal(root)
    if not source_verified:
        raise ValueError("Source changed during regrade; derived results must not be used.")
    return root
