from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.workbook import WorkbookValidationError, load_workbook_data


WORKBOOK = Path(__file__).resolve().parents[1] / "1000_20260331_20260413.xlsx"


@pytest.fixture(scope="module")
def workbook_data():
    return load_workbook_data(WORKBOOK)


def test_loads_baseline_metrics(workbook_data):
    assert workbook_data.metrics.admitted_assets == pytest.approx(737283390628.15)
    assert workbook_data.metrics.minimum_capital == pytest.approx(47793879465.9)
    assert workbook_data.metrics.comprehensive_solvency_ratio == pytest.approx(1.2970, abs=0.0001)


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
    assert local_government_bond["利率风险抵减因子"] == pytest.approx(0.0849715831)


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


def test_missing_workbook_raises_clear_error():
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"A": [1]}).to_excel(writer, index=False, sheet_name="Sheet1")
    buffer.seek(0)
    with pytest.raises(WorkbookValidationError, match="sheet"):
        load_workbook_data(buffer)
