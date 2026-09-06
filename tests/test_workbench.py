from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
from solvency_app.analysis import ratio_attribution, reconciliation, exposure_layers, report_changes
from solvency_app.scenario import Adjustment, PolicyParameters, run_scenario
from solvency_app.target import solve_target_level
from solvency_app.workbench import empty_editor, editor_adjustments, editor_from_adjustments
from solvency_app.workbook import (
    WorkbookValidationError, discover_workbook_sources, load_report_snapshot, load_workbook_data,
    _extract_metrics,
    _read_kbqs_sheet,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCES = discover_workbook_sources(ROOT / "origin stats")


@pytest.fixture(scope="module")
def latest():
    return load_workbook_data(SOURCES[-1].path)


@pytest.fixture(scope="module")
def snapshots():
    return [load_report_snapshot(source.path) for source in SOURCES]


def test_all_periods_reconcile(snapshots):
    for snapshot in snapshots:
        assert reconciliation(snapshot)["结果"].eq("一致").all(), snapshot.source_name


def test_latest_raw_fields_preserved(latest):
    assert len(latest.kbqs) == 10234
    assert {"穿透情况", "交易结构层级", "资产树标识符", "来源工作表", "来源行", "原始利率风险暴露"}.issubset(latest.kbqs.columns)
    assert latest.kbqs["来源行"].iloc[0] == 2
    assert latest.kbqs["来源行"].is_unique
    assert latest.kbqs["原始利率风险暴露"].eq(0).any()
    layers = exposure_layers(latest.kbqs)
    assert layers["记录数"].sum() == len(latest.kbqs)
    assert layers["认可价值"].sum() == pytest.approx(latest.kbqs["认可价值"].sum())


@pytest.mark.parametrize("month", ["2026-02", "2026-03"])
def test_historical_missing_exposure_is_not_silently_zeroed(month):
    source = next(source for source in SOURCES if source.report_month == month)
    with pd.ExcelFile(source.path) as excel:
        sheet = next(name for name in excel.sheet_names if name.startswith("KBQS_V_"))
        with pytest.raises(WorkbookValidationError, match="汇率风险暴露 有 4 条.*Excel 行号"):
            _read_kbqs_sheet(excel, sheet)


@pytest.mark.parametrize("metric", ["综合偿付能力充足率", "核心偿付能力充足率"])
def test_exact_period_bridge(snapshots, metric):
    bridge = ratio_attribution(snapshots[-1], snapshots[-2], metric)
    assert bridge.iloc[0]["数值"] + bridge[bridge["类型"] == "relative"]["数值"].sum() == pytest.approx(bridge.iloc[-1]["数值"])


def test_missing_report_item_is_not_zero():
    frame = lambda value: pd.DataFrame({"项目": [value], "期末数": [20.0]})
    assert pd.isna(report_changes(frame("new"), frame("old")).iloc[0]["变化"])


@pytest.mark.parametrize("value", [None, float("inf"), "bad", 0.0])
def test_invalid_minimum_capital_blocks(value):
    # Independent synthetic report; no workbook is edited.
    report = pd.DataFrame({"项目": ["认可资产", "认可负债", "实际资本", "核心一级资本", "核心二级资本", "最低资本", "量化风险最低资本"],
                           "期末数": [100, 40, 60, 40, 0, value, 50]})
    with pytest.raises(WorkbookValidationError):
        _extract_metrics(report)


def test_cache_keys_include_mtime_and_parser_version(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "load_workbook_data", lambda path: calls.append(path) or len(calls))
    app._load_data.clear()
    assert app._load_data("probe", 1, 4) == 1
    assert app._load_data("probe", 1, 4) == 1
    assert app._load_data("probe", 2, 4) == 2
    assert app._load_data("probe", 2, 5) == 3
    app._load_data.clear()


def test_editor_enable_round_trip_and_validation(latest):
    rows = empty_editor("上市普通股票")
    rows.loc[0, "数值"] = 1.0
    enabled = editor_adjustments(rows, latest)
    assert enabled[0].change_amount == 1e8
    assert editor_adjustments(editor_from_adjustments(enabled), latest) == enabled
    rows.loc[0, "启用"] = False
    assert editor_adjustments(rows, latest) == []
    rows.loc[0, "启用"] = True
    rows.loc[0, "数值"] = -1e10
    with pytest.raises(ValueError, match="超过"):
        editor_adjustments(rows, latest)
    rows.loc[0, "数值"] = 1.0
    rows.loc[0, "债券久期"] = "15-30年"
    with pytest.raises(ValueError, match="不支持"):
        editor_adjustments(rows, latest)


def test_target_lower_bound_already_met(latest):
    result = solve_target_level(latest, "地方政府债", "综合偿付能力充足率", 1.5, "position")
    assert result.solved and result.change_amount == 0
    assert result.achieved_ratio >= result.target_ratio


def test_target_replay_matches_latest_forward(latest):
    result = solve_target_level(latest, "地方政府债", "综合偿付能力充足率", 1.57, "position")
    replay = run_scenario(latest, [Adjustment("资产类型", "地方政府债", 0.0, "position", result.change_amount)])
    assert result.solved
    assert result.achieved_ratio == pytest.approx(replay.scenario["综合偿付能力充足率"])
    assert result.achieved_ratio == pytest.approx(1.57, abs=1e-7)


def _main():
    import app
    app.main()


def test_overview_does_not_load_asset_details(monkeypatch, snapshots):
    by_path = {str(source.path): snap for source, snap in zip(SOURCES, snapshots)}
    monkeypatch.setattr(app, "_load_snapshot", lambda source, *args: by_path[str(source.resolve())])
    monkeypatch.setattr(app, "_load_data", lambda *args: pytest.fail("overview must not load full assets"))
    history, errors = app._load_history_metrics(app._history_source_specs(SOURCES), app.WORKBOOK_CACHE_VERSION)
    monkeypatch.setattr(app, "_load_history_metrics", lambda *args: (history, errors))
    at = AppTest.from_function(_main).run(timeout=30)
    assert not at.exception
    assert any("152.17%" == m.value for m in at.metric)
    at.radio(key="workspace_page").set_value("总览与归因").run()
    assert not at.exception


def test_workbench_apply_target_reset_and_source_change(monkeypatch, latest, snapshots):
    by_path = {str(source.path): snap for source, snap in zip(SOURCES, snapshots)}
    monkeypatch.setattr(app, "_load_snapshot", lambda source, *args: by_path[str(source.resolve())])
    monkeypatch.setattr(app, "_load_data", lambda *args: latest)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = "情景工作台"
    at.run(timeout=30)
    assert not at.exception
    # Seed an editable plan; form edits must not mutate the applied plan before submission.
    at.session_state["wb_seed"] = editor_from_adjustments([Adjustment("资产类型", "上市普通股票", 0, "position", 1e8)])
    at.session_state["wb_epoch"] = 1
    at.run()
    assert "wb_applied" not in at.session_state.filtered_state
    next(b for b in at.button if b.label == "计算并应用").click().run()
    assert not at.exception
    assert len(at.session_state["wb_applied"]["adjustments"]) == 1
    at.radio(key="wb_view").set_value("目标倒推").run()
    assert not at.exception
    next(b for b in at.button if b.label == "开始倒推").click().run(timeout=30)
    assert not at.exception
    at.button(key="wb_apply_position").click().run()
    assert not at.exception
    assert at.session_state["wb_applied"]["adjustments"][0].member == "地方政府债"
    assert at.session_state["wb_view"] == "构建情景"
    next(b for b in at.button if b.label == "清空本次情景").click().run()
    assert not at.exception
    assert "wb_applied" not in at.session_state.filtered_state
    at.session_state["wb_applied"] = {"adjustments": [], "policy": PolicyParameters()}
    at.selectbox(key="selected_report_month").set_value("2026-06").run()
    assert not at.exception
    assert "wb_applied" not in at.session_state.filtered_state


def test_same_name_replacement_resets_applied_state():
    def render():
        import app
        import streamlit as st
        from solvency_app.workbook import discover_workbook_sources
        from dataclasses import replace
        source = discover_workbook_sources()[-1]
        if st.session_state.get("replace_source"):
            source = replace(source, modified_time_ns=source.modified_time_ns + 1)
        app._sync_selected_workbook_state(source)
    at = AppTest.from_function(render).run()
    at.session_state["wb_applied"] = {"test": True}
    at.session_state["replace_source"] = True
    at.run()
    assert not at.exception
    assert "wb_applied" not in at.session_state.filtered_state


def test_applied_market_inputs_survive_navigation(monkeypatch, latest, snapshots):
    by_path = {str(source.path): snap for source, snap in zip(SOURCES, snapshots)}
    monkeypatch.setattr(app, "_load_snapshot", lambda source, *args: by_path[str(source.resolve())])
    monkeypatch.setattr(app, "_load_data", lambda *args: latest)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = "情景工作台"
    at.run(timeout=30)
    at.checkbox(key="wb_market_enabled").check()
    at.number_input(key="market_equity_pct").set_value(-5.0)
    next(b for b in at.button if b.label == "计算并应用").click().run()
    assert not at.exception
    applied = at.session_state["wb_applied"]["adjustments"]
    assert applied
    at.radio(key="wb_view").set_value("目标倒推").run()
    at.radio(key="wb_view").set_value("构建情景").run()
    assert at.checkbox(key="wb_market_enabled").value
    assert at.number_input(key="market_equity_pct").value == -5.0
    next(b for b in at.button if b.label == "计算并应用").click().run()
    assert not at.exception
    assert at.session_state["wb_applied"]["adjustments"] == applied


@pytest.mark.parametrize("page", ["资产与风险", "数据与口径"])
def test_data_pages_render(monkeypatch, latest, snapshots, page):
    by_path = {str(source.path): snap for source, snap in zip(SOURCES, snapshots)}
    monkeypatch.setattr(app, "_load_snapshot", lambda source, *args: by_path[str(source.resolve())])
    monkeypatch.setattr(app, "_load_data", lambda *args: latest)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = page
    at.run(timeout=30)
    assert not at.exception
    assert len(at.dataframe) >= 2


def test_data_checks_remain_available_when_asset_details_fail(monkeypatch, snapshots):
    by_path = {str(source.path): snap for source, snap in zip(SOURCES, snapshots)}
    monkeypatch.setattr(app, "_load_snapshot", lambda source, *args: by_path[str(source.resolve())])
    def fail(*args):
        raise WorkbookValidationError("测试用无效风险暴露")
    monkeypatch.setattr(app, "_load_data", fail)
    at = AppTest.from_function(_main)
    at.session_state["workspace_page"] = "数据与口径"
    at.run(timeout=30)
    assert not at.exception
    assert at.error and "无效风险暴露" in at.error[0].value
    assert any("5 / 5" in item.value for item in at.success)
    assert len(at.dataframe) == 4
