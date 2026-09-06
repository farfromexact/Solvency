from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.target import solve_target_change
from solvency_app.workbook import (
    WorkbookValidationError,
    discover_workbook_sources,
    find_workbook_source,
    latest_workbook_source,
    load_baseline_metrics,
    load_workbook_data,
    parse_workbook_source,
)


ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_DIR = ROOT / "origin stats"
WORKBOOK = WORKBOOK_DIR / "1000_20260430_20260512.xlsx"


@pytest.fixture(scope="module")
def workbook_data():
    return load_workbook_data(WORKBOOK)


def test_parse_workbook_source_regular_filename():
    source = parse_workbook_source(WORKBOOK)
    assert source.company_code == "1000"
    assert source.report_month == "2026-04"
    assert source.report_date_label == "2026-04-30"
    assert source.timepoint_label == "2026-05-12"
    assert source.version_suffix == ""


def test_parse_workbook_source_version_suffix():
    source = parse_workbook_source(WORKBOOK_DIR / "1000_20251231_20260113v2.xlsx")
    assert source.report_month == "2025-12"
    assert source.report_date_label == "2025-12-31"
    assert source.timepoint_label == "2026-01-13 v2"
    assert source.version_suffix == "v2"


def test_discover_workbook_sources_sorts_and_defaults_to_latest():
    sources = discover_workbook_sources(WORKBOOK_DIR)
    assert [source.report_month for source in sources] == ["2025-12", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]

    latest = latest_workbook_source(sources)
    assert latest is not None
    assert latest.report_month == "2026-07"
    assert latest.timepoint_label == "2026-08-11"
    assert latest.path.name == "1000_20260731_20260811.xlsx"


def test_find_workbook_source_uses_selected_month_and_timepoint():
    sources = discover_workbook_sources(WORKBOOK_DIR)
    selected = find_workbook_source(sources, "2026-02", "2026-03-12")
    assert selected.path.name == "1000_20260228_20260312.xlsx"


def test_lightweight_baseline_metrics_match_full_parse(workbook_data):
    metrics = load_baseline_metrics(WORKBOOK)
    assert metrics.admitted_assets == pytest.approx(workbook_data.metrics.admitted_assets)
    assert metrics.minimum_capital == pytest.approx(workbook_data.metrics.minimum_capital)
    assert metrics.comprehensive_solvency_ratio == pytest.approx(workbook_data.metrics.comprehensive_solvency_ratio)


def test_history_ratio_chart_data_marks_current_and_delta():
    from app import _history_current_and_previous, _history_ratio_axis_domain, _history_ratio_chart_df

    source = parse_workbook_source(WORKBOOK)
    history = pd.DataFrame(
        [
            {
                "source_key": "previous",
                "报告月份": "2026-03",
                "报告月末": "2026-03-31",
                "底稿时点": "2026-04-13",
                "实际资本": 100.0,
                "最低资本": 80.0,
                "量化风险最低资本": 78.0,
                "核心偿付能力充足率": 0.95,
                "综合偿付能力充足率": 1.25,
            },
            {
                "source_key": source.source_key,
                "报告月份": "2026-04",
                "报告月末": source.report_date_label,
                "底稿时点": source.timepoint_label,
                "实际资本": 120.0,
                "最低资本": 90.0,
                "量化风险最低资本": 88.0,
                "核心偿付能力充足率": 1.02,
                "综合偿付能力充足率": 1.30,
            },
        ]
    )

    current, previous = _history_current_and_previous(history, source)
    assert current["报告月份"] == "2026-04"
    assert previous["报告月份"] == "2026-03"

    chart_df = _history_ratio_chart_df(history, source)
    assert chart_df["当前选中"].sum() == 2
    current_comprehensive = chart_df[
        (chart_df["当前选中"]) & (chart_df["指标"] == "综合偿付能力充足率")
    ].iloc[0]
    assert current_comprehensive["充足率"] == pytest.approx(130.0)
    assert current_comprehensive["较上期变化"] == pytest.approx(5.0)

    domain = _history_ratio_axis_domain(chart_df["充足率"])
    assert domain[0] > 0
    assert domain[0] <= 95.0
    assert domain[1] >= 150.0


