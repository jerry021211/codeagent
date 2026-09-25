"""Reproducible, offline lifecycle checks; never evidence of model quality."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

from evals.evidence import read_json, seal, snapshot, write_json


CASES = {
    "R01": "连续压缩三次，关闭数据库，在新 Python 进程恢复后继续",
    "R02": "保存后修改旧消息，恢复时拒绝继续使用旧切点",
    "R03": "摘要被输出上限截断，保留上次成功的摘要与切点",
}

BOUNDARY = (
    "本报告只验证程序规则。模型回复由固定脚本提供，没有调用真实模型，不代表摘要质量或 token 节省。"
    "恢复测试从已成功保存的 SQLite checkpoint 开始，验证新 Python 进程中的 WebAgentFactory 恢复；"
    "没有测试 Web 服务强杀、未保存数据恢复或真实发布等外部副作用。"
)


def stage_messages(stage: int) -> list[dict]:
    """Tool calls/results remain paired, with stable IDs across generated runs."""
    facts = [
        "目标文件 reports/客户汇总.json；JSON；字段顺序 姓名、金额；UTF-8；禁止覆盖已有文件。",
        "更正金额规则：金额保留两位小数，旧要求整数作废；目标文件和其他规则保持不变。",
        "状态更新：单元测试已通过；集成测试失败；全量测试未执行；尚未发布。凭据 receipt-R03-2026。",
        "恢复后只整理下一步：修复集成测试后再跑全量测试。不要重复声称发布成功。",
    ]
    messages = [{"role": "user", "content": f"STAGE_{stage}_FACT: {facts[stage - 1]}"}]
    for index in range(12):
        identifier = f"stage_{stage}_read_{index:02d}"
        messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": identifier,
                "name": "read_file", "input": {"file_path": f"fixture/stage-{stage}-{index:02d}.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier,
                "content": f"STAGE_{stage}_EVIDENCE_{index:02d}: " + (f"历史检查记录 {stage}-{index}，状态未变。" * 100)}]},
        ])
    messages.append({"role": "assistant", "content": f"第 {stage} 阶段材料已检查，等待下一阶段。"})
    return messages


def prepare_lifecycle(output: Path) -> Path:
    """Write fixed inputs, expected rules and a manual model-quality protocol."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for case_id, title in CASES.items():
        target = output / case_id
        write_json(target / "case.json", {"id": case_id, "title": title, "mode": "offline_rules",
            "paid_model_calls": 0, "quality_claim_allowed": False})
        write_json(target / "stages.json", {"stages": [stage_messages(i) for i in range(1, 5)]})
        checks = {
            "R01": ["重启前成功压缩三次", "新进程恢复消息、摘要、切点和发送视图一致", "最早归档仍可回读", "继续执行后可生成第四次摘要"],
            "R02": ["已保存的旧消息发生变化", "恢复后的首次投影清空旧摘要与旧切点", "修改后的原始消息仍在请求视图中"],
            "R03": ["截断摘要被拒绝", "上次成功摘要、切点、前缀哈希与归档路径保持不变", "原始消息保持不变", "进入失败冷却"],
        }[case_id]
        write_json(target / "expected.json", {"checks": checks, "boundary": BOUNDARY})
        (target / "README.md").write_text(f"# {case_id}：{title}\n\n{BOUNDARY}\n\n" +
            "自动检查：\n\n" + "\n".join(f"- {item}" for item in checks) + "\n", encoding="utf-8")
    manual = output / "manual-model-quality"
    for stage in range(1, 5):
        messages = stage_messages(stage)
        write_json(manual / f"stage-{stage:02d}.json", {"messages": messages})
        text = "\n\n".join(str(m["content"]) if isinstance(m["content"], str)
                           else "\n".join(block.get("content", f"检查 {block.get('input', {}).get('file_path', '')}") for block in m["content"])
                           for m in messages)
        (manual / f"stage-{stage:02d}.txt").write_text(text + "\n", encoding="utf-8")
    write_json(manual / "expected-for-reviewer-only.json", {
        "target": "reports/客户汇总.json", "format": "JSON", "fields": ["姓名", "金额"], "encoding": "UTF-8",
        "overwrite": False, "amount_decimals": 2, "unit_tests": "passed", "integration_tests": "failed",
        "full_tests": "not_run", "published": False, "receipt": "receipt-R03-2026",
        "next_step": "修复集成测试，再跑全量测试", "superseded": "整数金额规则已作废",
    })
    (manual / "final-question.txt").write_text(
        "请根据整段对话回答：目标文件、格式、字段顺序、编码、覆盖限制、金额精度分别是什么？"
        "哪些测试已完成、哪些失败、哪些没跑？是否已经发布？凭据是什么？下一步做什么？"
        "如需历史回读请按实际归档读取，不要编造。\n", encoding="utf-8")
    (manual / "review.md").write_text(
        "# 多轮摘要质量人工复核\n\n这套材料还没有自动执行真实模型多轮评测。以下是人工复核步骤，需真实模型，会产生费用。\n\n"
        "1. 新建专用于评测的对话，记录模型、代码版本和上下文设置。\n"
        "2. 按顺序发送 stage-01.txt、stage-02.txt、stage-03.txt。每份发送完等待回复，然后请求一次压缩。"
        "必须在事件或状态中确认 summary_revision 比上次增加 1；仅收到“已压缩”文字不算成功。\n"
        "3. 保存三次摘要、每次请求的 usage 和消息记录。如果某次没有真正压缩，记为该次未触发，不要记作通过。\n"
        "4. 三次压缩全部完成并成功保存后，正常关闭服务，再启动并打开原对话；保存恢复日志。\n"
        "5. 发送 stage-04.txt，再发送 final-question.txt。不要把 expected-for-reviewer-only.json 发给模型。\n"
        "6. 对照 expected-for-reviewer-only.json 逐项检查；答错或遗漏即记录该项失败。保留模型原始回答。\n"
        "7. 比较 token 时另建不开摘要的对话，同样发送全部材料并回答同一道题；两边都累计所有回复和摘要的 usage。\n\n"
        "人工发送的材料是用户文本，自动规则集使用成对工具消息，两者不能合并成同一项质量分数。"
        "该人工检查也不等于强杀恢复或外部操作的幂等性测试。\n\n"
        "| 检查项 | 通过/失败 | 原始证据路径 |\n|---|---|---|\n"
        "| 三次摘要确实成功 | 待填写 | |\n| 正常保存后重启可恢复 | 待填写 | |\n"
        "| 最早的路径、字段、编码和禁止覆盖规则 | 待填写 | |\n| 最新金额规则覆盖旧规则 | 待填写 | |\n"
        "| 测试状态、发布状态与凭据 | 待填写 | |\n| 后续行动准确 | 待填写 | |\n",
        encoding="utf-8")
    (output / "README.md").write_text(
        "# 滚动摘要与进程恢复固定材料\n\n" + BOUNDARY + "\n\n" +
        "\n".join(f"- [{key}：{title}]({key}/README.md)" for key, title in CASES.items()) +
        "\n- [真实模型逐轮材料与人工复核](manual-model-quality/review.md)\n", encoding="utf-8")
    seal(output)
    return output


