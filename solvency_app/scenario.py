from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .workbook import BaselineMetrics, WorkbookData


RISK_MAP = {
    "利率风险": ("利率风险暴露", "市场风险-利率风险最低资本"),
    "权益价格风险": ("权益价格风险暴露", "市场风险-权益价格风险最低资本"),
    "房地产价格风险": ("房地产风险暴露", "市场风险-房地产价格风险最低资本"),
    "汇率风险": ("汇率风险暴露", "市场风险-汇率风险最低资本"),
    "利差风险": ("利差风险暴露", "信用风险-利差风险最低资本"),
    "交易对手违约风险": ("交易对手风险暴露", "信用风险-交易对手违约风险最低资本"),
}

NUMERIC_COLUMNS = [
    "认可价值",
    "利率风险暴露",
    "利差风险暴露",
    "交易对手风险暴露",
    "权益价格风险暴露",
    "房地产风险暴露",
    "汇率风险暴露",
]

PRICE_MOVE_COLUMNS = [
    "认可价值",
    "利率风险暴露",
    "利差风险暴露",
    "交易对手风险暴露",
    "权益价格风险暴露",
    "房地产风险暴露",
    "汇率风险暴露",
]


@dataclass(frozen=True)
class Adjustment:
    dimension: str
    member: str
    change_pct: float
    mode: str = "position"
    change_amount: float = 0.0


@dataclass(frozen=True)
class PolicyParameters:
    minimum_capital_multiplier: float = 1.0
    market_risk_multiplier: float = 1.0
    credit_risk_multiplier: float = 1.0
    sync_actual_capital_with_assets: bool = False


@dataclass(frozen=True)
class ScenarioResult:
    baseline: dict[str, float]
    scenario: dict[str, float]
    risk_rates: pd.DataFrame
    exposure_summary: pd.DataFrame
    contribution_summary: pd.DataFrame
    adjustment_summary: pd.DataFrame


def build_asset_summary(kbqs: pd.DataFrame, dimension: str) -> pd.DataFrame:
    summary = (
        kbqs.groupby(dimension, dropna=False)[NUMERIC_COLUMNS]
        .sum()
        .sort_values("认可价值", ascending=False)
        .reset_index()
    )
    return summary


def run_scenario(
    data: WorkbookData,
    adjustments: list[Adjustment],
    policy: PolicyParameters | None = None,
) -> ScenarioResult:
    policy = policy or PolicyParameters()
    base_metrics = data.metrics
    risk_rates = _derive_risk_rates(data.kbqs, data.s05)
    adjusted_kbqs, adjustment_summary, valuation_capital_delta = _apply_adjustments(data.kbqs, adjustments)
    baseline_capital = _capital_by_risk(data.kbqs, risk_rates)
    scenario_capital = _capital_by_risk(adjusted_kbqs, risk_rates)

    market_delta = _risk_delta(
        baseline_capital, scenario_capital, ["利率风险", "权益价格风险", "房地产价格风险", "汇率风险"]
    )
    credit_delta = _risk_delta(
        baseline_capital, scenario_capital, ["利差风险", "交易对手违约风险"]
    )
    adjusted_delta = (
        market_delta * policy.market_risk_multiplier
        + credit_delta * policy.credit_risk_multiplier
    )

    scenario_minimum_capital = (
        base_metrics.minimum_capital + adjusted_delta
    ) * policy.minimum_capital_multiplier
    asset_delta = adjusted_kbqs["认可价值"].sum() - data.kbqs["认可价值"].sum()
    position_asset_delta = asset_delta - valuation_capital_delta
    scenario_actual_capital = base_metrics.actual_capital
    scenario_actual_capital += valuation_capital_delta
    if policy.sync_actual_capital_with_assets:
        scenario_actual_capital += position_asset_delta

    scenario_core_capital = base_metrics.core_capital
    scenario_core_capital += valuation_capital_delta
    if policy.sync_actual_capital_with_assets:
        scenario_core_capital += position_asset_delta

    baseline = _metrics_dict(base_metrics)
    scenario = {
        "认可资产": base_metrics.admitted_assets + asset_delta,
        "实际资本": scenario_actual_capital,
        "核心资本": scenario_core_capital,
        "最低资本": scenario_minimum_capital,
        "量化风险最低资本": base_metrics.quantitative_minimum_capital + adjusted_delta,
        "核心偿付能力充足率": _safe_div(scenario_core_capital, scenario_minimum_capital),
        "综合偿付能力充足率": _safe_div(scenario_actual_capital, scenario_minimum_capital),
    }

    exposure_summary = _build_exposure_comparison(data.kbqs, adjusted_kbqs)
    contribution_summary = _build_contribution_summary(baseline_capital, scenario_capital)
    return ScenarioResult(
        baseline=baseline,
        scenario=scenario,
        risk_rates=risk_rates,
        exposure_summary=exposure_summary,
        contribution_summary=contribution_summary,
        adjustment_summary=adjustment_summary,
    )


