"""Portable, versioned scenario inputs. No pickle, code, or workbook writes."""
from __future__ import annotations

from dataclasses import asdict, fields
import json
import math

import pandas as pd

from .scenario import Adjustment, PolicyParameters, run_scenario
from .workbench import editor_adjustments, editor_from_adjustments

SCHEMA_VERSION = 1
MODEL_VERSION = "risk-calibration-v1"


def make_plan(name, fingerprint, adjustments, policy):
    return {"schema_version": SCHEMA_VERSION, "model_version": MODEL_VERSION, "name": name.strip()[:60] or "未命名方案",
            "source_fingerprint": fingerprint, "adjustments": [asdict(a) for a in adjustments], "policy": asdict(policy)}


def serialize_plan(plan):
    return json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def load_plan(raw: bytes, fingerprint: str, data):
    if len(raw) > 200_000:
        raise ValueError("方案文件超过 200 KB 上限")
    try:
        plan = json.loads(raw)
        if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION or plan.get("model_version") != MODEL_VERSION:
            raise ValueError("方案格式或模型版本不匹配")
        if plan.get("source_fingerprint") != fingerprint:
            raise ValueError("方案底稿或解析版本与当前不同，请切回原底稿；不自动套用旧结果")
        name = plan["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise ValueError("方案名称无效")
        inputs = plan["adjustments"]
        if not isinstance(inputs, list) or len(inputs) > 100:
            raise ValueError("方案最多支持 100 条调整")
        adjustments = []
        for item in inputs:
            if not isinstance(item, dict) or set(item) != {f.name for f in fields(Adjustment)}:
                raise ValueError("资产调整字段不完整")
            if item["dimension"] != "资产类型" or item["mode"] not in ("position", "price"):
                raise ValueError("方案只支持资产类型的加减仓或价格变化")
            if not isinstance(item["member"], str) or not isinstance(item["duration_bucket"], str):
                raise ValueError("资产名称或久期分组无效")
            for field in ("change_pct", "change_amount"):
                if type(item[field]) not in (float, int) or not math.isfinite(item[field]):
                    raise ValueError("调整金额或百分比不是有效数值")
            if item["change_pct"] and item["change_amount"]:
                raise ValueError("不能同时提供金额和百分比")
            adjustments.append(Adjustment(**item))
        p = plan["policy"]
        if not isinstance(p, dict) or set(p) != {f.name for f in fields(PolicyParameters)}:
            raise ValueError("口径参数字段不完整")
        for key in ("minimum_capital_multiplier", "market_risk_multiplier", "credit_risk_multiplier"):
            value = p[key]
            lower = 0.01 if key == "minimum_capital_multiplier" else 0
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= 2:
                raise ValueError("口径乘数超出允许范围")
        if any(type(p[key]) is not bool for key in ("sync_actual_capital_with_assets", "use_calibrated_factors")):
            raise ValueError("口径开关必须为布尔值")
        policy = PolicyParameters(**p)
        editor_adjustments(editor_from_adjustments(adjustments), data)
        run_scenario(data, adjustments, policy)
        return make_plan(name, fingerprint, adjustments, policy), adjustments, policy
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("方案文件不是有效的情景输入 JSON") from exc


def compare_plans(plans, fingerprint, data):
    rows = []
    for plan in plans:
        valid, adjustments, policy = load_plan(serialize_plan(plan), fingerprint, data)
        result = run_scenario(data, adjustments, policy)
        rows.append({"方案": valid["name"], "风险因子": "底稿校准" if policy.use_calibrated_factors else "旧简化",
                     "调整条数": len(adjustments), "综合充足率（%）": result.scenario["综合偿付能力充足率"] * 100,
                     "核心充足率（%）": result.scenario["核心偿付能力充足率"] * 100,
                     "实际资本（亿元）": result.scenario["实际资本"] / 1e8,
                     "最低资本（亿元）": result.scenario["最低资本"] / 1e8,
                     "认可资产变化（亿元）": (result.scenario["认可资产"] - result.baseline["认可资产"]) / 1e8})
    return pd.DataFrame(rows)


def switch_plan(sell_asset, buy_asset, amount):
    if sell_asset == buy_asset or not math.isfinite(amount) or amount <= 0:
        raise ValueError("请选择不同的卖出和买入资产，并输入正金额")
    return [Adjustment("资产类型", sell_asset, 0, "position", -amount),
            Adjustment("资产类型", buy_asset, 0, "position", amount)]