def test_history_capital_chart_data_uses_yi_yuan_and_compact_height():
    from app import _history_capital_chart_df, _history_table_height

    source = parse_workbook_source(WORKBOOK)
    history = pd.DataFrame(
        [
            {
                "source_key": "previous",
                "报告月份": "2026-03",
                "报告月末": "2026-03-31",
                "底稿时点": "2026-04-13",
                "实际资本": 10_000_000_000.0,
                "最低资本": 8_000_000_000.0,
                "量化风险最低资本": 7_500_000_000.0,
            },
            {
                "source_key": source.source_key,
                "报告月份": "2026-04",
                "报告月末": source.report_date_label,
                "底稿时点": source.timepoint_label,
                "实际资本": 12_000_000_000.0,
                "最低资本": 9_000_000_000.0,
                "量化风险最低资本": 8_800_000_000.0,
            },
        ]
    )

    chart_df = _history_capital_chart_df(history, source)
    current_actual = chart_df[(chart_df["当前选中"]) & (chart_df["指标"] == "实际资本")].iloc[0]
    assert current_actual["金额"] == pytest.approx(120.0)
    assert current_actual["较上期变化"] == pytest.approx(20.0)
    assert _history_table_height(2) < 180


def test_loads_baseline_metrics(workbook_data):
    assert workbook_data.metrics.admitted_assets == pytest.approx(769701295456.44)
    assert workbook_data.metrics.minimum_capital == pytest.approx(51090271656.25)
    assert workbook_data.metrics.comprehensive_solvency_ratio == pytest.approx(1.3348, abs=0.0001)


def test_zero_change_scenario_matches_baseline(workbook_data):
    asset_dimension = workbook_data.kbqs.columns[4]
    asset_member = str(workbook_data.kbqs.iloc[0][asset_dimension])
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension=asset_dimension, member=asset_member, change_pct=0.0)],
    )
    assert result.scenario["最低资本"] == pytest.approx(result.baseline["最低资本"])
    assert result.scenario["实际资本"] == pytest.approx(result.baseline["实际资本"])
    assert result.scenario["综合偿付能力充足率"] == pytest.approx(
        result.baseline["综合偿付能力充足率"]
    )


def test_equity_asset_increase_raises_minimum_capital(workbook_data):
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member="上市普通股票", change_pct=10.0)],
    )
    assert result.scenario["最低资本"] > result.baseline["最低资本"]
    assert result.scenario["综合偿付能力充足率"] < result.baseline["综合偿付能力充足率"]


@pytest.mark.parametrize(
    "asset_type",
    [
        "组合类保险资产管理产品-权益类",
        "组合类保险资产管理产品-混合类",
    ],
)
def test_equity_asset_management_products_raise_minimum_capital(workbook_data, asset_type):
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member=asset_type, change_pct=10.0)],
    )
    assert result.scenario["最低资本"] > result.baseline["最低资本"]
    assert result.scenario["综合偿付能力充足率"] < result.baseline["综合偿付能力充足率"]


def test_investment_property_rights_raise_minimum_capital(workbook_data):
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member="投资性房地产物权", change_pct=10.0)],
    )
    assert result.scenario["最低资本"] > result.baseline["最低资本"]
    assert result.scenario["综合偿付能力充足率"] < result.baseline["综合偿付能力充足率"]


@pytest.mark.parametrize(
    "asset_type",
    [
        "贷款资产",
        "买入返售金融资产",
        "证券投资基金-货币市场",
        "组合类保险资产管理产品-货币市场类",
        "无固定期限资本债券",
    ],
)
def test_unmapped_assets_with_baseline_exposure_use_fallback_risk_factors(workbook_data, asset_type):
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member=asset_type, change_pct=10.0)],
    )
    assert result.scenario["最低资本"] != pytest.approx(result.baseline["最低资本"])


def test_account_level_adjustment_stays_account_scoped(workbook_data):
    account_summary = build_asset_summary(workbook_data.kbqs, "账户")
    account_member = str(account_summary.iloc[0]["账户"])
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="账户", member=account_member, change_pct=5.0)],
    )
    value_delta = result.exposure_summary.loc[
        result.exposure_summary["项目"] == "认可价值", "变化"
    ].iloc[0]
    assert value_delta > 0


