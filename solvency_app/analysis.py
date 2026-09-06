"""Reporting attribution and data-scope checks, separate from scenario estimates."""
from __future__ import annotations

import pandas as pd

from .workbook import ReportSnapshot, WorkbookData


def report_values(report: pd.DataFrame) -> dict[str, float]:
    values = pd.to_numeric(report["期末数"], errors="coerce")
    return dict(zip(report["项目"].str.strip(), values))


def ratio_attribution(current: ReportSnapshot, previous: ReportSnapshot, metric: str) -> pd.DataFrame:
    """Exact sequential bridge: change numerator first, then denominator."""
    core = metric == "核心偿付能力充足率"
    before = previous.metrics.core_capital if core else previous.metrics.actual_capital
    after = current.metrics.core_capital if core else current.metrics.actual_capital
    d0, d1 = previous.metrics.minimum_capital, current.metrics.minimum_capital
    return pd.DataFrame([
        {"项目": "比较期充足率", "类型": "total", "数值": before / d0 * 100},
        {"项目": "核心资本变化" if core else "实际资本变化", "类型": "relative", "数值": (after - before) / d0 * 100},
        {"项目": "最低资本变化", "类型": "relative", "数值": (after / d1 - after / d0) * 100},
        {"项目": "本期充足率", "类型": "total", "数值": after / d1 * 100},
    ])


def report_changes(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    now, before = report_values(current), report_values(previous)
    return pd.DataFrame([
        {"项目": name, "比较期": before.get(name, float("nan")), "本期": value,
         "变化": value - before.get(name, float("nan"))}
        for name, value in now.items()
    ])


def reconciliation(snapshot: ReportSnapshot | WorkbookData) -> pd.DataFrame:
    m = snapshot.metrics
    s01, s05 = report_values(snapshot.s01), report_values(snapshot.s05)
    checks = [
        ("认可资产 − 认可负债 = 实际资本", m.admitted_assets - m.admitted_liabilities, m.actual_capital, 1.0, "元"),
        ("量化 + 控制风险 + 附加 = 最低资本", m.quantitative_minimum_capital + m.control_risk_minimum_capital + m.additional_capital, m.minimum_capital, 1.0, "元"),
        ("S01 与 S05 最低资本一致", s05.get("最低资本", float("nan")), m.minimum_capital, 1.0, "元"),
        ("核心充足率与报表披露值一致", m.core_solvency_ratio * 100, s01.get("核心偿付能力充足率", float("nan")) * 100, 0.0051, "百分点"),
        ("综合充足率与报表披露值一致", m.comprehensive_solvency_ratio * 100, s01.get("综合偿付能力充足率", float("nan")) * 100, 0.0051, "百分点"),
    ]
    return pd.DataFrame([
        {"检查项": name, "计算值": actual, "底稿值": expected, "差额": actual - expected,
         "容差": tolerance, "单位": unit,
         "结果": "一致" if pd.notna(actual - expected) and abs(actual - expected) <= tolerance else "需核查"}
        for name, actual, expected, tolerance, unit in checks
    ])


def exposure_layers(kbqs: pd.DataFrame) -> pd.DataFrame:
    """Do not infer economic holdings by summing parent and child assets."""
    work = kbqs.copy()
    layers = pd.to_numeric(work.get("交易结构层级", pd.Series(index=work.index, dtype=float)), errors="coerce")
    work["数据层次"] = "层级缺失 / 待核对"
    work.loc[layers == 0, "数据层次"] = "表层记录（尚未与总表勾稽）"
    work.loc[layers > 0, "数据层次"] = "穿透底层记录（不可与表层相加）"
    return work.groupby("数据层次", sort=False).agg(记录数=("认可价值", "size"), 认可价值=("认可价值", "sum")).reset_index()
