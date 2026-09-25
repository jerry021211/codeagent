"""Small, named steps for a reproducible evaluation campaign; never runs live implicitly."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shutil

from evals.context_suite.materials import definitions, prepare
from evals.context_suite.runner import ROOT, new_directory, run_rules, run_suite, validate_materials
from evals.evidence import file_hash, seal, write_json


def _suite(mode, scale, cases, variants, repeats):
    count = len(cases) * len(variants) * repeats
    return {"mode": mode, "scale": scale, "cases": cases, "variants": variants,
            "repeats": repeats, "timeout": 180, "max_iterations": 12, "max_tokens": 2048,
            "max_total_tokens": 0,
            "max_api_calls": 16, "max_trials": count, "max_total_api_calls": count * 16,
            "order_seed": 20260923}


ALL_CASES = [f"S{i:02d}" for i in range(1, 7)]
STEPS = {
    "01-check": {"name": "检查题目和评分器", "kind": "validate", "paid": False},
    "02-rules": {"name": "检查切点、水位、归档等程序规则", "kind": "rules", "paid": False},
    "03-rehearsal": {"name": "六题四组离线彩排", "kind": "suite", "paid": False,
                     "options": _suite("offline", "stress", ALL_CASES, list("ABCD"), 1)},
    "04-recovery": {"name": "连续压缩与新进程恢复规则", "kind": "lifecycle", "paid": False},
    "05-live-smoke": {"name": "真实模型试跑：需求变更与日志回读", "kind": "suite", "paid": True,
                      "options": _suite("live", "stress", ["S03", "S04"], ["A", "D"], 1)},
    "06-live-repeat": {"name": "S03 重复五次看稳定性", "kind": "suite", "paid": True,
                       "options": _suite("live", "stress", ["S03"], ["A", "D"], 5)},
    "07-live-cases": {"name": "六类题目各重复三次", "kind": "suite", "paid": True,
                      "options": _suite("live", "stress", ALL_CASES, ["A", "D"], 3)},
    "08-live-production": {"name": "默认生产阈值下的六题试跑", "kind": "suite", "paid": True,
                           "options": _suite("live", "production", ALL_CASES, ["A", "D"], 1)},
    "09-live-ablation": {"name": "四组比较：区分摘要和工具投影的贡献", "kind": "suite", "paid": True,
                         "options": _suite("live", "production", ALL_CASES, list("ABCD"), 2)},
}


def step_definitions():
    """Return copies so callers cannot change future runs by modifying a plan."""
    return [{"id": key, **deepcopy(value)} for key, value in STEPS.items()]


def describe_steps():
    lines = ["按需逐步执行，每次只运行一个步骤；前四步不调用模型。", ""]
    for step in step_definitions():
        options = step.get("options", {})
        calls = options.get("max_total_api_calls", 0) if step["paid"] else 0
        count = options.get("max_trials", "规则检查")
        lines.append(f'{step["id"]}  {step["name"]}；{"真实模型，可能产生费用" if step["paid"] else "免费本地检查"}；{count} 场；真实请求上限 {calls}')
    lines += ["", "示例：python -m evals.context_suite step 01-check",
              "先预览：python -m evals.context_suite step 06-live-repeat --preview",
              "手工三轮摘要与 Web 重启：参照 prepare-kit 生成的 manual/ 材料。"]
    return "\n".join(lines)


def run_step(name: str, *, output: Path = ROOT / "eval-results") -> Path:
    if name not in STEPS:
        raise ValueError(f"Unknown step: {name}")
    step = deepcopy(STEPS[name])
    print(f'{name}：{step["name"]}', flush=True)
    if step["paid"]:
        options = step["options"]
        print(f'将调用真实模型：{options["max_trials"]} 场，最多 {options["max_total_api_calls"]} 次请求；次数上限不是金额上限。', flush=True)
    if step["kind"] == "suite":
        return run_suite(output=output, **step["options"])
    if step["kind"] == "validate":
        return validate_materials(output)
    if step["kind"] == "rules":
        return run_rules(output)
    from evals.context_suite.lifecycle import run_lifecycle
    return run_lifecycle(output)


def prepare_kit(output: Path | None = None) -> Path:
    """Expand all inputs and human instructions without loading credentials."""
    from evals.context_suite.lifecycle import prepare_lifecycle
    from evals.context_suite.manual_materials import prepare_manual

    if output is None:
        root = new_directory(ROOT / "eval-results", "materials")
    else:
        root = output.resolve()
        root.mkdir(parents=True, exist_ok=False)
    for scale in ("stress", "production"):
        prepare(root / f"materials-{scale}", scale=scale)
    prepare_lifecycle(root / "lifecycle")
    prepare_manual(root / "manual")
    write_json(root / "steps.json", step_definitions())
    write_json(root / "coverage.json", {
        "scope": "单 Agent 上下文机制、固定历史续答、人工多轮质量；不覆盖完整真实编码任务或真实发布幂等性",
        "mechanisms": [
            {"mechanism": "短历史不误压缩", "cases": ["S01"], "steps": ["03-rehearsal", "07-live-cases"]},
            {"mechanism": "完整请求水位与硬预算", "tests": ["test_context_budget.py", "test_context_side_budget.py"], "steps": ["02-rules"]},
            {"mechanism": "完整回合切点与工具配对", "tests": ["test_context_runtime.py", "test_context_recovery_protocol.py"], "steps": ["02-rules"]},
            {"mechanism": "旧工具结果投影清理", "cases": ["S02"], "tests": ["test_context_projection.py"], "steps": ["02-rules", "09-live-ablation"]},
            {"mechanism": "摘要保留更正、状态和标识", "cases": ["S03", "S05", "S06"], "steps": ["06-live-repeat", "07-live-cases"]},
            {"mechanism": "大工具输出归档和分页回读", "cases": ["S04", "M03"], "tests": ["test_tool_output_paging.py"], "steps": ["02-rules", "05-live-smoke", "manual"]},
            {"mechanism": "连续多次摘要与旧摘要合并", "cases": ["R01", "M01"], "steps": ["04-recovery", "manual"]},
            {"mechanism": "正常保存后恢复与继续压缩", "cases": ["R01", "M02"], "steps": ["04-recovery", "manual"]},
            {"mechanism": "原始历史变化使旧切点失效", "cases": ["R02"], "steps": ["04-recovery"]},
            {"mechanism": "摘要失败保护和冷却", "cases": ["R03"], "tests": ["test_context_limits.py", "test_summary_character_budget.py"], "steps": ["02-rules", "04-recovery"]},
            {"mechanism": "用量记账、摘要开销和缺失值", "tests": ["test_context_token_report.py", "test_context_grading.py"], "steps": ["02-rules", "06-live-repeat", "07-live-cases"]},
        ],
    })
    write_json(root / "campaign-record-template.json", {
        "instructions": "复制此文件到材料包之外填写；不要修改封存结果。失败批次也登记。不要把此表或 gold 交给被测 Agent。",
        "code_revision": "", "main_model": "", "summary_model": "", "configuration_notes": "",
        "runs": [{"step": "", "experiment_directory": "", "source_sha256": "", "all_trials": None,
                  "A_passes": None, "D_passes": None, "D_summary_attempts": None, "D_summary_successes": None,
                  "A_total_tokens": None, "D_total_tokens": None, "usage_complete": None,
                  "token_reduction_percent": None, "failures_and_explanations": ""}],
        "manual_cases": [{"case_id": "", "record_file": "", "passed": None}],
        "limitations": "合成固定历史；重复次数少；token 不是费用；离线结果不代表模型质量。",
    })
    guide = ROOT / "docs/context-evaluation-walkthrough.md"
    if guide.exists():
        shutil.copyfile(guide, root / "一步一步操作.md")
    (root / "README.md").write_text(
        "# 上下文评测完整材料包\n\n"
        "先打开 [一步一步操作](一步一步操作.md)，一次只执行其中一个步骤。\n\n"
        "- materials-stress/：六题短材料，调低软阈值，便于先验证。\n"
        "- materials-production/：相同六题的长材料，使用生产默认阈值。\n"
        "- lifecycle/：连续摘要、进程恢复、失效切点及失败回退的规则题。\n"
        "- manual/：真实模型连续会话和 Web 重启的逐轮输入、答案和操作。\n"
        "- steps.json：每一步的固定参数；用 step 命令启动时会从当前代码配方生成材料。\n"
        "- coverage.json：每个上下文机制由哪道题、哪个规则测试覆盖。\n"
        "- campaign-record-template.json：复制到包外，登记每次实验和失败。\n\n"
        "本命令只准备材料，没有运行模型，材料本身不代表测试通过。"
        "gold.json、期望答案和评分表留给评测者；只把指定 workspace 和逐轮问题交给 Agent。\n\n"
        "修改展开文件不会改变自动评测题目；自动评测读取 evals/context_suite/cases.json 和材料生成器。"
        "每次 run 会冻结当前代码，但不同批次之间可能不同，正式对比期间请勿改模型、代码或参数。\n",
        encoding="utf-8",
    )
    sources = ["evals/context_suite/cases.json", "evals/context_suite/materials.py", "evals/context_suite/plan.py",
               "evals/context_suite/lifecycle.py", "evals/context_suite/manual_materials.py"]
    write_json(root / "kit-manifest.json", {"version": "context-kit-v2", "created_at": datetime.now(timezone.utc).isoformat(),
              "model_calls": 0, "fixed_cases": [c["id"] for c in definitions()],
              "source_files": {name: file_hash(ROOT / name) for name in sources}})
    seal(root)
    return root
