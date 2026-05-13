from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import pandas as pd

from .workbook import BaselineMetrics, WorkbookData


VALUE_COL = "认可价值"
ACCOUNT_COL = "账户"
ASSET_TYPE_COL = "资产类型"

NUMERIC_COLUMNS = [
    VALUE_COL,
    "利率风险暴露",
    "利差风险暴露",
    "交易对手风险暴露",
    "权益价格风险暴露",
    "房地产风险暴露",
    "汇率风险暴露",
]

PRICE_MOVE_COLUMNS = NUMERIC_COLUMNS

MARKET_ITEMS = {
    "利率风险": "市场风险-利率风险最低资本",
    "权益价格风险": "市场风险-权益价格风险最低资本",
    "房地产价格风险": "市场风险-房地产价格风险最低资本",
    "境外固定收益价格风险": "市场风险-境外固定收益类资产价格风险最低资本",
    "境外权益价格风险": "市场风险-境外权益类资产价格风险最低资本",
    "汇率风险": "市场风险-汇率风险最低资本",
}

CREDIT_ITEMS = {
    "利差风险": "信用风险-利差风险最低资本",
    "交易对手违约风险": "信用风险-交易对手违约风险最低资本",
}

QUANT_ITEMS = {
    "寿险业务保险风险": "寿险业务保险风险最低资本合计",
    "非寿险业务保险风险": "非寿险业务保险风险最低资本合计",
    "市场风险": "市场风险-最低资本合计",
    "信用风险": "信用风险-最低资本合计",
}

MARKET_CORRELATION = pd.DataFrame(
    [
        [1.00, -0.14, -0.18, 0.00, -0.16, 0.07],
        [-0.14, 1.00, 0.75, 0.10, 0.83, 0.06],
        [-0.18, 0.75, 1.00, 0.10, 0.83, 0.06],
        [0.00, 0.10, 0.10, 1.00, 0.10, 0.06],
        [-0.16, 0.83, 0.83, 0.10, 1.00, 0.06],
        [0.07, 0.06, 0.06, 0.06, 0.06, 1.00],
    ],
    index=list(MARKET_ITEMS),
    columns=list(MARKET_ITEMS),
)

CREDIT_CORRELATION = pd.DataFrame(
    [[1.00, 0.25], [0.25, 1.00]],
    index=list(CREDIT_ITEMS),
    columns=list(CREDIT_ITEMS),
)

QUANT_CORRELATION = pd.DataFrame(
    [
        [1.00, 0.00, 0.30, 0.15],
        [0.00, 1.00, 0.10, 0.10],
        [0.30, 0.10, 1.00, 0.35],
        [0.15, 0.10, 0.35, 1.00],
    ],
    index=list(QUANT_ITEMS),
    columns=list(QUANT_ITEMS),
)


@dataclass(frozen=True)
class Adjustment:
    dimension: str
    member: str
    change_pct: float
    mode: str = "position"
    change_amount: float = 0.0
    duration_bucket: str = "存量平均"


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


@dataclass(frozen=True)
class RiskFactors:
    interest_hedge: float = 0.0
    spread: float = 0.0
    counterparty: float = 0.0
    equity: float = 0.0
    real_estate: float = 0.0
    fx: float = 0.0
    category: str = "未映射"


FACTOR_ASSUMPTIONS = [
    ("债券基金/固收产品", "债券型基金、固定收益类资管产品", RiskFactors(equity=0.06, category="债券基金/固收产品")),
    ("上市股票", "上市普通股票", RiskFactors(equity=0.3516, category="上市股票")),
    ("股票/混合基金", "股票型基金、混合型基金", RiskFactors(equity=0.2587, category="股票/混合基金")),
    ("未上市股权", "未上市股权、长期股权投资", RiskFactors(equity=0.41, category="未上市股权")),
    ("股权投资基金", "股权投资基金", RiskFactors(equity=0.451, category="股权投资基金")),
    ("债权投资计划", "基础设施/不动产债权投资计划", RiskFactors(counterparty=0.093, category="债权投资计划")),
    ("信托计划", "固定收益类信托计划", RiskFactors(counterparty=0.095, category="信托计划")),
    ("不动产", "不动产项目公司股权、不动产金融产品、REITs等", RiskFactors(real_estate=0.18, category="不动产")),
]