def _derive_risk_rates(kbqs: pd.DataFrame, s05: pd.DataFrame) -> pd.DataFrame:
    rows = []
    capital_lookup = {
        str(row.get("项目", "")).strip(): float(row.get("期末数") or 0.0)
        for _, row in s05.iterrows()
    }
    for risk_name, (exposure_col, capital_item) in RISK_MAP.items():
        exposure = float(pd.to_numeric(kbqs[exposure_col], errors="coerce").fillna(0).sum())
        capital = _lookup_capital(capital_lookup, capital_item)
        rows.append(
            {
                "风险类型": risk_name,
                "风险暴露字段": exposure_col,
                "基准风险暴露": exposure,
                "基准最低资本": capital,
                "单位资本率": _safe_div(capital, exposure),
            }
        )
    return pd.DataFrame(rows)


def _lookup_capital(capital_lookup: dict[str, float], capital_item: str) -> float:
    if capital_item in capital_lookup:
        return capital_lookup[capital_item]
    for key, value in capital_lookup.items():
        if capital_item in key or key in capital_item:
            return value
    return 0.0


def _apply_adjustments(kbqs: pd.DataFrame, adjustments: list[Adjustment]) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    adjusted = kbqs.copy()
    rows = []
    valuation_capital_delta = 0.0
    for item in adjustments:
        if not item.member or (item.change_pct == 0 and item.change_amount == 0):
            continue
        mask = adjusted[item.dimension].astype(str) == str(item.member)
        before_value = float(adjusted.loc[mask, "认可价值"].sum())
        if before_value == 0 and item.change_amount != 0:
            factor = 1.0
        elif item.change_amount != 0:
            factor = max(0.0, 1.0 + item.change_amount / before_value)
        else:
            factor = max(0.0, 1.0 + item.change_pct / 100.0)

        columns = PRICE_MOVE_COLUMNS if item.mode == "price" else NUMERIC_COLUMNS
        adjusted.loc[mask, columns] = adjusted.loc[mask, columns] * factor
        after_value = float(adjusted.loc[mask, "认可价值"].sum())
        value_delta = after_value - before_value
        if item.mode == "price":
            valuation_capital_delta += value_delta
        rows.append(
            {
                "模块": "上涨/下跌" if item.mode == "price" else "加仓/减仓/建仓",
                "维度": item.dimension,
                "对象": item.member,
                "变化比例": factor - 1.0,
                "认可价值变化": value_delta,
            }
        )
    return adjusted, pd.DataFrame(rows), valuation_capital_delta


def _capital_by_risk(kbqs: pd.DataFrame, risk_rates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in risk_rates.iterrows():
        exposure_col = row["风险暴露字段"]
        exposure = float(pd.to_numeric(kbqs[exposure_col], errors="coerce").fillna(0).sum())
        rate = float(row["单位资本率"])
        rows.append(
            {
                "风险类型": row["风险类型"],
                "风险暴露": exposure,
                "估算最低资本": exposure * rate,
            }
        )
    return pd.DataFrame(rows)


def _risk_delta(base: pd.DataFrame, scenario: pd.DataFrame, risks: list[str]) -> float:
    base_sum = base.loc[base["风险类型"].isin(risks), "估算最低资本"].sum()
    scenario_sum = scenario.loc[scenario["风险类型"].isin(risks), "估算最低资本"].sum()
    return float(scenario_sum - base_sum)


def _build_exposure_comparison(base: pd.DataFrame, scenario: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in [
        "认可价值",
        "利率风险暴露",
        "利差风险暴露",
        "交易对手风险暴露",
        "权益价格风险暴露",
        "房地产风险暴露",
        "汇率风险暴露",
    ]:
        before = float(base[col].sum())
        after = float(scenario[col].sum())
        rows.append(
            {
                "项目": col,
                "基准": before,
                "情景": after,
                "变化": after - before,
                "变化率": _safe_div(after - before, before),
            }
        )
    return pd.DataFrame(rows)


def _build_contribution_summary(base: pd.DataFrame, scenario: pd.DataFrame) -> pd.DataFrame:
    merged = base.merge(scenario, on="风险类型", suffixes=("_基准", "_情景"))
    merged["最低资本变化"] = merged["估算最低资本_情景"] - merged["估算最低资本_基准"]
    merged["风险暴露变化"] = merged["风险暴露_情景"] - merged["风险暴露_基准"]
    return merged.sort_values("最低资本变化", key=lambda s: s.abs(), ascending=False)


def _metrics_dict(metrics: BaselineMetrics) -> dict[str, float]:
    return {
        "认可资产": metrics.admitted_assets,
        "实际资本": metrics.actual_capital,
        "核心资本": metrics.core_capital,
        "最低资本": metrics.minimum_capital,
        "量化风险最低资本": metrics.quantitative_minimum_capital,
        "核心偿付能力充足率": metrics.core_solvency_ratio,
        "综合偿付能力充足率": metrics.comprehensive_solvency_ratio,
    }


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator
