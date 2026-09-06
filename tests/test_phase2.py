from dataclasses import replace
import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
from solvency_app.calibration import build_calibration, calibration_ready, FOREIGN_FIXED
from solvency_app.portfolio import surface_holdings, tree_inventory, distribution, maturity_profile
from solvency_app.saved_plans import make_plan, serialize_plan, load_plan, compare_plans, switch_plan
from solvency_app.scenario import Adjustment, PolicyParameters, run_scenario
from solvency_app.workbook import load_workbook_data, load_report_snapshot, discover_workbook_sources, _build_interest_factor_table


@pytest.fixture(scope="module")
def data():
    return load_workbook_data(discover_workbook_sources()[-1].path)


def test_calibration_reconciles_and_recovers_capital(data):
    assert calibration_ready(data.calibration_checks)
    table = data.risk_factor_table
    assert ((table["单位价值资本因子"] * table["认可价值"]) - table["风险资本"]).abs().max() < 1e-4
    zero = run_scenario(data, [], PolicyParameters(use_calibrated_factors=True))
    assert zero.scenario["最低资本"] == pytest.approx(data.metrics.minimum_capital)


def test_calibrated_equity_moves_foreign_and_fx_risks(data):
    result = run_scenario(data, [Adjustment("资产类型", "上市普通股票", 10)], PolicyParameters(use_calibrated_factors=True))
    changes = result.contribution_summary.set_index("风险类型")["最低资本变化"]
    factors = data.risk_factor_table[data.risk_factor_table["资产类型"].eq("上市普通股票")].set_index("风险类型")
    for risk in ["权益价格风险", "境外权益价格风险", "汇率风险"]:
        assert changes[risk] == pytest.approx(factors.loc[risk, "风险资本"] * .1)
        assert changes[risk] > 0


def test_invalid_calibration_cannot_fall_back_silently(data):
    checks = data.calibration_checks.copy()
    checks.loc[0, "结果"] = "需核查"
    damaged = replace(data, calibration_checks=checks)
    with pytest.raises(ValueError, match="勾稽未通过"):
        run_scenario(damaged, [], PolicyParameters(use_calibrated_factors=True))
    assert run_scenario(damaged, []).scenario["最低资本"] == data.metrics.minimum_capital


def test_active_missing_mc_is_invalid_even_if_total_is_zero(data):
    detail = data.cal_detail.copy()
    index = detail.index[detail["是否计算汇率风险"].eq("是")][0]
    detail.loc[index, "MC.4"] = float("nan")
    _, checks = build_calibration(detail, data.kbqs, data.s05)
    assert not calibration_ready(checks)
    assert checks.set_index("检查项").loc["汇率风险 有效风险资本", "结果"] == "需核查"


def test_foreign_capital_not_arbitrarily_split(data):
    s05 = data.s05.copy()
    s05.loc[s05["项目"].eq(FOREIGN_FIXED), "期末数"] = 100
    _, checks = build_calibration(data.cal_detail, data.kbqs, s05)
    assert not calibration_ready(checks)
    assert "境外价格明细可区分固收与权益" in checks["检查项"].tolist()


def test_calibrated_target_replays(data):
    from solvency_app.target import solve_target_level
    policy = PolicyParameters(use_calibrated_factors=True)
    result = solve_target_level(data, "地方政府债", "综合偿付能力充足率", 1.57, "position", policy=policy)
    assert result.solved
    replay = run_scenario(data, [Adjustment("资产类型", "地方政府债", 0, "position", result.change_amount)], policy)
    assert replay.scenario["综合偿付能力充足率"] == pytest.approx(result.achieved_ratio)


def test_weighted_duration_and_missing_coverage():
    raw = pd.DataFrame({"资产类型": ["国债"] * 3, "修正久期": [16., 20., float("nan")],
                        "利率风险资产价值": [100., 300., 100.], "PV基础": [100., 300., 100.], "资产端利率风险MC": [10., 30., 10.]})
    table = _build_interest_factor_table(raw)
    known = table[table["久期桶"].eq("15-30年")].iloc[0]
    total = table[table["久期桶"].eq("存量平均")].iloc[0]
    assert known["加权修正久期"] == 19
    assert known["久期覆盖率"] == 1
    assert total["久期覆盖率"] == .8
    assert table[table["久期桶"].eq("久期待核对")]["加权修正久期"].isna().all()


def test_bond_price_bp_sign_symmetry(data):
    up, _ = app._bond_market_shock_adjustments(data, {("国债", "15-30年"): 10})
    down, _ = app._bond_market_shock_adjustments(data, {("国债", "15-30年"): -10})
    assert up[0].change_amount == -down[0].change_amount
    assert up[0].change_amount < 0


def test_surface_and_tree_totals_do_not_add_parents_and_children(data):
    surface = surface_holdings(data.kbqs)
    assert len(surface) == 4605
    assert surface["认可价值"].sum() == pytest.approx(809596660500.11)
    trees = tree_inventory(data.kbqs)
    assert len(trees) == 205
    assert trees["表层记录数"].eq(1).all()
    assert trees["底层记录数"].sum() == 5596
    assert distribution(surface, "币种分类")["占所选范围比例"].sum() == pytest.approx(1)