def build_asset_summary(kbqs: pd.DataFrame, dimension: str) -> pd.DataFrame:
    summary = (
        kbqs.groupby(dimension, dropna=False)[NUMERIC_COLUMNS]
        .sum()
        .sort_values(VALUE_COL, ascending=False)
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
    capital_lookup = _capital_lookup(data.s05)

    adjusted_kbqs, adjustment_summary, valuation_capital_delta = _apply_adjustments(data.kbqs, adjustments)
    baseline_capital = _baseline_capital_by_risk(capital_lookup)
    capital_deltas = _calculate_capital_deltas(
        data.kbqs,
        adjustment_summary,
        capital_lookup,
        policy,
        data.interest_factor_table,
        data.spread_factor_table,
    )
    scenario_capital = _scenario_capital_by_risk(baseline_capital, capital_deltas)

    market_delta = capital_deltas["市场风险"]
    credit_delta = capital_deltas["信用风险"]
    quant_delta = _quantitative_delta(capital_lookup, market_delta, credit_delta)

    scenario_minimum_capital = (
        base_metrics.minimum_capital + quant_delta
    ) * policy.minimum_capital_multiplier
    scenario_quantitative_capital = (
        base_metrics.quantitative_minimum_capital + quant_delta
    ) * policy.minimum_capital_multiplier

    asset_delta = adjusted_kbqs[VALUE_COL].sum() - data.kbqs[VALUE_COL].sum()
    position_asset_delta = asset_delta - valuation_capital_delta
    scenario_actual_capital = base_metrics.actual_capital + valuation_capital_delta
    scenario_core_capital = base_metrics.core_capital + valuation_capital_delta
    if policy.sync_actual_capital_with_assets:
        scenario_actual_capital += position_asset_delta
        scenario_core_capital += position_asset_delta

    baseline = _metrics_dict(base_metrics)
    scenario = {
        "认可资产": base_metrics.admitted_assets + asset_delta,
        "实际资本": scenario_actual_capital,
        "核心资本": scenario_core_capital,
        "最低资本": scenario_minimum_capital,
        "量化风险最低资本": scenario_quantitative_capital,
        "核心偿付能力充足率": _safe_div(scenario_core_capital, scenario_minimum_capital),
        "综合偿付能力充足率": _safe_div(scenario_actual_capital, scenario_minimum_capital),
    }

    return ScenarioResult(
        baseline=baseline,
        scenario=scenario,
        risk_rates=_factor_table(data.interest_factor_table, data.spread_factor_table),
        exposure_summary=_build_exposure_comparison(data.kbqs, adjusted_kbqs),
        contribution_summary=_build_contribution_summary(baseline_capital, scenario_capital),
        adjustment_summary=adjustment_summary,
    )


def _apply_adjustments(kbqs: pd.DataFrame, adjustments: list[Adjustment]) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    adjusted = kbqs.copy()
    rows = []
    valuation_capital_delta = 0.0
    for item in adjustments:
        if not item.member or (item.change_pct == 0 and item.change_amount == 0):
            continue
        mask = adjusted[item.dimension].astype(str) == str(item.member)
        before_value = float(adjusted.loc[mask, VALUE_COL].sum())
        if before_value == 0 and item.change_amount != 0:
            factor = 1.0
        elif item.change_amount != 0:
            factor = max(0.0, 1.0 + item.change_amount / before_value)
        else:
            factor = max(0.0, 1.0 + item.change_pct / 100.0)

        columns = PRICE_MOVE_COLUMNS if item.mode == "price" else NUMERIC_COLUMNS
        adjusted.loc[mask, columns] = adjusted.loc[mask, columns] * factor
        after_value = float(adjusted.loc[mask, VALUE_COL].sum())
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
                "久期桶": item.duration_bucket or "存量平均",
            }
        )
    return adjusted, pd.DataFrame(rows), valuation_capital_delta


def _calculate_capital_deltas(
    kbqs: pd.DataFrame,
    adjustment_summary: pd.DataFrame,
    capital_lookup: dict[str, float],
    policy: PolicyParameters,
    interest_factor_table: pd.DataFrame,
    spread_factor_table: pd.DataFrame,
) -> dict[str, float]:
    direct = {name: 0.0 for name in [*MARKET_ITEMS, *CREDIT_ITEMS]}
    if not adjustment_summary.empty:
        for _, row in adjustment_summary.iterrows():
            direct = _add_risk_delta_from_adjustment(kbqs, row, direct, interest_factor_table, spread_factor_table)

    base_market = _vector_from_lookup(capital_lookup, MARKET_ITEMS)
    scenario_market = base_market.copy()
    scenario_market["利率风险"] = max(0.0, scenario_market["利率风险"] + direct["利率风险"])
    for name in ["权益价格风险", "房地产价格风险", "境外固定收益价格风险", "境外权益价格风险", "汇率风险"]:
        scenario_market[name] = max(0.0, scenario_market[name] + direct[name])
    market_delta = (
        _correlated_capital(scenario_market, MARKET_CORRELATION)
        - _correlated_capital(base_market, MARKET_CORRELATION)
    ) * policy.market_risk_multiplier

    base_credit = _vector_from_lookup(capital_lookup, CREDIT_ITEMS)
    scenario_credit = base_credit.copy()
    for name in CREDIT_ITEMS:
        scenario_credit[name] = max(0.0, scenario_credit[name] + direct[name])
    credit_delta = (
        _correlated_capital(scenario_credit, CREDIT_CORRELATION)
        - _correlated_capital(base_credit, CREDIT_CORRELATION)
    ) * policy.credit_risk_multiplier

    direct["市场风险"] = market_delta
    direct["信用风险"] = credit_delta
    return direct


