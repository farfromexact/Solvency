"""Workbook-derived marginal rates with independent reporting controls.

These are average capital/value ratios in the existing risk view, not regulatory
RFs and not a reconstruction of cash flows or parent/child holdings.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# Column suffixes are checked against the workbook's upper-level headers by the reader.
RISK_COLUMNS = {
    "利差风险": ("MC.1", "风险暴露.1", "是否计算利差风险", "信用风险-利差风险最低资本"),
    "交易对手违约风险": ("MC.2", "风险暴露.2", "是否计算交易对手违约风险", "信用风险-交易对手违约风险最低资本"),
    "境外资产价格风险": ("MC.3", "风险暴露.3", "是否计算境外资产价格", None),
    "汇率风险": ("MC.4", "风险暴露.4", "是否计算汇率风险", "市场风险-汇率风险最低资本"),
    "权益价格风险": ("MC.5", "风险暴露.5", "是否计算权益价格风险", "市场风险-权益价格风险最低资本"),
    "房地产价格风险": ("MC.6", "风险暴露.6", "是否计算房地产价格风险", "市场风险-房地产价格风险最低资本"),
}
FOREIGN_FIXED = "市场风险-境外固定收益类资产价格风险最低资本"
FOREIGN_EQUITY = "市场风险-境外权益类资产价格风险最低资本"


def build_calibration(detail: pd.DataFrame, kbqs: pd.DataFrame, s05: pd.DataFrame):
    lookup = dict(zip(s05["项目"].str.strip(), pd.to_numeric(s05["期末数"], errors="coerce")))
    checks, tables = [], []

    def check(name, actual, expected, tolerance=1.0):
        ok = np.isfinite(actual) and np.isfinite(expected) and abs(actual - expected) <= tolerance
        checks.append({"检查项": name, "计算值": actual, "底稿值": expected,
                       "差额": actual - expected, "容差": tolerance, "结果": "一致" if ok else "需核查"})

    if detail.empty:
        check("CAL_DETAIL 风险明细可读取", float("nan"), 1)
        return pd.DataFrame(), pd.DataFrame(checks)
    values = pd.to_numeric(detail["认可价值"], errors="coerce")
    by_asset = values.groupby(detail["资产类型"]).sum(min_count=1)
    kb_values = kbqs.groupby("资产类型")["认可价值"].sum(min_count=1)
    aligned = pd.concat([by_asset.rename("cal"), kb_values.rename("kb")], axis=1)
    gap = (aligned["cal"] - aligned["kb"]).abs()
    check("CAL_DETAIL 与 KBQS 各资产类型价值最大差额", gap.max() if aligned.notna().all().all() else float("nan"), 0)
    check("CAL_DETAIL 认可价值有效", float((~np.isfinite(values)).sum()), 0, 0)

    for risk, (mc_col, ex_col, flag_col, report_item) in RISK_COLUMNS.items():
        if not {mc_col, ex_col, flag_col}.issubset(detail.columns):
            check(f"{risk} 字段完整", float("nan"), 1)
            continue
        mc = pd.to_numeric(detail[mc_col], errors="coerce")
        exposure = pd.to_numeric(detail[ex_col], errors="coerce")
        # Blank outputs in non-applicable risk blocks remain structural N/A.
        # Active risk rows and rows with a nonzero exposure must have a finite MC.
        active = detail[flag_col].astype(str).str.strip().eq("是") | exposure.fillna(0).ne(0)
        malformed = detail[mc_col].notna() & ~np.isfinite(mc)
        invalid = (active & ~np.isfinite(mc)) | malformed
        check(f"{risk} 有效风险资本", float(invalid.sum()), 0, 0)
        mc = mc.fillna(0)
        expected = lookup.get(report_item, float("nan")) if report_item else lookup.get(FOREIGN_FIXED, float("nan")) + lookup.get(FOREIGN_EQUITY, float("nan"))
        check(f"{risk} 明细与 S05", mc.sum(), expected)
        mapped_risk = risk
        if risk == "境外资产价格风险":
            fixed, equity = lookup.get(FOREIGN_FIXED, float("nan")), lookup.get(FOREIGN_EQUITY, float("nan"))
            # A combined detail column cannot be divided between two nonzero risks.
            if fixed == 0:
                mapped_risk = "境外权益价格风险"
            elif equity == 0:
                mapped_risk = "境外固定收益价格风险"
            else:
                check("境外价格明细可区分固收与权益", float("nan"), 1)
                continue
        totals = mc.groupby(detail["资产类型"]).sum()
        ex_totals = exposure.groupby(detail["资产类型"]).sum(min_count=1)
        for asset, value in by_asset.items():
            capital = float(totals.get(asset, 0))
            if value <= 0:
                if capital != 0:
                    check(f"{asset} 的 {risk} 有正价值分母", float("nan"), 1)
                continue
            tables.append({"资产类型": asset, "风险类型": mapped_risk, "认可价值": value,
                           "风险暴露": ex_totals.get(asset, float("nan")), "风险资本": capital,
                           "单位价值资本因子": capital / value,
                           "来源字段": mc_col, "来源/口径": "CAL_DETAIL MC / 同类风险视图认可价值"})
    return pd.DataFrame(tables), pd.DataFrame(checks)


def calibration_ready(checks: pd.DataFrame) -> bool:
    return not checks.empty and checks["结果"].eq("一致").all()