def test_tree_keys_preserve_accounts_and_flag_ambiguous_roots():
    raw = pd.DataFrame({"账户": ["A", "A", "A", "B"], "资产树标识符": ["T"] * 4,
                        "交易结构层级": [0, 0, 1, 1], "认可价值": [100, 100, 20, 30]})
    trees = tree_inventory(raw).set_index("账户")
    assert len(trees) == 2
    assert trees.loc["A", "定位结果"] == "多个表层"
    assert trees.loc["B", "定位结果"] == "无表层"
    assert trees["表层价值"].isna().all()


def test_maturity_missing_not_zero():
    raw = pd.DataFrame({"到期日": [None, "2026-06-01", "2027-01-01"], "认可价值": [10, 20, 30]})
    table = maturity_profile(raw, "2026-07-31").set_index("到期分组")
    assert table.loc["无到期日 / 未提供", "认可价值"] == 10
    assert table.loc["已到期 / 待核对", "认可价值"] == 20
    assert table.loc["1年以内", "认可价值"] == 30


def test_saved_plan_roundtrip_compare_and_source_guard(data):
    policy = PolicyParameters(use_calibrated_factors=True)
    adjustments = [Adjustment("资产类型", "上市普通股票", -5, "price")]
    plan = make_plan("压力 A", "same-source", adjustments, policy)
    valid, recovered, p = load_plan(serialize_plan(plan), "same-source", data)
    assert recovered == adjustments and p == policy and valid == plan
    table = compare_plans([make_plan("基准", "same-source", [], policy), plan], "same-source", data)
    assert table.iloc[1]["实际资本（亿元）"] < table.iloc[0]["实际资本（亿元）"]
    with pytest.raises(ValueError, match="底稿"):
        load_plan(serialize_plan(plan), "different-source", data)


@pytest.mark.parametrize("damage", ["nan", "negative_multiplier", "code", "version", "unknown_field", "oversell"])
def test_import_rejects_invalid_payloads(data, damage):
    plan = make_plan("A", "f", [Adjustment("资产类型", "上市普通股票", 1)], PolicyParameters())
    if damage == "nan":
        plan["adjustments"][0]["change_pct"] = float("nan")
    elif damage == "negative_multiplier":
        plan["policy"]["minimum_capital_multiplier"] = -1
    elif damage == "code":
        plan["adjustments"][0]["change_pct"] = "__import__('os')"
    elif damage == "version":
        plan["model_version"] = "old"
    elif damage == "unknown_field":
        plan["adjustments"][0]["surprise"] = 1
    else:
        plan["adjustments"][0]["change_pct"] = -150
    with pytest.raises(ValueError):
        load_plan(json.dumps(plan).encode(), "f", data)


def test_equal_value_switch_preserves_assets(data):
    adjustments = switch_plan("上市普通股票", "地方政府债", 1e8)
    result = run_scenario(data, adjustments, PolicyParameters(use_calibrated_factors=True))
    assert result.scenario["认可资产"] == pytest.approx(result.baseline["认可资产"])
    assert result.scenario["实际资本"] == result.baseline["实际资本"]
    assert result.scenario["最低资本"] != result.baseline["最低资本"]


def _main():
    import app
    app.main()


@pytest.mark.parametrize("section", ["表层持仓", "穿透追溯", "风险与久期", "原风险视图"])
def test_asset_sections_render(monkeypatch, data, section):
    snapshot = load_report_snapshot(discover_workbook_sources()[-1].path)
    monkeypatch.setattr(app, "_load_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(app, "_load_data", lambda *args: data)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = "资产与风险"
    at.session_state["asset_section"] = section
    at.run(timeout=30)
    assert not at.exception
    assert len(at.dataframe) >= 1


def test_ui_save_compare_apply_and_source_reset(monkeypatch, data):
    snapshot = load_report_snapshot(discover_workbook_sources()[-1].path)
    monkeypatch.setattr(app, "_load_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(app, "_load_data", lambda *args: data)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = "情景工作台"
    at.run(timeout=30)
    assert not at.exception
    at.button(key="wb_save_plan").click().run()
    assert len(at.session_state["wb_library"]) == 1
    at.radio(key="wb_view").set_value("方案对比").run()
    assert not at.exception
    at.button(key="wb_compare_basis").click().run()
    assert not at.exception
    at.button(key="wb_load_saved").click().run()
    assert not at.exception
    assert at.session_state["wb_view"] == "构建情景"
    assert len(at.session_state["wb_library"]) == 1
    assert at.session_state["wb_applied"]["policy"].use_calibrated_factors
    at.selectbox(key="selected_report_month").set_value("2026-06").run()
    assert not at.exception
    assert not at.session_state["wb_library"]