def _add_risk_delta_from_adjustment(
    kbqs: pd.DataFrame,
    adjustment: pd.Series,
    direct: dict[str, float],
    interest_factor_table: pd.DataFrame,
    spread_factor_table: pd.DataFrame,
) -> dict[str, float]:
    out = direct.copy()
    dimension = str(adjustment["维度"])
    member = str(adjustment["对象"])
    value_delta = float(adjustment["认可价值变化"])
    duration_bucket = str(adjustment.get("久期桶", "存量平均") or "存量平均")
    if value_delta == 0:
        return out

    mask = kbqs[dimension].astype(str) == member
    scoped = kbqs.loc[mask, [ASSET_TYPE_COL, VALUE_COL]].copy()
    total = float(scoped[VALUE_COL].sum())
    if total <= 0:
        return out

    by_type = scoped.groupby(ASSET_TYPE_COL, dropna=False)[VALUE_COL].sum()
    for asset_type, base_value in by_type.items():
        delta = value_delta * float(base_value) / total
        factors = _factors_for_asset_type(str(asset_type), duration_bucket, interest_factor_table, spread_factor_table)
        # 寿险利率风险按净现金流情景法计量，新增固收资产现金流是对负债端利率风险的抵减。
        out["利率风险"] -= delta * factors.interest_hedge
        out["利差风险"] += delta * factors.spread
        out["交易对手违约风险"] += delta * factors.counterparty
        out["权益价格风险"] += delta * factors.equity
        out["房地产价格风险"] += delta * factors.real_estate
        out["汇率风险"] += delta * factors.fx
    return out


def _factors_for_asset_type(
    asset_type: str,
    duration_bucket: str,
    interest_factor_table: pd.DataFrame,
    spread_factor_table: pd.DataFrame,
) -> RiskFactors:
    interest_hedge = _lookup_interest_factor(interest_factor_table, asset_type, duration_bucket)
    spread = _lookup_spread_factor(spread_factor_table, asset_type)
    if "政策性金融债" in asset_type or "政府支持机构债" in asset_type:
        return RiskFactors(interest_hedge=interest_hedge, spread=spread, category="政策性金融债")
    if any(key in asset_type for key in ["金融债", "同业存单", "资本债"]):
        return RiskFactors(interest_hedge=interest_hedge, spread=spread, category="金融债")
    if any(key in asset_type for key in ["企业债", "公司债", "中期票据", "资产支持证券", "资产支持计划"]):
        return RiskFactors(interest_hedge=interest_hedge, spread=spread, category="信用债")
    if interest_hedge:
        return RiskFactors(interest_hedge=interest_hedge, spread=spread, category="MC_RESULT利率资产")
    if "债券型" in asset_type or "固定收益类" in asset_type:
        return RiskFactors(equity=0.06, category="债券基金/固收产品")
    if "上市普通股票" in asset_type or asset_type == "优先股":
        return RiskFactors(equity=0.3516, category="上市股票")
    if "股票型" in asset_type or "混合型" in asset_type:
        return RiskFactors(equity=0.2587, category="股票/混合基金")
    if "股权投资基金" in asset_type:
        return RiskFactors(equity=0.451, category="股权投资基金")
    if "未上市股权" in asset_type or "长期股权投资" in asset_type or "股权投资计划" in asset_type:
        return RiskFactors(equity=0.41, category="未上市股权")
    if "信托计划" in asset_type:
        return RiskFactors(counterparty=0.095, category="信托计划")
    if "债权投资计划" in asset_type:
        return RiskFactors(counterparty=0.093, category="债权投资计划")
    if any(key in asset_type for key in ["不动产", "基础设施证券投资基金", "REIT"]):
        return RiskFactors(real_estate=0.18, category="不动产")
    return RiskFactors()


def _lookup_interest_factor(table: pd.DataFrame, asset_type: str, duration_bucket: str) -> float:
    if table.empty:
        return 0.0
    typed = table[table["资产类型"].astype(str) == asset_type]
    if typed.empty:
        return 0.0
    selected = typed[typed["久期桶"].astype(str) == duration_bucket]
    if selected.empty:
        selected = typed[typed["久期桶"].astype(str) == "存量平均"]
    if selected.empty:
        return 0.0
    return float(selected.iloc[0]["利率风险抵减因子"] or 0.0)