def test_factor_table_includes_interest_rate_hedge_assumption(workbook_data):
    result = run_scenario(workbook_data, [])
    local_government_bond = result.risk_rates[
        (result.risk_rates["资产映射"] == "地方政府债")
        & (result.risk_rates["适用范围"] == "久期桶：存量平均")
    ].iloc[0]
    source_factor = workbook_data.interest_factor_table[
        (workbook_data.interest_factor_table["资产类型"] == "地方政府债")
        & (workbook_data.interest_factor_table["久期桶"] == "存量平均")
    ].iloc[0]
    assert local_government_bond["利率风险抵减因子"] == pytest.approx(source_factor["利率风险抵减因子"])


def test_interest_exposure_uses_pv_basis_rate(workbook_data):
    summary = build_asset_summary(workbook_data.kbqs, "资产类型")
    fixed_income_product = summary[
        summary["资产类型"] == "组合类保险资产管理产品-固定收益类"
    ].iloc[0]
    source_factor = workbook_data.interest_factor_table[
        (workbook_data.interest_factor_table["资产类型"] == "组合类保险资产管理产品-固定收益类")
        & (workbook_data.interest_factor_table["久期桶"] == "存量平均")
    ].iloc[0]
    expected_interest_exposure = fixed_income_product["认可价值"] * source_factor["PV口径抵减因子"]
    assert fixed_income_product["利率风险暴露"] == pytest.approx(expected_interest_exposure)
    assert fixed_income_product["利率风险暴露"] < fixed_income_product["认可价值"]
    assert fixed_income_product["权益价格风险暴露"] == pytest.approx(fixed_income_product["认可价值"])


def test_local_government_bond_increase_changes_minimum_capital(workbook_data):
    result = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="地方政府债",
                change_pct=0.0,
                mode="position",
                change_amount=1_000_000_000.0,
            )
        ],
    )
    assert result.scenario["最低资本"] < result.baseline["最低资本"]
    assert result.scenario["综合偿付能力充足率"] > result.baseline["综合偿付能力充足率"]


def test_local_government_bond_reduction_raises_minimum_capital(workbook_data):
    result = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="地方政府债",
                change_pct=0.0,
                mode="position",
                change_amount=-1_000_000_000.0,
            )
        ],
    )
    assert result.scenario["最低资本"] > result.baseline["最低资本"]
    assert result.scenario["综合偿付能力充足率"] < result.baseline["综合偿付能力充足率"]


def test_policy_financial_bond_increase_uses_dynamic_spread_factor(workbook_data):
    result = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="政策性金融债",
                change_pct=0.0,
                mode="position",
                change_amount=1_000_000_000.0,
            )
        ],
    )
    assert result.scenario["最低资本"] < result.baseline["最低资本"]
    spread_factor = workbook_data.spread_factor_table[
        workbook_data.spread_factor_table["资产类型"] == "政策性金融债"
    ].iloc[0]
    assert spread_factor["利差风险因子"] == pytest.approx(
        spread_factor["利差风险MC"] / spread_factor["利差风险暴露"]
    )
    assert 0 < spread_factor["利差风险因子"] < 0.05


def test_position_amount_adjustment_changes_minimum_capital(workbook_data):
    result = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="上市普通股票",
                change_pct=0.0,
                mode="position",
                change_amount=100_000_000.0,
            )
        ],
    )
    assert result.scenario["最低资本"] > result.baseline["最低资本"]
    assert result.adjustment_summary["模块"].iloc[0] == "加仓/减仓/建仓"


def test_price_move_changes_actual_capital(workbook_data):
    equity_rows = workbook_data.kbqs[workbook_data.kbqs["权益价格风险暴露"] > 0]
    asset_member = str(equity_rows.iloc[0]["资产类型"])
    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member=asset_member, change_pct=10.0, mode="price")],
    )
    assert result.scenario["实际资本"] > result.baseline["实际资本"]
    assert result.scenario["认可资产"] > result.baseline["认可资产"]
    assert result.adjustment_summary["模块"].iloc[0] == "上涨/下跌"