def run_lifecycle(output: Path) -> Path:
    """Create a sealed run directory beneath output; workers run frozen sources."""
    output = output.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = output / f"{stamp}-context-lifecycle-{uuid.uuid4().hex[:8]}"
    run.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    write_json(run / "source-manifest.json", snapshot(source, run / "engine-snapshot"))
    prepare_lifecycle(run / "materials")
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(run / "engine-snapshot"), PYTHONUTF8="1",
        LANGSMITH_TRACING="false", LANGCHAIN_TRACING="false", LANGCHAIN_TRACING_V2="false")
    results = []
    for case_id, title in CASES.items():
        case = run / case_id
        case.mkdir()
        phases = []
        for phase in ("prepare", "resume"):
            command = [sys.executable, "-m", "evals.context_suite.lifecycle_worker", "--case", str(case), "--phase", phase]
            try:
                completed = subprocess.run(command, cwd=run / "engine-snapshot", env=environment,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                (case / f"{phase}-stdout.txt").write_text(completed.stdout, encoding="utf-8")
                (case / f"{phase}-stderr.txt").write_text(completed.stderr, encoding="utf-8")
                phase_result = read_json(case / f"{phase}-result.json") if (case / f"{phase}-result.json").exists() else {}
                phases.append({**phase_result, "phase": phase, "returncode": completed.returncode,
                    "passed": completed.returncode == 0 and phase_result.get("passed") is True})
            except subprocess.TimeoutExpired as exc:
                for stream, content in (("stdout", exc.stdout), ("stderr", exc.stderr)):
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    (case / f"{phase}-{stream}.txt").write_text(content or "", encoding="utf-8")
                write_json(case / f"{phase}-error.json", {"error": "worker_timeout", "timeout_seconds": exc.timeout})
                phases.append({"phase": phase, "passed": False, "error": "worker_timeout"})
            except OSError as exc:
                error = {"error": "worker_start_failed", "detail": f"{type(exc).__name__}: {exc}"}
                write_json(case / f"{phase}-error.json", error)
                phases.append({"phase": phase, "passed": False, **error})
            if not phases[-1].get("passed"):
                break
        item = {"id": case_id, "title": title, "passed": len(phases) == 2 and all(p.get("passed") for p in phases), "phases": phases}
        write_json(case / "result.json", item)
        results.append(item)
    passed = all(r["passed"] for r in results)
    write_json(run / "result.json", {"suite": "context-lifecycle-v1", "mode": "offline_rules", "paid_model_calls": 0,
        "model_quality_measured": False, "quality_measurement": False, "boundary": BOUNDARY,
        "passed": passed, "all_passed": passed, "valid": passed, "cases": results})
    (run / "report.md").write_text("# 滚动摘要与进程恢复规则检查\n\n" + BOUNDARY +
        "\n\n真实模型调用：0。脚本回复不计真实 token。\n\n| 案例 | 检查 | 结果 | 证据 |\n|---|---|---|---|\n" +
        "\n".join(f"| {r['id']} | {r['title']} | {'通过' if r['passed'] else '失败，请看证据'} | [result.json]({r['id']}/result.json) |" for r in results) +
        "\n\n每个案例使用独立数据库；prepare 进程完成 checkpoint 并退出后，resume 进程通过 WebAgentFactory 恢复。"
        "两阶段保留进程 ID、规范消息、实际脚本请求、摘要状态、SQLite 和归档。\n", encoding="utf-8")
    seal(run)
    return run