def _lookup_spread_factor(table: pd.DataFrame, asset_type: str) -> float:
    if table.empty:
        return 0.0
    selected = table[table["资产类型"].astype(str) == asset_type]
    if selected.empty:
        return 0.0
    return float(selected.iloc[0]["利差风险因子"] or 0.0)


def _baseline_capital_by_risk(capital_lookup: dict[str, float]) -> pd.DataFrame:
    rows = []
    for risk_name, item_name in {**MARKET_ITEMS, **CREDIT_ITEMS}.items():
        rows.append(
            {
                "风险类型": risk_name,
                "风险资本": _lookup_capital(capital_lookup, item_name),
            }
        )
    rows.extend(
        [
            {"风险类型": "市场风险合计", "风险资本": _lookup_capital(capital_lookup, QUANT_ITEMS["市场风险"])},
            {"风险类型": "信用风险合计", "风险资本": _lookup_capital(capital_lookup, QUANT_ITEMS["信用风险"])},
        ]
    )
    return pd.DataFrame(rows)


def _scenario_capital_by_risk(base: pd.DataFrame, deltas: dict[str, float]) -> pd.DataFrame:
    out = base.copy()
    for idx, row in out.iterrows():
        risk = row["风险类型"]
        key = risk.removesuffix("合计")
        out.loc[idx, "风险资本"] = max(0.0, float(row["风险资本"]) + deltas.get(key, 0.0))
    return out


def _quantitative_delta(capital_lookup: dict[str, float], market_delta: float, credit_delta: float) -> float:
    base = _vector_from_lookup(capital_lookup, QUANT_ITEMS)
    scenario = base.copy()
    scenario["市场风险"] = max(0.0, scenario["市场风险"] + market_delta)
    scenario["信用风险"] = max(0.0, scenario["信用风险"] + credit_delta)
    return _correlated_capital(scenario, QUANT_CORRELATION) - _correlated_capital(base, QUANT_CORRELATION)


def _vector_from_lookup(capital_lookup: dict[str, float], items: dict[str, str]) -> pd.Series:
    return pd.Series({risk: _lookup_capital(capital_lookup, item) for risk, item in items.items()}, dtype="float64")


def _correlated_capital(vector: pd.Series, correlation: pd.DataFrame) -> float:
    aligned = vector.reindex(correlation.index).fillna(0.0).astype(float)
    value = float(aligned.to_numpy() @ correlation.to_numpy() @ aligned.to_numpy())
    return sqrt(max(value, 0.0))


def _capital_lookup(s05: pd.DataFrame) -> dict[str, float]:
    return {
        str(row.get("项目", "")).strip(): float(row.get("期末数") or 0.0)
        for _, row in s05.iterrows()
    }


def _lookup_capital(capital_lookup: dict[str, float], capital_item: str) -> float:
    if capital_item in capital_lookup:
        return capital_lookup[capital_item]
    for key, value in capital_lookup.items():
        if capital_item in key or key in capital_item:
            return value
    return 0.0


def _factor_table(interest_factor_table: pd.DataFrame, spread_factor_table: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if not interest_factor_table.empty:
        for _, row in interest_factor_table.iterrows():
            rows.append(
                {
                    "资产映射": row["资产类型"],
                    "适用范围": f"久期桶：{row['久期桶']}",
                    "利率风险抵减因子": row["利率风险抵减因子"],
                    "利差风险因子": 0.0,
                    "交易对手风险因子": 0.0,
                    "权益价格风险因子": 0.0,
                    "房地产风险因子": 0.0,
                    "汇率风险因子": 0.0,
                    "来源/口径": row["来源/口径"],
                }
            )
    if not spread_factor_table.empty:
        for _, row in spread_factor_table.iterrows():
            rows.append(
                {
                    "资产映射": row["资产类型"],
                    "适用范围": "利差风险",
                    "利率风险抵减因子": 0.0,
                    "利差风险因子": row["利差风险因子"],
                    "交易对手风险因子": 0.0,
                    "权益价格风险因子": 0.0,
                    "房地产风险因子": 0.0,
                    "汇率风险因子": 0.0,
                    "来源/口径": row["来源/口径"],
                }
            )
    for category, scope, factors in FACTOR_ASSUMPTIONS:
        rows.append(
            {
                "资产映射": category,
                "适用范围": scope,
                "利率风险抵减因子": factors.interest_hedge,
                "利差风险因子": factors.spread,
                "交易对手风险因子": factors.counterparty,
                "权益价格风险因子": factors.equity,
                "房地产风险因子": factors.real_estate,
                "汇率风险因子": factors.fx,
                "来源/口径": "当前简化资产风险参数；后续可替换为监管规则参数表",
            }
        )
    return pd.DataFrame(rows)


def _build_exposure_comparison(base: pd.DataFrame, scenario: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in NUMERIC_COLUMNS:
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
    merged["最低资本变化"] = merged["风险资本_情景"] - merged["风险资本_基准"]
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