def test_minimum_capital_policy_multiplier(workbook_data):
    result = run_scenario(
        workbook_data,
        [],
        PolicyParameters(minimum_capital_multiplier=0.95),
    )
    assert result.scenario["最低资本"] == pytest.approx(
        result.baseline["最低资本"] * 0.95
    )


def test_target_solver_finds_comprehensive_position_solution(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="地方政府债",
        metric="综合偿付能力充足率",
        target_delta_pct_points=1.0,
        mode="position",
        scan_steps=20,
        binary_steps=25,
    )
    assert result.solved
    assert result.change_amount > 0
    assert result.change_pct > 0
    assert result.actual_capital_delta == pytest.approx(0.0)
    assert result.achieved_ratio >= result.target_ratio - 0.0001


def test_target_position_solution_matches_forward_configuration_scenario(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="地方政府债",
        metric="综合偿付能力充足率",
        target_delta_pct_points=1.0,
        mode="position",
        duration_bucket="15-30年",
        scan_steps=20,
        binary_steps=25,
    )
    forward = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="地方政府债",
                mode="position",
                change_pct=0.0,
                change_amount=result.change_amount,
                duration_bucket="15-30年",
            )
        ],
        PolicyParameters(),
    )
    assert result.solved
    assert forward.scenario["综合偿付能力充足率"] == pytest.approx(result.achieved_ratio)
    assert forward.scenario["实际资本"] - forward.baseline["实际资本"] == pytest.approx(result.actual_capital_delta)


def test_target_solver_can_improve_solvency_by_reducing_equity(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="上市普通股票",
        metric="综合偿付能力充足率",
        target_delta_pct_points=5.0,
        mode="position",
        scan_steps=20,
        binary_steps=25,
    )
    assert result.solved
    assert result.change_amount < 0
    assert result.change_pct < 0
    assert result.achieved_ratio >= result.target_ratio - 0.0001


def test_target_equity_reduction_solution_matches_forward_scenario(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="上市普通股票",
        metric="综合偿付能力充足率",
        target_delta_pct_points=5.0,
        mode="position",
        scan_steps=20,
        binary_steps=25,
    )
    forward = run_scenario(
        workbook_data,
        [
            Adjustment(
                dimension="资产类型",
                member="上市普通股票",
                mode="position",
                change_pct=0.0,
                change_amount=result.change_amount,
            )
        ],
        PolicyParameters(),
    )
    assert result.solved
    assert forward.scenario["综合偿付能力充足率"] == pytest.approx(result.achieved_ratio)


def test_target_solver_supports_core_metric(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="政策性金融债",
        metric="核心偿付能力充足率",
        target_delta_pct_points=1.0,
        mode="price",
        scan_steps=20,
        binary_steps=25,
    )
    assert result.solved
    assert result.target_ratio == pytest.approx(result.baseline_ratio + 0.01)


def test_target_solver_duration_bucket_changes_solution(workbook_data):
    short = solve_target_change(
        workbook_data,
        asset_type="地方政府债",
        metric="综合偿付能力充足率",
        target_delta_pct_points=1.0,
        mode="position",
        duration_bucket="7-10年",
        scan_steps=20,
        binary_steps=25,
    )
    long = solve_target_change(
        workbook_data,
        asset_type="地方政府债",
        metric="综合偿付能力充足率",
        target_delta_pct_points=1.0,
        mode="position",
        duration_bucket="15-30年",
        scan_steps=20,
        binary_steps=25,
    )
    assert short.solved and long.solved
    assert short.change_amount != pytest.approx(long.change_amount)


def test_target_solver_zero_change_returns_zero_amount(workbook_data):
    result = solve_target_change(
        workbook_data,
        asset_type="地方政府债",
        metric="综合偿付能力充足率",
        target_delta_pct_points=0.0,
        mode="position",
    )
    assert result.solved
    assert result.change_amount == 0
    assert result.change_pct == 0


