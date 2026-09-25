"""Conditional price scenarios, never a claim about a provider's actual bill."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from evals.context_suite.token_report import KINDS
from evals.evidence import file_hash, read_json
from evals.metrics import TOKEN_FIELDS

PRICING_PATH = Path(__file__).with_name("pricing.json")


def load_pricing(path=PRICING_PATH):
    pricing = read_json(path)
    if pricing.get("currency") != "CNY" or pricing.get("unit_tokens") != 1_000_000:
        raise ValueError("Pricing must use CNY per 1,000,000 tokens")
    scenarios = pricing.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != {"offpeak", "peak"}:
        raise ValueError("Pricing requires offpeak and peak scenarios")
    for scenario in scenarios.values():
        for kind in KINDS:
            rates = scenario.get(kind, {})
            if set(rates) != set(TOKEN_FIELDS):
                raise ValueError("Pricing must explicitly specify all four token fields (null if unknown)")
            for value in rates.values():
                if value is None:
                    continue
                try:
                    rate = Decimal(str(value))
                except InvalidOperation as exc:
                    raise ValueError("Invalid token price") from exc
                if not rate.is_finite() or rate < 0:
                    raise ValueError("Token prices must be finite and nonnegative")
    return {**pricing, "source_file_sha256": file_hash(path)}


def _cost(values, rates):
    total = Decimal(0)
    for field in TOKEN_FIELDS:
        count = values.get(field)
        if type(count) is not int or count < 0:
            return None, f"{field} 用量缺失或无效"
        rate = rates[field]
        if count and rate is None:
            return None, f"{field} 有用量，但价格未提供"
        if count:
            total += Decimal(count) * Decimal(str(rate)) / Decimal(1_000_000)
    return total, None


def _sum(values):
    return sum(values, Decimal(0)) if all(v is not None for v in values) else None


def _json_numbers(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_numbers(v) for v in value]
    return value


def build_cost_report(tokens, pricing=None):
    pricing = load_pricing() if pricing is None else pricing
    scenarios = {}
    for name, scenario in pricing["scenarios"].items():
        trials = []
        for trial in tokens["trials"]:
            by_kind = {}
            for kind in KINDS:
                values = trial["by_kind"][kind]
                amount, reason = _cost(values, scenario[kind])
                if tokens["mode"] != "live":
                    amount, reason = None, "离线模拟，没有真实用量"
                elif not values["complete"]:
                    amount, reason = None, "请求或用量不完整，不能按零成本处理"
                by_kind[kind] = {"estimated_cny": amount, "unavailable_reason": reason}
            trials.append({"case_id": trial["case_id"], "variant": trial["variant"], "repeat": trial["repeat"],
                           "trial_directory": trial["trial_directory"], "task_success": trial["task_success"],
                           "by_kind": by_kind,
                           "estimated_cny": _sum([v["estimated_cny"] for v in by_kind.values()])})
        groups = []
        for token_group in tokens["groups"]:
            subset = [t for t in trials if t["variant"] == token_group["variant"]]
            amount = _sum([t["estimated_cny"] for t in subset])
            groups.append({"variant": token_group["variant"], "trials": len(subset),
                           "successes": sum(t["task_success"] for t in subset),
                           "total_tokens": token_group["totals"]["total_tokens"],
                           "by_kind": {kind: _sum([t["by_kind"][kind]["estimated_cny"] for t in subset]) for kind in KINDS},
                           "estimated_cny": amount,
                           "mean_cny_per_trial": amount / len(subset) if amount is not None else None})
        pairs = []
        for token_pair in tokens["paired_A_D"]:
            pair = {t["variant"]: t for t in trials if t["case_id"] == token_pair["case_id"]
                    and t["repeat"] == token_pair["repeat"] and t["variant"] in {"A", "D"}}
            a = pair.get("A", {}).get("estimated_cny")
            d = pair.get("D", {}).get("estimated_cny")
            comparable = token_pair["comparable"] and a is not None and d is not None
            pairs.append({"case_id": token_pair["case_id"], "repeat": token_pair["repeat"],
                          "A_estimated_cny": a, "D_estimated_cny": d, "comparable": comparable,
                          "reduction_fraction": (a - d) / a if comparable and a else None})
        comparable = bool(pairs) and all(p["comparable"] for p in pairs)
        a = _sum([p["A_estimated_cny"] for p in pairs]) if comparable else None
        d = _sum([p["D_estimated_cny"] for p in pairs]) if comparable else None
        scenarios[name] = {"label": scenario["label"], "trials": trials, "groups": groups,
                           "paired_A_D": pairs, "comparison": {
                               "comparable": comparable, "A_estimated_cny": a, "D_estimated_cny": d,
                               "reduction_fraction": (a - d) / a if comparable and a else None}}
    return _json_numbers({"version": "conditional-cost-v1", "currency": "CNY", "actual_billed_cny": None,
                          "status": "conditional_estimate_not_bill", "pricing": pricing, "scenarios": scenarios})


def _money(value):
    return "未知" if value is None else f"¥{value:.8f}"


def cost_report_lines(costs):
    lines = ["", "**Token 与费用总览（人民币，按用户价格表估算）**", "",
             "以下假设主模型和摘要模型分别适用所列单价。尚未核实具体模型、平台、生效日期及峰谷时段；"
             "两列分别是假设全部请求按空闲/高峰单价计算，不是实际扣费，也不是已确认的账单上下界。",
             "公式：（普通输入 × 未命中单价 + 缓存读取 × 命中单价 + 输出 × 输出单价）÷ 1,000,000。"
             "缓存创建若非零而没有单价，费用显示未知。摘要、重试和失败试验已记录的用量均纳入；缺失不当零。", "",
             "| 时段假设 | 调用类型 | 未命中输入/百万 token | 缓存命中/百万 token | 输出/百万 token |",
             "|---|---|---:|---:|---:|"]
    for scenario in costs["pricing"]["scenarios"].values():
        for kind, label in (("main", "主模型"), ("context_summary", "摘要")):
            rates = scenario[kind]
            def rate(field):
                return "未知" if rates[field] is None else f'¥{rates[field]}'
            lines.append(f'| {scenario["label"]} | {label} | {rate("input_tokens")} | {rate("cache_read_input_tokens")} | {rate("output_tokens")} |')
    lines += ["", "| 组 | 通过/试验 | 总 token（含摘要） | 空闲估算总费用 | 高峰估算总费用 | 空闲平均/场 | 高峰平均/场 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    offpeak, peak = costs["scenarios"]["offpeak"], costs["scenarios"]["peak"]
    for low, high in zip(offpeak["groups"], peak["groups"]):
        token_text = "未知" if low["total_tokens"] is None else f'{low["total_tokens"]:,}'
        lines.append(f'| {low["variant"]} | {low["successes"]}/{low["trials"]} | {token_text} | {_money(low["estimated_cny"])} | {_money(high["estimated_cny"])} | {_money(low["mean_cny_per_trial"])} | {_money(high["mean_cny_per_trial"])} |')
    lines += ["", "| 组 | 调用类型 | 空闲估算费用 | 高峰估算费用 |", "|---|---|---:|---:|"]
    for low, high in zip(offpeak["groups"], peak["groups"]):
        for kind, label in (("main", "主模型"), ("context_summary", "摘要")):
            lines.append(f'| {low["variant"]} | {label} | {_money(low["by_kind"][kind])} | {_money(high["by_kind"][kind])} |')
    for scenario in costs["scenarios"].values():
        comparison = scenario["comparison"]
        fraction = comparison["reduction_fraction"]
        change = "不计算（用量、价格、配对条件不足或 A 费用为零）" if fraction is None else (
            f'{"降低" if fraction >= 0 else "增加"} {abs(fraction) * 100:.1f}%')
        lines += ["", f'{scenario["label"]}：D 相对 A 的估算费用{change}。']
    lines += ["", "费用与答对率一起判断，失败后少做任务产生的低费用不代表效率提升。"
              "单价来自用户截图，完整价格配置及其 SHA-256 保存在 result.json 的 cost_estimates.pricing；"
              "actual_billed_cny 和原 cost 仍为 null，等待账单核验。"]
    return lines
