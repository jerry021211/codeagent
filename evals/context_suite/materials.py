"""Generate identical, reviewable inputs for every ablation from small recipes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from codeagent.context import ContextConfig
from codeagent.context.history import history_hash
from codeagent.messages import validate_tool_history
from evals.evidence import read_json, write_json

CASES = Path(__file__).with_name("cases.json")
VARIANTS = {
    "A": {"mode": "off", "tool_projection_enabled": False},
    "B": {"mode": "off", "tool_projection_enabled": True},
    "C": {"mode": "model", "tool_projection_enabled": False},
    "D": {"mode": "model", "tool_projection_enabled": True},
}


def definitions():
    return read_json(CASES)


def context_settings(variant: str, scale: str):
    settings = dict(VARIANTS[variant])
    if scale == "stress":
        settings["compact_threshold_chars"] = 40_000
    ContextConfig(**settings)  # Fail early if the real configuration rejects it.
    return settings


def filler(index: int, length: int):
    line = f"样例检查 {index:02d}：候选记录已逐项浏览；本条仅为合成背景，不新增需求、结论或授权。\n"
    return (line * (length // len(line) + 1))[:length]


def tool_round(messages, index, name, arguments, result):
    call_id = f"seed_{index:03d}"
    messages.extend([
        {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}]},
    ])


def material(case: dict, scale: str):
    """Gold is returned separately and must never be copied into the workspace."""
    recipe = case["recipe"]
    messages = [{"role": "user", "content": "请调查报告任务并保留后续交接所需的有效约定。"}]
    workspace = {"README.txt": "这是合成上下文评测工作区。最终交付 answer.json，不需要执行命令。\n"}
    ingress = []
    if recipe == "search":
        for i in range(18):
            text = (case["facts"][0] + "\n" if i == 0 else "") + filler(i, 19_000 if scale == "production" else 9_000)
            path = f"evidence/search-{i:02d}.txt"
            workspace[path] = text
            tool_round(messages, i, "grep", {"pattern": "报告", "path": path}, text)
        messages.append({"role": "assistant", "content": "阶段调查结束，待交接。"})
    else:
        for fact in case["facts"]:
            messages.extend([{"role": "assistant", "content": fact}, {"role": "user", "content": "记录这一阶段的信息。"}])
        if recipe == "archive":
            lines = [f"audit line {i:04d}: " + "normal record; " * 8 for i in range(1, 2401)]
            lines[1200] = "fault_code=E_REPLAY_731 record_id=ROW-4829 retryable=false"
            raw = "\n".join(lines)
            tool_round(messages, 0, "bash", {"command": "synthetic_read_only_audit"}, "__INGRESS_0__")
            ingress.append({"message_index": len(messages) - 1, "tool_use": {"id": "seed_000", "name": "bash", "input": {"command": "synthetic_read_only_audit"}}, "raw_output": raw})
        messages.append({"role": "assistant", "content": "本阶段已记录。"})
        if recipe != "short":
            for i in range(18):
                messages.extend([
                    {"role": "user", "content": f"继续浏览第 {i + 1} 组背景，不改变既有约定。"},
                    {"role": "assistant", "content": filler(i, 19_000 if scale == "production" else 3_500)},
                ])
    validate_tool_history(messages)
    prompt = (
        "继续上述任务。" + case["question"] + "\n"
        "只把最终结果写入 answer.json，使用一个严格的 JSON 对象；字段必须完整，不加额外字段。"
        "答案来自此前的有效证据，必要时使用文件工具或上下文归档工具核实。完成后简短说明。"
    )
    seed = {"case_id": case["id"], "scale": scale, "messages": messages, "prompt": prompt,
            "workspace_files": workspace, "ingress": ingress, "history_sha256": history_hash(messages),
            "initial_context_state": {}, "history_origin": "synthetic_fixed_history", "current_turn": "new_user_turn"}
    gold = {key: deepcopy(case[key]) for key in ("id", "name", "expected", "fact_markers")}
    gold["requires_archive_evidence"] = case.get("requires_archive_evidence", False)
    return seed, gold


def prepare(output: Path, *, scale="production"):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    index = []
    for case in definitions():
        seed, gold = material(case, scale)
        root = output / case["id"]
        write_json(root / "seed.json", seed)
        write_json(root / "gold.json", gold)
        write_json(root / "review-template.json", {
            "case_id": case["id"], "trial_directory": "", "reviewer": "", "summary_revision": None,
            "checks": [{"field": key, "expected": value, "preserved": None, "source_quote": "", "summary_quote": ""}
                       for key, value in gold["expected"].items()],
            "stale_requirement_retained": None, "unsupported_completion_claim": None,
            "exact_identifier_corrupted": None, "fabricated_authorization": None,
            "required_fact_not_in_summary_input": None, "notes": "",
        })
        (root / "prompt.txt").write_text(seed["prompt"] + "\n", encoding="utf-8")
        for relative, text in seed["workspace_files"].items():
            path = root / "workspace" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        index.append({"id": case["id"], "name": case["name"], "focus": case["focus"],
                      "message_count": len(seed["messages"]), "history_chars": len(json.dumps(seed["messages"], ensure_ascii=False)),
                      "history_sha256": seed["history_sha256"]})
    write_json(output / "index.json", {"version": "context-v1", "scale": scale, "cases": index,
                                       "profiles": {v: context_settings(v, scale) for v in VARIANTS}})
    (output / "README.md").write_text(
        "# 已展开的固定历史材料\n\nseed.json 是未压缩的输入，gold.json 是独立验收答案。"
        "只将各案例 workspace/ 中的文件交给 Agent；不要把 gold、seed 或评测源码复制进去。\n\n"
        "S04 的 __INGRESS_0__ 在 worker 中经真实 finalize_tool_results 替换为归档预览，路径由试验目录生成。"
        "raw_output 是上下文层接收的合成原文，不表示真实 bash 工具会把同样长度的输出完整传入。\n",
        encoding="utf-8",
    )
    return index