def test_waterfall_decomposition_matches_scenario_result(workbook_data):
    from app import _build_capital_waterfall, _build_ratio_waterfall

    result = run_scenario(
        workbook_data,
        [Adjustment(dimension="资产类型", member="上市普通股票", change_pct=-10.0)],
    )
    ratio = _build_ratio_waterfall(result, "综合偿付能力充足率")
    capital = _build_capital_waterfall(result)
    assert ratio.iloc[-1]["标签位置"] == pytest.approx(
        result.scenario["综合偿付能力充足率"] * 100.0
    )
    assert capital.iloc[-1]["标签位置"] == pytest.approx(
        result.scenario["最低资本"] / 100000000.0
    )


def test_bond_market_shock_uses_bucket_duration_and_bp_sign(workbook_data):
    from app import _bond_market_shock_adjustments

    adjustments, summary = _bond_market_shock_adjustments(
        workbook_data,
        {("国债", "15-30年"): 10.0},
    )
    assert len(adjustments) == 1
    assert adjustments[0].member == "国债"
    assert adjustments[0].duration_bucket == "15-30年"
    assert adjustments[0].mode == "price"
    assert adjustments[0].change_amount < 0
    raw = workbook_data.mc_detail
    scoped = raw[(raw["资产类型"] == "国债") & raw["修正久期"].ge(15) & raw["修正久期"].lt(30)]
    values = scoped["账面价值净价"] + scoped["应收利息"]
    expected_duration = (values * scoped["修正久期"]).sum() / values.sum()
    assert summary.iloc[0]["估算久期"] == pytest.approx(expected_duration)
    assert summary.iloc[0]["估算久期"] != pytest.approx(22.5)
    assert summary.iloc[0]["估算价格变化"] == pytest.approx(adjustments[0].change_amount)


def test_equity_market_shock_creates_price_adjustments(workbook_data):
    from app import _equity_market_shock_adjustments
    from solvency_app.scenario import build_asset_summary

    adjustments = _equity_market_shock_adjustments(workbook_data, -5.0)
    members = {item.member for item in adjustments}
    assert "上市普通股票" in members
    assert "证券投资基金-股票型" in members
    assert all(item.mode == "price" for item in adjustments)
    assert all(item.change_amount < 0 for item in adjustments)

    summary = build_asset_summary(workbook_data.kbqs, "资产类型")
    mixed_fund_value = float(summary.loc[summary["资产类型"] == "证券投资基金-混合型", "认可价值"].sum())
    mixed_fund_adjustment = next(item for item in adjustments if item.member == "证券投资基金-混合型")
    assert mixed_fund_adjustment.change_amount == pytest.approx(mixed_fund_value * -0.035)


def test_adjustment_summary_marks_non_bond_duration_not_applicable(workbook_data):
    from app import _display_adjustment_summary

    result = run_scenario(
        workbook_data,
        [
            Adjustment(dimension="资产类型", member="上市普通股票", change_pct=2.0, mode="price"),
            Adjustment(dimension="资产类型", member="地方政府债", change_pct=-1.0, mode="price", duration_bucket="15-30年"),
        ],
    )
    display = _display_adjustment_summary(workbook_data, result.adjustment_summary)
    stock_bucket = display.loc[display["对象"] == "上市普通股票", "久期桶"].iloc[0]
    bond_bucket = display.loc[display["对象"] == "地方政府债", "久期桶"].iloc[0]
    assert stock_bucket == "不适用"
    assert bond_bucket == "15-30年"


def test_sortable_money_df_keeps_numeric_money_columns_for_sorting(workbook_data):
    from app import _sortable_money_df

    summary = build_asset_summary(workbook_data.kbqs, "资产类型")
    display, _ = _sortable_money_df(summary)
    assert pd.api.types.is_numeric_dtype(display["利率风险暴露"])
    local_government_value = summary.loc[summary["资产类型"] == "地方政府债", "利率风险暴露"].iloc[0]
    local_government_display = display.loc[display["资产类型"] == "地方政府债", "利率风险暴露"].iloc[0]
    assert local_government_display == pytest.approx(local_government_value / 100000000.0)


def test_missing_workbook_raises_clear_error():
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"A": [1]}).to_excel(writer, index=False, sheet_name="Sheet1")
    buffer.seek(0)
    with pytest.raises(WorkbookValidationError, match="sheet"):
        load_workbook_data(buffer)
