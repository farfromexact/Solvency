from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.target import solve_target_change
from solvency_app.workbook import WorkbookValidationError, load_workbook_data


WORKBOOK = Path(__file__).resolve().parents[1] / "1000_20260430_20260512.xlsx"


@pytest.fixture(scope="module")
def workbook_data():
    return load_workbook_data(WORKBOOK)


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


def test_missing_workbook_raises_clear_error():
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"A": [1]}).to_excel(writer, index=False, sheet_name="Sheet1")
    buffer.seek(0)
    with pytest.raises(WorkbookValidationError, match="sheet"):
        load_workbook_data(buffer)
