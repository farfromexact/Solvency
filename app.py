from __future__ import annotations

import math
from dataclasses import asdict, replace

import altair as alt
import pandas as pd
import streamlit as st

from solvency_app.policies import load_policy_overlays
from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.target import solve_target_level
from solvency_app.analysis import ratio_attribution, report_changes, reconciliation, exposure_layers
from solvency_app.calibration import calibration_ready
from solvency_app.portfolio import surface_holdings, tree_inventory, distribution, maturity_profile
from solvency_app.saved_plans import make_plan, serialize_plan, load_plan, compare_plans, switch_plan
from solvency_app.workbench import empty_editor, editor_adjustments, editor_from_adjustments, active_plan_rows
from solvency_app.workbook import (
    WorkbookSource,
    WorkbookValidationError,
    discover_workbook_sources,
    find_workbook_source,
    latest_workbook_source,
    load_baseline_metrics,
    load_workbook_data,
    load_report_snapshot,
)


st.set_page_config(page_title="偿付能力分析与情景工作台", layout="wide")
WORKBOOK_CACHE_VERSION = 5


def _apply_columbia_theme() -> None:
    """Align the app shell with the warm, navy-and-brass stats dashboard theme."""
    st.markdown(
        """
        <style>
        :root {
            --columbia-bg: #F4F3EE;
            --columbia-ink: #1C2433;
            --columbia-navy: #1B3A5C;
            --columbia-sky-soft: #A8D0E4;
            --columbia-brass: #C9A84C;
            --columbia-brass-deep: #A8882E;
            --columbia-cream: #FFFBF3;
            --columbia-border: #D9CDB5;
            --columbia-muted: #5C6B7A;
            --columbia-foam: #FBF6EC;
            --columbia-crimson: #8C3A3A;
        }

        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        }

        .stApp,
        [data-testid="stAppViewContainer"],
        [data-testid="stAppViewContainer"] > .main {
            background: var(--columbia-bg);
            color: var(--columbia-ink);
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        section.main > div {
            position: relative;
            z-index: 1;
        }

        h1, h2, h3 {
            color: var(--columbia-navy);
            font-family: inherit;
            letter-spacing: 0;
            font-weight: 700;
        }

        h1 {
            font-size: clamp(1.6rem, 3.2vw, 2.2rem) !important;
            border-bottom: 2px solid var(--columbia-sky-soft);
            padding-bottom: 0.38rem;
        }

        h2 {
            border-left: 3px solid var(--columbia-brass);
            background: var(--columbia-cream);
            padding-left: 0.65rem;
            box-shadow: 0 3px 12px rgba(27, 58, 92, 0.04);
        }

        h3 {
            color: #2A4A6B;
        }

        p, label, [data-testid="stCaptionContainer"] {
            color: var(--columbia-muted);
            font-family: inherit;
        }

        div[data-testid="stMetric"] {
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid var(--columbia-border);
            border-radius: 6px;
            padding: 0.85rem 1rem;
            box-shadow: 0 2px 10px rgba(27, 58, 92, 0.05);
        }

        div[data-testid="stMetric"] label,
        div[data-testid="stMetricLabel"] {
            color: var(--columbia-navy);
            font-family: inherit;
            letter-spacing: 0;
        }

        div[data-testid="stMetricValue"],
        div[data-testid="stMetricValue"] > div {
            color: var(--columbia-ink);
            font-size: clamp(1.4rem, 2.2vw, 2rem);
            white-space: normal;
            overflow: visible;
            text-overflow: clip;
        }

        [data-testid="stCaptionContainer"] p { color: #526170 !important; font-size: 0.9rem; }
        .block-container { padding-top: 2rem; }
        [data-testid="stMetricValue"] * { white-space: normal !important; text-overflow: clip !important; overflow: visible !important; }
        [data-testid="stMetricLabel"] p { white-space: normal !important; }
        @media (max-width: 1200px) {
            .st-key-overview_metrics [data-testid="stHorizontalBlock"],
            .st-key-scenario_metrics [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            .st-key-overview_metrics [data-testid="stColumn"],
            .st-key-scenario_metrics [data-testid="stColumn"] {
                flex: 1 1 calc(50% - 1rem) !important;
                min-width: calc(50% - 1rem) !important;
            }
        }
        @media (max-width: 650px) {
            .st-key-overview_metrics [data-testid="stColumn"],
            .st-key-scenario_metrics [data-testid="stColumn"] { flex-basis: 100% !important; min-width: 100% !important; }
        }

        div[data-testid="stMetricDelta"] {
            border-radius: 999px;
        }

        div[data-testid="stDataFrame"],
        div[data-testid="stVegaLiteChart"] {
            background: rgba(255, 255, 255, 0.76);
            border: 1px solid var(--columbia-border);
            border-radius: 6px;
            box-shadow: 0 3px 12px rgba(27, 58, 92, 0.04);
        }

        div[data-testid="stDataFrame"] {
            overflow: hidden;
        }

        div[data-testid="stVegaLiteChart"] {
            padding: 0.45rem;
        }

        div[data-testid="stAlert"] {
            border-radius: 6px;
            border-color: rgba(201, 168, 76, 0.55);
            background: rgba(255, 251, 243, 0.85);
        }

        div[data-baseweb="tab-list"] {
            gap: 0.2rem 0.55rem;
            border-bottom: 1px solid var(--columbia-border);
            padding: 0 0.15rem;
        }

        button[data-baseweb="tab"],
        button[data-testid="stTab"] {
            color: var(--columbia-muted);
            flex: 0 0 auto;
            min-height: 2.45rem;
            padding: 0.45rem 0.8rem !important;
            white-space: nowrap;
        }

        button[data-baseweb="tab"][aria-selected="true"],
        button[data-testid="stTab"][aria-selected="true"] {
            color: var(--columbia-navy);
            font-weight: 700;
        }

        div[data-baseweb="tab-highlight"] {
            background-color: var(--columbia-brass);
        }

        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] > div,
        div[data-testid="stNumberInput"] div[data-baseweb="input"] {
            background: var(--columbia-cream);
            border-color: var(--columbia-border);
            color: var(--columbia-ink);
        }

        div[data-baseweb="select"] *,
        div[data-baseweb="input"] input,
        div[data-testid="stNumberInput"] input,
        textarea {
            color: var(--columbia-ink);
        }

        div[data-baseweb="select"] > div:focus-within,
        div[data-baseweb="input"] > div:focus-within {
            border-color: var(--columbia-brass);
            box-shadow: 0 0 0 1px var(--columbia-brass);
        }

        .stButton > button,
        button[kind="primary"] {
            background: var(--columbia-navy);
            color: #FFFDF8 !important;
            border: 1px solid var(--columbia-navy);
            border-radius: 4px;
            font-family: inherit;
            font-weight: 600;
            box-shadow: none;
        }

        .stButton > button:hover,
        button[kind="primary"]:hover {
            background: linear-gradient(180deg, #3A5F88 0%, #243F63 100%);
            color: #FFFDF8 !important;
            border-color: var(--columbia-brass);
        }

        .stButton > button *,
        button[kind="primary"] * {
            color: #FFFDF8 !important;
        }

        hr {
            border-color: var(--columbia-border);
            background: none;
        }

        code, pre, kbd {
            color: var(--columbia-ink);
            background: var(--columbia-cream);
            border-color: var(--columbia-border);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    _apply_columbia_theme()
    st.title("偿付能力分析与情景工作台")
    st.caption("基于现有底稿反推口径的情景估算，不替代监管报送系统或完整偿二代复算引擎。")

    sources = discover_workbook_sources()
    if not sources:
        st.error("origin stats 目录下没有找到可用的月度 Excel 底稿。")
        return

    with st.sidebar:
        source = _render_workbook_selector(sources)
        page = st.radio("导航", ["总览与归因", "情景工作台", "资产与风险", "数据与口径"], key="workspace_page")
        st.caption("原始底稿只读。更换月份或替换同名文件时，会清空已应用的情景。")
    _sync_selected_workbook_state(source)
    try:
        snapshot = _load_snapshot(source.path, source.modified_time_ns, WORKBOOK_CACHE_VERSION)
    except WorkbookValidationError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.exception(exc)
        return

    st.caption(f"报告月份 {source.report_month}　·　底稿时点 {source.timepoint_label}")
    checks = reconciliation(snapshot)
    if (checks["结果"] != "一致").any():
        st.warning("底稿总表存在勾稽差异，请先到“数据与口径”核查；本次不生成情景结果。")
        if page == "情景工作台":
            st.dataframe(checks, hide_index=True, width="stretch")
            return
    if page == "总览与归因":
        history_df, errors = _load_history_metrics(_history_source_specs(sources), WORKBOOK_CACHE_VERSION)
        _render_overview(snapshot, history_df, errors, source, sources)
        return
    try:
        data = _load_data(source.path, source.modified_time_ns, WORKBOOK_CACHE_VERSION)
    except Exception as exc:
        st.error(f"资产明细无法读取：{exc}。总览仍可使用。")
        if page == "数据与口径":
            _render_reconciliation(checks)
            with st.expander("仍可读取的原始总表"):
                st.dataframe(snapshot.s01, hide_index=True, width="stretch")
                st.dataframe(snapshot.s05, hide_index=True, width="stretch")
        return
    if page == "情景工作台":
        _render_workbench(data, source)
    elif page == "资产与风险":
        _render_asset_explorer(data, source)
    else:
        _render_data_quality(data, checks, source)


@st.cache_data(show_spinner="正在解析底稿...")
def _load_data(source, mtime_ns: int, cache_version: int):
    return load_workbook_data(source)


@st.cache_data(show_spinner="正在读取总表...")
def _load_snapshot(source, mtime_ns: int, cache_version: int):
    return load_report_snapshot(source)


@st.cache_data(show_spinner="正在读取历史指标...")
def _load_history_metrics(source_specs: tuple[tuple[str, int, str, str, str, str, str, int], ...], cache_version: int):
    rows = []
    errors = []
    for path, _mtime_ns, source_key, report_month, report_date_label, timepoint_label, file_name, version_rank in source_specs:
        try:
            metrics = load_baseline_metrics(path)
        except Exception as exc:
            errors.append({"底稿": file_name, "错误": str(exc)})
            continue
        rows.append(
            {
                "source_key": source_key,
                "报告月份": report_month,
                "报告月末": report_date_label,
                "底稿时点": timepoint_label,
                "文件名": file_name,
                "version_rank": version_rank,
                "认可资产": metrics.admitted_assets,
                "实际资本": metrics.actual_capital,
                "核心资本": metrics.core_capital,
                "最低资本": metrics.minimum_capital,
                "量化风险最低资本": metrics.quantitative_minimum_capital,
                "核心偿付能力充足率": metrics.core_solvency_ratio,
                "综合偿付能力充足率": metrics.comprehensive_solvency_ratio,
            }
        )
    history = pd.DataFrame(rows)
    if not history.empty:
        history = history.sort_values(["报告月末", "底稿时点", "version_rank", "文件名"]).reset_index(drop=True)
    return history, errors


def _history_source_specs(sources: list[WorkbookSource]) -> tuple[tuple[str, int, str, str, str, str, str, int], ...]:
    return tuple(
        (
            str(source.path),
            source.modified_time_ns,
            source.source_key,
            source.report_month,
            source.report_date_label,
            source.timepoint_label,
            source.path.name,
            source.sort_key[2],
        )
        for source in sources
    )


def _render_workbook_selector(sources: list[WorkbookSource]) -> WorkbookSource:
    st.subheader("数据选择")
    latest = latest_workbook_source(sources)
    month_options = sorted({source.report_month for source in sources})
    default_month = latest.report_month if latest else month_options[-1]
    if st.session_state.get("selected_report_month") not in month_options:
        st.session_state["selected_report_month"] = default_month

    selected_month = st.selectbox(
        "报告月份",
        month_options,
        key="selected_report_month",
    )

    month_sources = [source for source in sources if source.report_month == selected_month]
    month_sources = sorted(month_sources, key=lambda source: source.sort_key)
    timepoint_options = [source.timepoint_label for source in month_sources]
    default_timepoint = timepoint_options[-1]
    if st.session_state.get("selected_timepoint") not in timepoint_options:
        st.session_state["selected_timepoint"] = default_timepoint
    selected_timepoint = st.selectbox(
        "底稿时点",
        timepoint_options,
        key="selected_timepoint",
    )

    source = find_workbook_source(sources, selected_month, selected_timepoint)
    st.caption(f"{len(sources)} 个可用底稿 · {source.path.name}")
    return source


def _sync_selected_workbook_state(source: WorkbookSource) -> None:
    previous_key = st.session_state.get("active_workbook_source_key")
    fingerprint = f"{source.source_key}|{source.modified_time_ns}|{WORKBOOK_CACHE_VERSION}"
    if previous_key and previous_key != fingerprint:
        _clear_target_solver_cache()
        _reset_workbench()
    st.session_state["active_workbook_source_key"] = fingerprint


def _clear_target_solver_cache() -> None:
    for key in ["target_solver_signature", "target_solver_rows"]:
        st.session_state.pop(key, None)


def _render_baseline(data, history_df: pd.DataFrame, source: WorkbookSource) -> None:
    st.subheader("基准指标")
    metrics = data.metrics
    previous = _previous_period_metrics(history_df, source)
    cols = _metric_columns("overview_metrics")
    cols[0].metric(
        "综合充足率",
        _fmt_pct(metrics.comprehensive_solvency_ratio),
        _history_ratio_delta(previous, "综合偿付能力充足率", metrics.comprehensive_solvency_ratio),
    )
    cols[1].metric(
        "核心充足率",
        _fmt_pct(metrics.core_solvency_ratio),
        _history_ratio_delta(previous, "核心偿付能力充足率", metrics.core_solvency_ratio),
    )
    cols[2].metric("实际资本（亿元）", f"{metrics.actual_capital / 1e8:,.2f}", _history_money_delta(previous, "实际资本", metrics.actual_capital), delta_color="off")
    cols[3].metric("最低资本（亿元）", f"{metrics.minimum_capital / 1e8:,.2f}", _history_money_delta(previous, "最低资本", metrics.minimum_capital), delta_color="off")
    st.caption(f"认可资产 {_fmt_money(metrics.admitted_assets)}　·　核心资本 {_fmt_money(metrics.core_capital)}")


def _metric_columns(key: str):
    with st.container(key=key):
        return st.columns(4)


def _previous_period_metrics(history_df: pd.DataFrame, source: WorkbookSource) -> pd.Series | None:
    trend = _trend_history_df(history_df, source)
    if trend.empty:
        return None
    previous = trend[trend["报告月末"].astype(str) < source.report_date_label]
    if previous.empty:
        return None
    return previous.sort_values("报告月末").iloc[-1]


def _history_money_delta(previous: pd.Series | None, column: str, current_value: float) -> str | None:
    if previous is None or column not in previous or pd.isna(previous[column]):
        return None
    return _fmt_money_delta(current_value - float(previous[column]))


def _history_ratio_delta(previous: pd.Series | None, column: str, current_value: float) -> str | None:
    if previous is None or column not in previous or pd.isna(previous[column]):
        return None
    return _fmt_pct_delta(current_value - float(previous[column]))


def _render_overview(snapshot, history, errors, source, sources) -> None:
    _render_baseline(snapshot, history, source)
    previous_row = _previous_period_metrics(history, source)
    if previous_row is not None:
        st.caption(f"指标变化比较期：{previous_row['报告月份']} / {previous_row['底稿时点']}（上一可用月份）")
    earlier = [s for s in sources if s.report_date < source.report_date]
    st.subheader("本期变化归因")
    if not earlier:
        st.info("这是最早的可用底稿，没有更早期间可供比较。")
    else:
        previous_source = st.selectbox("归因比较期", earlier, index=len(earlier) - 1,
                                       format_func=lambda s: f"{s.report_month} / {s.timepoint_label}",
                                       key=f"comparison_{source.source_key}")
        try:
            previous = _load_snapshot(previous_source.path, previous_source.modified_time_ns, WORKBOOK_CACHE_VERSION)
        except Exception as exc:
            st.warning(f"比较期总表无法读取：{exc}")
        else:
            m, p = snapshot.metrics, previous.metrics
            st.info(
                f"较 {previous_source.report_month}，实际资本变化 {_fmt_money_delta(m.actual_capital - p.actual_capital)}，"
                f"最低资本变化 {_fmt_money_delta(m.minimum_capital - p.minimum_capital)}；"
                f"综合充足率变化 {_fmt_pct_delta(m.comprehensive_solvency_ratio - p.comprehensive_solvency_ratio)}，"
                f"核心充足率变化 {_fmt_pct_delta(m.core_solvency_ratio - p.core_solvency_ratio)}。"
            )
            metric = st.radio("归因指标", ["综合偿付能力充足率", "核心偿付能力充足率"], horizontal=True)
            bridge = ratio_attribution(snapshot, previous, metric)
            steps = _waterfall_steps(list(bridge.itertuples(index=False, name=None)))
            st.altair_chart(_waterfall_chart(steps, "百分点"), width="stretch")
            st.caption("来源：两期 S01 期末数。先改变资本、再改变最低资本；两项影响之和与充足率变化一致，不代表交易或收益归因。")
            with st.expander("查看资本结构与风险子项变化"):
                st.markdown("**资本结构（亿元）**")
                capital = report_changes(snapshot.s01, previous.s01)
                selected = capital[capital["项目"].isin(["核心一级资本", "核心二级资本", "附属一级资本", "附属二级资本", "实际资本"])].copy()
                st.dataframe(_numeric_report_change(selected), hide_index=True, width="stretch")
                risk = report_changes(snapshot.s05, previous.s05)
                risk = risk[risk["项目"].str.contains("风险-") & ~risk["项目"].str.contains("合计|分散")]
                risk = risk.sort_values("变化", key=lambda s: s.abs(), ascending=False)
                st.markdown("**风险子项变化（亿元，按绝对变化排序）**")
                st.dataframe(_numeric_report_change(risk), hide_index=True, width="stretch")
                st.caption("风险子项未经跨风险分散汇总，不能直接相加为总最低资本变化。缺失项目保持空值，不补零。")
    _render_history_trend(history, source, errors)


def _numeric_report_change(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for name in ("比较期", "本期", "变化"):
        out[name] = out[name] / 1e8
    return out


def _reset_workbench(preserve_library: bool = False) -> None:
    library = st.session_state.get("wb_library", {}) if preserve_library else {}
    for key in list(st.session_state):
        if key.startswith(("wb_", "market_bp_", "market_equity_pct")):
            st.session_state.pop(key, None)
    if library:
        st.session_state["wb_library"] = library


def _replace_workbench(adjustments, policy) -> None:
    _reset_workbench(preserve_library=True)
    st.session_state["wb_applied"] = {"adjustments": adjustments, "policy": policy}
    st.session_state["wb_seed"] = editor_from_adjustments(adjustments)
    st.session_state["wb_view"] = "构建情景"
    st.session_state["wb_notice"] = "已用倒推方案替换当前情景，下面的正算结果已同步更新。"


def _workbench_policy(default: PolicyParameters) -> PolicyParameters:
    with st.expander("高级口径参数"):
        cols = st.columns(3)
        minimum = cols[0].number_input("最低资本乘数", min_value=0.01, max_value=2.0,
                                       value=default.minimum_capital_multiplier, step=0.01, key="wb_policy_minimum")
        market = cols[1].number_input("市场风险增量乘数", min_value=0.0, max_value=2.0,
                                      value=default.market_risk_multiplier, step=0.01, key="wb_policy_market")
        credit = cols[2].number_input("信用风险增量乘数", min_value=0.0, max_value=2.0,
                                      value=default.credit_risk_multiplier, step=0.01, key="wb_policy_credit")
        sync = st.checkbox("配置规模变化同步计入实际资本和核心资本（简化假设）",
                           value=default.sync_actual_capital_with_assets, key="wb_policy_sync")
        calibrated = st.checkbox("使用底稿校准风险因子（关闭则使用旧简化口径）",
                                 value=default.use_calibrated_factors, key="wb_policy_calibrated")
        st.caption("校准口径：CAL_DETAIL 风险资本 / 同类风险视图认可价值，包含已核对的境外价格和汇率风险；不是监管 RF，也不自动联动表层与底层持仓。")
        st.caption("市场、信用乘数仅影响情景增量，不改变存量风险。最低资本乘数作用于测算总额，不代表自动适用任何监管优惠。")
        st.caption("默认配置情景不计入资本分子；价格变化计入资本分子。暂不自动处理融资负债、税项及损失吸收效应。")
    return PolicyParameters(minimum, market, credit, sync, calibrated)


def _render_workbench(data, source) -> None:
    st.subheader("情景工作台")
    applied = st.session_state.get("wb_applied", {"adjustments": [], "policy": PolicyParameters(use_calibrated_factors=True)})
    adjustments, policy = applied["adjustments"], applied["policy"]
    if notice := st.session_state.pop("wb_notice", None):
        st.success(notice)
    left, right = st.columns([4, 1])
    left.caption(f"当前生效：{len(adjustments)} 条资产调整 · 底稿 {source.report_month} / {source.timepoint_label}")
    right.button("清空本次情景", on_click=_reset_workbench, args=(True,), width="stretch")
    if adjustments:
        st.dataframe(active_plan_rows(adjustments), hide_index=True, width="stretch")
        if len({a.member for a in adjustments}) < len(adjustments):
            st.warning("同一资产存在多条生效调整，将按列表顺序叠加。")
    else:
        st.info("当前没有生效的资产调整，结果为基准加已应用的高级口径参数。")
    with st.expander("当前生效口径参数"):
        st.write({"最低资本乘数": policy.minimum_capital_multiplier,
                  "市场风险增量乘数": policy.market_risk_multiplier,
                  "信用风险增量乘数": policy.credit_risk_multiplier,
                  "配置变化同步资本": policy.sync_actual_capital_with_assets,
                  "风险因子": "底稿校准" if policy.use_calibrated_factors else "旧简化口径"})
    _render_plan_library(data, adjustments, policy)
    view = st.radio("操作方式", ["构建情景", "目标倒推", "方案对比"], horizontal=True, key="wb_view")
    if view == "构建情景":
        _render_plan_editor(data, policy)
    elif view == "目标倒推":
        if policy.use_calibrated_factors and not calibration_ready(data.calibration_checks):
            st.error("底稿因子校准未通过，请先核查或在构建情景页明确切换旧口径。")
            return
        _render_level_solver(data, policy)
    else:
        _render_plan_comparison(data, adjustments, policy)
    st.divider()
    st.caption("以下仅展示“当前生效情景”的正算结果。编辑草稿和未应用的倒推方案不会改变结果。")
    try:
        result = run_scenario(data, adjustments, policy)
    except ValueError as exc:
        st.error(str(exc))
        return
    _render_result(result)
    _render_detail_tabs(data, result)


def _apply_library_plan(raw, data):
    try:
        plan, adjustments, policy = load_plan(raw, st.session_state["active_workbook_source_key"], data)
    except ValueError as exc:
        st.session_state["wb_library_error"] = str(exc)
        return
    _replace_workbench(adjustments, policy)
    st.session_state["wb_notice"] = f"已用“{plan['name']}”替换当前方案并重新计算。"


def _render_plan_library(data, adjustments, policy):
    with st.expander("方案保存、载入与导出"):
        if error := st.session_state.pop("wb_library_error", None):
            st.error(error)
        st.caption("保存的是当前生效输入，不含尚未应用的草稿。同底稿最多保留 6 个方案；切换底稿或关闭会话前可下载 JSON。载入时核对底稿、修改时间和模型版本，并重新正算。")
        fingerprint = st.session_state["active_workbook_source_key"]
        name = st.text_input("方案名称", value="方案 A", max_chars=60, key="wb_plan_name")
        current = make_plan(name, fingerprint, adjustments, policy)
        library = st.session_state.setdefault("wb_library", {})
        if st.button("保存当前生效方案", key="wb_save_plan"):
            if current["name"] in library:
                st.warning("已有同名方案，请改名或先移除旧方案，不自动覆盖。")
            elif len(library) >= 6:
                st.warning("本次会话已保存 6 个方案，请先下载或移除一个。")
            else:
                library[current["name"]] = current
                st.success(f"已保存 {current['name']}")
        st.download_button("下载当前方案 JSON", serialize_plan(current), file_name="solvency-scenario.json", mime="application/json", key="wb_download_plan")
        if library:
            saved_name = st.selectbox("已保存方案", list(library), key="wb_saved_selection")
            st.button("用已保存方案替换当前情景", key="wb_load_saved", on_click=_apply_library_plan,
                      args=(serialize_plan(library[saved_name]), data))
            if st.button("从本次会话移除此方案", key="wb_remove_saved"):
                del library[saved_name]
                st.rerun()
        upload = st.file_uploader("载入已下载的方案 JSON", type=["json"], key="wb_import_plan")
        if upload is not None:
            st.button("核对并应用导入方案", key="wb_apply_import", on_click=_apply_library_plan, args=(upload.getvalue(), data))


def _render_plan_comparison(data, adjustments, policy):
    fingerprint = st.session_state["active_workbook_source_key"]
    baseline = make_plan("原始底稿基准", fingerprint, [], PolicyParameters())
    plans = [baseline, make_plan("当前生效", fingerprint, adjustments, policy)]
    plans.extend(st.session_state.get("wb_library", {}).values())
    st.caption("全部方案按当前底稿重新正算；仅横向比较同一报告期。金额单位亿元，充足率单位 %。")
    try:
        table = compare_plans(plans, fingerprint, data)
        table.loc[0, "风险因子"] = "底稿原值"
    except ValueError as exc:
        st.error(str(exc))
        return
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        col: st.column_config.NumberColumn(format="%.2f") for col in table.columns if "（" in col})
    with st.expander("当前情景的新旧因子口径对照"):
        if st.button("计算新旧口径对照", key="wb_compare_basis"):
            try:
                variants = [make_plan(label, fingerprint, adjustments, replace(policy, use_calibrated_factors=enabled))
                            for label, enabled in [("旧简化", False), ("底稿校准", True)]]
                comparison = compare_plans(variants, fingerprint, data)
                st.dataframe(comparison, hide_index=True, width="stretch")
                st.caption("两行除风险因子开关外保持完全相同的输入与政策乘数；差异不是实际投资收益。")
            except ValueError as exc:
                st.error(str(exc))
    with st.expander("单一资产敏感性"):
        asset = st.selectbox("敏感性资产", sorted(data.kbqs["资产类型"].unique()), key="wb_sensitivity_asset")
        mode = st.radio("敏感性动作", ["价格变化", "加减仓"], horizontal=True, key="wb_sensitivity_mode")
        if st.button("计算 -10% 至 +10% 敏感性", key="wb_sensitivity_run"):
            variants = [make_plan(f"{pct:+d}%", fingerprint, [Adjustment("资产类型", asset, pct, "price" if mode == "价格变化" else "position")], policy)
                        for pct in [-10, -5, 0, 5, 10]]
            try:
                st.dataframe(compare_plans(variants, fingerprint, data), hide_index=True, width="stretch")
                st.caption("各行从底稿基准独立出发，沿用当前生效口径，不叠加当前其他资产调整。")
            except ValueError as exc:
                st.error(str(exc))


def _render_plan_editor(data, policy) -> None:
    options = build_asset_summary(data.kbqs, "资产类型")["资产类型"].tolist()
    seed = st.session_state.get("wb_seed", empty_editor(options[0]))
    epoch = st.session_state.get("wb_epoch", 0)
    with st.expander("快速构建等额资产切换"):
        st.caption("生成卖出 A、买入 B 两行，替换编辑草稿；仍需点击下方“计算并应用”。不计交易成本、税和资金到账时差。")
        sell = st.selectbox("卖出资产", options, key="wb_switch_sell")
        buy = st.selectbox("买入资产", options, index=min(1, len(options)-1), key="wb_switch_buy")
        amount = st.number_input("切换金额（亿元）", min_value=0.01, value=1.0, key="wb_switch_amount")
        if st.button("生成切换草稿", key="wb_switch_generate"):
            try:
                proposed = editor_from_adjustments(switch_plan(sell, buy, amount * 1e8))
                editor_adjustments(proposed, data)
                st.session_state["wb_seed"] = proposed
                st.session_state["wb_epoch"] = epoch + 1
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    # Streamlit removes widget state when a page hides the widget. Keep the last
    # applied market inputs separately so returning to the editor is lossless.
    for key, value in st.session_state.get("wb_saved_market_inputs", {}).items():
        if key not in st.session_state:
            st.session_state[key] = value
    with st.form("wb_editor_form"):
        st.caption("勾选启用后才纳入计算。编辑完成点击“计算并应用”；未提交的输入是草稿。零持仓建仓暂不支持。")
        rows = st.data_editor(seed, num_rows="dynamic", hide_index=True, width="stretch",
                              key=f"wb_editor_{epoch}", column_config={
            "启用": st.column_config.CheckboxColumn(default=True),
            "动作": st.column_config.SelectboxColumn(options=["加减仓", "价格变化"], required=True),
            "资产类型": st.column_config.SelectboxColumn(options=options, required=True, width="medium"),
            "输入方式": st.column_config.SelectboxColumn(options=["亿元", "%"], required=True),
            "数值": st.column_config.NumberColumn(required=True, format="%.4f"),
            "债券久期": st.column_config.SelectboxColumn(options=["存量平均", "<3年", "3-5年", "5-7年", "7-10年", "10-15年", "15-30年", "30年以上"], required=True),
        })
        st.caption("非债券选择“存量平均”；多条调整按行顺序作用于剩余存量。表层与穿透底层尚未完成持仓重建，本轮仍沿用原风险视图估算。")
        st.caption("窄屏可横向滚动表格、折叠左侧栏，或点击表格右上角全屏按钮查看全部输入列。")
        market_enabled = st.checkbox("叠加市场冲击", key="wb_market_enabled", value=False)
        with st.expander("市场冲击输入（仅勾选后生效）"):
            market_adjustments = _render_market_shock_controls(data)
        draft_policy = _workbench_policy(policy)
        submitted = st.form_submit_button("计算并应用", type="primary")
    if submitted:
        try:
            draft = editor_adjustments(rows, data)
            if market_enabled:
                draft.extend(market_adjustments)
            # Round-trip the combined plan through the same validation, including shocks.
            editor_adjustments(editor_from_adjustments(draft), data)
            run_scenario(data, draft, draft_policy)  # Validate before replacing the applied state.
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["wb_applied"] = {"adjustments": draft, "policy": draft_policy}
            st.session_state["wb_saved_market_inputs"] = {
                key: st.session_state[key] for key in st.session_state
                if key == "wb_market_enabled" or key.startswith(("market_bp_", "market_equity_pct"))
            }
            st.session_state["wb_seed"] = rows.copy()
            st.session_state["wb_epoch"] = epoch + 1
            st.session_state["wb_notice"] = "情景已计算并应用。"
            st.rerun()


def _render_level_solver(data, policy) -> None:
    st.caption("倒推从底稿基准及当前已应用口径出发，不叠加已有资产调整。应用时会替换当前方案。")
    metric = st.radio("目标指标", ["综合偿付能力充足率", "核心偿付能力充足率"], horizontal=True, key="wb_target_metric")
    baseline = float(run_scenario(data, [], policy).scenario[metric])
    goal = st.radio("目标方式", ["至少达到", "调整到目标值"], horizontal=True, key="wb_target_goal")
    target = st.number_input("目标充足率（%）", min_value=0.01, max_value=1000.0,
                             value=round(baseline * 100 + 5, 2), step=1.0, key=f"wb_target_level_{metric}")
    st.caption(f"当前基准 {baseline:.2%}；目标相对变化 {target - baseline * 100:+.2f} 个百分点。")
    cols = st.columns([3, 2])
    asset = cols[0].selectbox("资产类型", build_asset_summary(data.kbqs, "资产类型")["资产类型"].tolist(), key="wb_target_asset")
    durations = _duration_options(data, asset) or ["存量平均"]
    bucket = cols[1].selectbox("债券久期", durations, key=f"wb_target_bucket_{asset}")
    signature = (metric, goal, target, asset, bucket, tuple(asdict(policy).values()))
    if st.button("开始倒推", type="primary"):
        with st.spinner("正在搜索两个方向的可行金额..."):
            results = [solve_target_level(data, asset, metric, target / 100, mode, bucket, policy,
                                          "at_least" if goal == "至少达到" else "exact")
                       for mode in ("position", "price")]
        st.session_state["wb_target_results"] = [asdict(r) for r in results]
        st.session_state["wb_target_signature"] = signature
    if "wb_target_results" not in st.session_state:
        return
    if st.session_state.get("wb_target_signature") != signature:
        st.info("参数已变化，请重新倒推。旧方案不可应用。")
        return
    for column, result in zip(st.columns(2), st.session_state["wb_target_results"]):
        with column.container(border=True):
            mode = result["mode"]
            st.markdown("**配置变化方案**" if mode == "position" else "**价格变化方案**")
            if not result["solved"]:
                st.warning("当前搜索范围内未找到可行方案")
                st.write("所需变化金额：—")
                st.caption(result["reason"])
                continue
            st.metric("所需变化（亿元）", f"{result['change_amount'] / 1e8:+,.2f}")
            st.write(f"求解后充足率 **{result['achieved_ratio']:.2%}**")
            st.caption(f"最低资本变化 {_fmt_money_delta(result['minimum_capital_delta'])}；实际资本变化 {_fmt_money_delta(result['actual_capital_delta'])}。")
            st.caption("搜索边界：增量不超过该类存量的 5 倍，减量不超过存量；未施加融资、流动性或投资限额。")
            if abs(result["change_amount"]) < 1e-6:
                st.info("当前基准已满足目标，无需调整。")
                continue
            replay = [Adjustment("资产类型", asset, 0.0, mode, result["change_amount"], bucket)]
            st.button("用此方案替换当前情景", key=f"wb_apply_{mode}", on_click=_replace_workbench,
                      args=(replay, policy), width="stretch")


def _render_asset_explorer(data, source) -> None:
    st.subheader("资产与风险")
    section = st.radio("分析视角", ["表层持仓", "穿透追溯", "风险与久期", "原风险视图"], horizontal=True, key="asset_section")
    if section == "原风险视图":
        _render_raw_risk_view(data)
        return
    if section == "风险与久期":
        _render_calibrated_risks(data)
        return
    if section == "穿透追溯":
        trees = tree_inventory(data.kbqs)
        if trees.empty:
            st.info("当前底稿没有资产树标识，不能可靠连接表层和穿透记录。")
            return
        unique = trees["定位结果"].eq("唯一表层").sum()
        st.write(f"{len(trees)} 个账户内资产树，其中 {unique} 个可定位唯一表层。")
        st.caption("资产树是分组关系；同一树中仅凭层级不能推断每条资产的直接父节点。不会按证券代码去重，也不跨账户连接。")
        st.dataframe(trees, hide_index=True, width="stretch", column_config={"表层价值": st.column_config.NumberColumn(format="%.2f 元")})
        choices = list(range(len(trees)))
        index = st.selectbox("选择资产树", choices, format_func=lambda i: f"{trees.iloc[i]['账户']} · {trees.iloc[i]['资产树标识符']} · {trees.iloc[i]['表层资产']}")
        tree = trees.iloc[index]
        selected = data.kbqs[data.kbqs["账户"].eq(tree["账户"]) & data.kbqs["资产树标识符"].astype(str).eq(tree["资产树标识符"])].copy()
        selected = selected.sort_values(["交易结构层级", "来源行"])
        columns = ["交易结构层级", "资产类型", "证券名称", "认可价值", "穿透情况", "来源行", "来源工作表"]
        st.dataframe(selected[columns], hide_index=True, width="stretch")
        st.caption("以上各层认可价值不可相加；显示金额单位为元。")
        return
    holdings = surface_holdings(data.kbqs)
    total = holdings["认可价值"].sum()
    gap = data.metrics.admitted_assets - total
    st.write(f"表层记录 {len(holdings):,} 条，认可价值 {_fmt_money(total)}；与 S01 认可资产仍相差 {_fmt_money(gap)}。")
    st.warning("本页仅取交易结构层级 = 0，不含穿透底层或层级缺失记录。尚不是完整公司持仓；集中度分母只使用本页所选表层范围。")
    accounts = st.multiselect("账户范围（留空表示全部）", sorted(holdings["账户"].unique()), key="surface_accounts")
    if accounts:
        holdings = holdings[holdings["账户"].isin(accounts)]
    dimension = st.selectbox("结构维度", ["资产类型", "币种分类", "穿透情况", "交易对手", "产品发行人"], key="surface_dimension")
    table = distribution(holdings, dimension)
    _render_distribution(table, dimension)
    with st.expander("到期结构"):
        st.caption("按报告月末计算合同到期天数，不等于可变现期限；无到期日可能是永续资产、权益或缺失，不补成零期限。")
        _render_distribution(maturity_profile(holdings, source.report_date), "到期分组")
    with st.expander("表层明细与来源"):
        query = st.text_input("名称或证券代码", key="surface_search")
        selected = holdings
        if query:
            mask = selected["证券名称"].fillna("").astype(str).str.contains(query, regex=False) | selected["证券代码"].fillna("").astype(str).str.contains(query, regex=False)
            selected = selected[mask]
        st.dataframe(selected, hide_index=True, width="stretch")


def _render_distribution(table, dimension) -> None:
    display = table.copy()
    display["认可价值（亿元）"] = display.pop("认可价值") / 1e8
    display["占所选范围（%）"] = display.pop("占所选范围比例") * 100
    st.dataframe(display, hide_index=True, width="stretch", column_config={
        "认可价值（亿元）": st.column_config.NumberColumn(format="%.2f"),
        "占所选范围（%）": st.column_config.NumberColumn(format="%.2f"),
    })
    if not display.empty:
        chart = alt.Chart(display.head(10)).mark_bar(color="#395B7D").encode(
            x=alt.X("认可价值（亿元）:Q"), y=alt.Y(f"{dimension}:N", sort="-x", axis=alt.Axis(labelLimit=200)),
            tooltip=[dimension, alt.Tooltip("认可价值（亿元）:Q", format=",.2f"), alt.Tooltip("占所选范围（%）:Q", format=".2f")])
        st.altair_chart(chart, width="stretch")


def _render_calibrated_risks(data) -> None:
    st.caption("按原跨表风险视图归集，不将这张表解释为去重持仓的资本贡献；父产品变化不会自动带动其穿透明细。")
    if calibration_ready(data.calibration_checks):
        st.success("风险明细与 S05 及 KBQS 价值口径已勾稽，可以使用底稿校准因子。")
    else:
        st.warning("校准检查未全部通过，校准口径不能用于情景计算。")
    with st.expander("校准勾稽明细"):
        st.dataframe(data.calibration_checks, hide_index=True, width="stretch")
    if not data.risk_factor_table.empty:
        asset = st.selectbox("校准资产类型", sorted(data.risk_factor_table["资产类型"].unique()), key="calibration_asset")
        selected = data.risk_factor_table[data.risk_factor_table["资产类型"].eq(asset)]
        table = selected[["风险类型", "认可价值", "风险资本", "单位价值资本因子", "来源字段"]].copy()
        table["认可价值"] /= 1e8
        table["风险资本"] /= 1e8
        table["单位价值资本因子"] *= 100
        table = table.rename(columns={"认可价值": "风险视图价值（亿元）", "风险资本": "子项资本（亿元）", "单位价值资本因子": "单位价值资本因子（%）"})
        st.dataframe(table, hide_index=True, width="stretch", column_config={col: st.column_config.NumberColumn(format="%.4f") for col in table.columns if "（" in col})
        st.caption("资本因子 = 同类资产该风险 MC 合计 / 同类认可价值；它已反映底稿内结构，不是监管 RF。各子项尚未扣除跨风险分散效应。")
    st.markdown("#### 底稿加权修正久期")
    durations = data.interest_factor_table[["资产类型", "久期桶", "利率风险资产价值", "加权修正久期", "久期覆盖率"]].copy()
    durations["利率风险资产价值"] /= 1e8
    durations = durations.rename(columns={"利率风险资产价值": "全价价值（亿元）"})
    st.dataframe(durations, hide_index=True, width="stretch", column_config={
        "全价价值（亿元）": st.column_config.NumberColumn(format="%.2f"), "加权修正久期": st.column_config.NumberColumn(format="%.3f 年"),
    })
    st.caption("以账面价值净价 + 应收利息加权；缺失久期单列，不用桶中点代替。底稿中的零值保留，不据此认定没有利率风险。价格冲击仍是一阶近似，不包含凸性和负债重估。")
    with st.expander("校准来源明细（元）"):
        st.dataframe(data.cal_detail, hide_index=True, width="stretch")


def _render_raw_risk_view(data) -> None:
    st.subheader("资产与风险")
    st.warning("本页是跨表风险视图，包含表层与穿透记录。认可价值汇总不是公司总持仓，不能将两层相加。")
    dimension = st.radio("汇总维度", ["资产类型", "账户"], horizontal=True)
    layers = pd.to_numeric(data.kbqs.get("交易结构层级"), errors="coerce")
    scope = st.selectbox("查看层次", ["全部风险记录", "表层记录", "穿透底层记录", "层级缺失记录"])
    selected = data.kbqs
    if layers is not None:
        masks = {"表层记录": layers == 0, "穿透底层记录": layers > 0, "层级缺失记录": layers.isna()}
        if scope in masks:
            selected = selected.loc[masks[scope]]
    summary, config = _sortable_money_df(build_asset_summary(selected, dimension))
    st.dataframe(summary, column_config=config, hide_index=True, width="stretch")
    st.caption("利率风险暴露为原模型反推展示值；原始字段在下方明细单独保留。")
    with st.expander("查询资产明细与来源"):
        query = st.text_input("证券名称或代码包含")
        if query:
            mask = selected["证券名称"].fillna("").str.contains(query, regex=False)
            mask |= selected["证券代码"].fillna("").astype(str).str.contains(query, regex=False)
            selected = selected[mask]
        st.caption(f"共 {len(selected):,} 条记录；下表金额单位为元，来源行为 Excel 原始行号。")
        st.dataframe(selected, hide_index=True, width="stretch")


def _render_reconciliation(checks) -> None:
    passed = int(checks["结果"].eq("一致").sum())
    message = f"总表勾稽：{passed} / {len(checks)} 项一致（按列示容差核对）。"
    if passed == len(checks):
        st.success(message)
    else:
        st.warning(message)
    st.dataframe(checks[["检查项", "结果", "差额", "容差", "单位"]], hide_index=True, width="stretch")
    with st.expander("勾稽计算值与底稿值"):
        st.dataframe(checks, hide_index=True, width="stretch")


def _render_data_quality(data, checks, source) -> None:
    st.subheader("数据与口径")
    st.write(f"来源文件：{source.path.name}")
    st.caption(f"{len(data.kbqs):,} 条风险记录 · {data.kbqs['资产类型'].nunique()} 类资产 · {data.kbqs['账户'].nunique()} 类账户")
    _render_reconciliation(checks)
    st.markdown("**表层与穿透记录（分开展示，金额为亿元）**")
    layer_table = exposure_layers(data.kbqs)
    layer_table["认可价值"] /= 1e8
    st.dataframe(layer_table, hide_index=True, width="stretch", column_config={
        "认可价值": st.column_config.NumberColumn("认可价值（亿元）", format="%.2f"),
    })
    gap = data.kbqs["认可价值"].sum() - data.metrics.admitted_assets
    st.warning(f"风险视图全部记录与 S01 认可资产的差额为 {_fmt_money(gap)}。包含穿透层级及口径差异，不认定为数据错误，也不据此调整底稿。")
    missing_codes = data.kbqs["证券代码"].isna().sum()
    st.caption(f"证券代码缺失 {missing_codes:,} 条。来源文件、工作表和行号用于本次定位，不将证券代码视为唯一标识。")
    st.markdown("**当前模型边界**")
    st.write("情景继续采用原跨表风险视图上的增量估算。已接入经勾稽的境外价格、汇率及其他风险平均因子，但不自动联动表层与穿透明细，亦不重算负债现金流、税项、损失吸收效应及政策适用条件。")
    st.caption("一般监管底线：核心充足率 50%、综合充足率 100%；完整达标判断还需风险综合评级等信息，本应用不输出整体合规结论。")
    st.markdown("[监管指标来源：保险公司偿付能力管理规定](https://www.nfra.gov.cn/cn/view/pages/rulesDetail.html?docId=962016&itemId=4214)")
    with st.expander("政策资料摘要（说明性，不自动应用）"):
        st.dataframe(load_policy_overlays(), hide_index=True, width="stretch")
    with st.expander("原始总表与分账户资本"):
        for table in (data.s01, data.s05, data.account_capital):
            st.dataframe(_display_report_money_df(table), hide_index=True, width="stretch")


def _render_history_trend(history_df: pd.DataFrame, source: WorkbookSource, errors: list[dict[str, str]]) -> None:
    if errors:
        failed = "、".join(item["底稿"] for item in errors[:3])
        suffix = "等" if len(errors) > 3 else ""
        st.warning(f"有 {len(errors)} 个历史底稿无法读取趋势指标，已跳过：{failed}{suffix}")

    trend = _trend_history_df(history_df, source)
    if trend.empty:
        st.info("当前没有可展示的历史趋势。")
        return

    st.subheader("历史趋势")

    ratio_tab, capital_tab = st.tabs(["充足率趋势", "资本驱动"])
    with ratio_tab:
        metric = st.radio("趋势指标", ["综合偿付能力充足率", "核心偿付能力充足率"], horizontal=True)
        st.altair_chart(_history_ratio_chart(trend, source, metric), width="stretch")
        st.caption("横轴按可用报告月份排列；缺失月份不补值，变化均指较上一可用月份。")
    with capital_tab:
        st.altair_chart(_history_capital_chart(trend, source), width="stretch")

    with st.expander("查看完整历史表"):
        st.dataframe(_display_history_df(trend), width="stretch", hide_index=True,
                     height=_history_table_height(len(trend)))


def _history_current_and_previous(trend: pd.DataFrame, source: WorkbookSource) -> tuple[pd.Series | None, pd.Series | None]:
    if trend.empty:
        return None, None
    selected = trend[trend["source_key"] == source.source_key]
    current = selected.iloc[-1] if not selected.empty else trend.iloc[-1]
    previous = trend[trend["报告月末"].astype(str) < str(current["报告月末"])]
    if previous.empty:
        return current, None
    return current, previous.sort_values("报告月末").iloc[-1]


def _history_ratio_chart(trend: pd.DataFrame, source: WorkbookSource, metric: str = "综合偿付能力充足率") -> alt.Chart:
    chart_df = _history_ratio_chart_df(trend, source)
    chart_df = chart_df[chart_df["指标"] == metric]
    month_order = trend["报告月份"].tolist()
    minimum = 50.0 if metric == "核心偿付能力充足率" else 100.0
    ratio_domain = [max(0, math.floor((min(chart_df["充足率"].min(), minimum) - 10) / 10) * 10),
                    math.ceil((max(chart_df["充足率"].max(), minimum) + 10) / 10) * 10]
    metric_colors = alt.Scale(
        domain=[metric],
        range=["#1B3A5C" if minimum == 100 else "#3F7CAC"],
    )
    base = alt.Chart(chart_df).encode(
        x=alt.X("报告月份:N", sort=month_order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("充足率:Q", scale=alt.Scale(domain=ratio_domain), title="%"),
        color=alt.Color("指标:N", scale=metric_colors, legend=alt.Legend(title=None, orient="top")),
        tooltip=[
            alt.Tooltip("报告月份:N"),
            alt.Tooltip("底稿时点:N"),
            alt.Tooltip("指标:N"),
            alt.Tooltip("充足率:Q", format=",.2f"),
            alt.Tooltip("较上期变化:Q", format="+.2f"),
        ],
    )
    line = base.mark_line(strokeWidth=3)
    points = base.mark_circle(size=70, opacity=0.9)
    selected_points = (
        alt.Chart(chart_df[chart_df["当前选中"]])
        .mark_circle(size=180, stroke="white", strokeWidth=2)
        .encode(
            x=alt.X("报告月份:N", sort=month_order, title=None),
            y=alt.Y("充足率:Q", scale=alt.Scale(domain=ratio_domain), title="%"),
            color=alt.Color("指标:N", scale=metric_colors, legend=None),
            tooltip=[
                alt.Tooltip("报告月份:N"),
                alt.Tooltip("底稿时点:N"),
                alt.Tooltip("指标:N"),
                alt.Tooltip("充足率:Q", format=",.2f"),
                alt.Tooltip("较上期变化:Q", format="+.2f"),
            ],
        )
    )
    threshold_layers = []
    for label, value, color in [(f"{'核心' if minimum == 50 else '综合'}监管底线 {minimum:.0f}%", minimum, "#8C3A3A")]:
        threshold_df = pd.DataFrame([{"报告月份": month_order[-1], "监管线": label, "充足率": value}])
        threshold_layers.extend(
            [
                alt.Chart(threshold_df)
                .mark_rule(color=color, strokeDash=[5, 5], strokeWidth=1.6, opacity=0.95)
                .encode(y=alt.Y("充足率:Q", scale=alt.Scale(domain=ratio_domain))),
                alt.Chart(threshold_df)
                .mark_text(color=color, align="right", dx=-8, dy=-6, fontSize=12, fontWeight=600)
                .encode(
                    x=alt.X("报告月份:N", sort=month_order),
                    y=alt.Y("充足率:Q", scale=alt.Scale(domain=ratio_domain)),
                    text="监管线:N",
                ),
            ]
        )
    threshold_chart = alt.layer(*threshold_layers)
    return (threshold_chart + line + points + selected_points).properties(height=320)


def _history_ratio_chart_df(trend: pd.DataFrame, source: WorkbookSource) -> pd.DataFrame:
    chart_df = trend[
        [
            "source_key",
            "报告月份",
            "报告月末",
            "底稿时点",
            "核心偿付能力充足率",
            "综合偿付能力充足率",
        ]
    ].copy()
    chart_df = chart_df.melt(
        id_vars=["source_key", "报告月份", "报告月末", "底稿时点"],
        value_vars=["综合偿付能力充足率", "核心偿付能力充足率"],
        var_name="指标",
        value_name="充足率",
    )
    chart_df["充足率"] = chart_df["充足率"] * 100.0
    chart_df["较上期变化"] = chart_df.groupby("指标")["充足率"].diff()
    chart_df["当前选中"] = chart_df["source_key"] == source.source_key
    return chart_df


def _history_capital_chart(trend: pd.DataFrame, source: WorkbookSource) -> alt.Chart:
    chart_df = _history_capital_chart_df(trend, source)
    month_order = trend["报告月份"].tolist()
    selected = chart_df[chart_df["当前选中"]]
    bar = (
        alt.Chart(chart_df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("报告月份:N", sort=month_order, title=None, axis=alt.Axis(labelAngle=0)),
            xOffset=alt.XOffset("指标:N"),
            y=alt.Y("金额:Q", title="亿元"),
            color=alt.Color(
                "指标:N",
                scale=alt.Scale(domain=["实际资本", "最低资本", "量化风险最低资本"], range=["#1B3A5C", "#8C3A3A", "#C08A2D"]),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip("报告月份:N"),
                alt.Tooltip("底稿时点:N"),
                alt.Tooltip("指标:N"),
                alt.Tooltip("金额:Q", format=",.2f"),
                alt.Tooltip("较上期变化:Q", format="+,.2f"),
            ],
        )
    )
    selected_points = (
        alt.Chart(selected)
        .mark_tick(thickness=3, size=26, color="#1C2433")
        .encode(
            x=alt.X("报告月份:N", sort=month_order, title=None),
            xOffset=alt.XOffset("指标:N"),
            y=alt.Y("金额:Q", title="亿元"),
        )
    )
    return (bar + selected_points).properties(height=320)


def _history_capital_chart_df(trend: pd.DataFrame, source: WorkbookSource) -> pd.DataFrame:
    chart_df = trend[
        [
            "source_key",
            "报告月份",
            "报告月末",
            "底稿时点",
            "实际资本",
            "最低资本",
            "量化风险最低资本",
        ]
    ].copy()
    chart_df = chart_df.melt(
        id_vars=["source_key", "报告月份", "报告月末", "底稿时点"],
        value_vars=["实际资本", "最低资本", "量化风险最低资本"],
        var_name="指标",
        value_name="金额",
    )
    chart_df["金额"] = chart_df["金额"] / 100000000.0
    chart_df["较上期变化"] = chart_df.groupby("指标")["金额"].diff()
    chart_df["当前选中"] = chart_df["source_key"] == source.source_key
    return chart_df


def _history_ratio_axis_domain(values: pd.Series) -> list[float]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    anchors = pd.Series([100.0, 120.0, 150.0])
    if numeric.empty:
        numeric = anchors
    else:
        numeric = pd.concat([numeric, anchors], ignore_index=True)
    lower = math.floor((float(numeric.min()) - 10.0) / 10.0) * 10.0
    upper = math.ceil((float(numeric.max()) + 10.0) / 10.0) * 10.0
    return [max(0.0, lower), max(upper, lower + 10.0)]


def _history_table_height(row_count: int) -> int:
    return min(max(92, 38 + row_count * 35), 220)


def _trend_history_df(history_df: pd.DataFrame, source: WorkbookSource) -> pd.DataFrame:
    if history_df.empty:
        return history_df
    ordered = history_df.sort_values(["报告月末", "底稿时点", "version_rank", "文件名"])
    trend = ordered.groupby("报告月份", as_index=False).tail(1)
    selected = ordered[ordered["source_key"] == source.source_key]
    if not selected.empty and not (trend["source_key"] == source.source_key).any():
        trend = pd.concat([trend[trend["报告月份"] != source.report_month], selected], ignore_index=True)
    return trend.sort_values(["报告月末", "底稿时点", "version_rank", "文件名"]).reset_index(drop=True)


def _display_history_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df[
        [
            "报告月份",
            "底稿时点",
            "认可资产",
            "实际资本",
            "最低资本",
            "核心偿付能力充足率",
            "综合偿付能力充足率",
        ]
    ].copy()
    for col in ["认可资产", "实际资本", "最低资本"]:
        out[col] = out[col].map(_fmt_money)
    for col in ["核心偿付能力充足率", "综合偿付能力充足率"]:
        out[col] = out[col].map(_fmt_pct)
    return out


EQUITY_MARKET_SHOCK_BETAS = {
    "上市普通股票": 1.0,
    "优先股": 1.0,
    "证券投资基金-股票型": 1.0,
    "证券投资基金-混合型": 0.7,
    "组合类保险资产管理产品-权益类": 1.0,
    "组合类保险资产管理产品-混合类": 0.7,
}


def _render_market_shock_controls(data) -> list[Adjustment]:
    st.caption(
        "用于模拟更接近市场的冲击：股市用统一涨跌幅；国债和地方政府债按久期 bucket 输入收益率变化(bp)。"
        "利率 bp 为正表示收益率上行、价格下跌。"
    )
    equity_pct = st.number_input(
        "股市涨跌%",
        min_value=-100.0,
        max_value=100.0,
        value=0.0,
        step=1.0,
        key="market_equity_pct",
    )
    adjustments = _equity_market_shock_adjustments(data, float(equity_pct))
    rows = []
    if adjustments:
        rows.extend(
            {
                "冲击类型": "股市涨跌",
                "对象": item.member,
                "久期桶": "不适用",
                "冲击": float(equity_pct) * EQUITY_MARKET_SHOCK_BETAS.get(item.member, 1.0),
                "估算价格变化": item.change_amount,
            }
            for item in adjustments
        )

    st.markdown("##### 国债 / 地方政府债收益率冲击")
    bond_shocks: dict[tuple[str, str], float] = {}
    for asset_type, col in zip(["国债", "地方政府债"], st.columns(2)):
        with col:
            st.markdown(f"**{asset_type}**")
            bucket_rows = _bond_bucket_rows(data, asset_type)
            if bucket_rows.empty:
                st.info("当前底稿无可用久期 bucket。")
                continue
            for _, row in bucket_rows.iterrows():
                bucket = str(row["久期桶"])
                bp = st.number_input(
                    f"{bucket} bp",
                    min_value=-300.0,
                    max_value=300.0,
                    value=0.0,
                    step=1.0,
                    format="%.1f",
                    key=f"market_bp_{asset_type}_{bucket}",
                )
                bond_shocks[(asset_type, bucket)] = float(bp)
    bond_adjustments, bond_summary = _bond_market_shock_adjustments(data, bond_shocks)
    adjustments.extend(bond_adjustments)
    if not bond_summary.empty:
        rows.extend(bond_summary.to_dict("records"))

    if rows:
        st.dataframe(_display_market_shock_df(pd.DataFrame(rows)), width="stretch", hide_index=True)
    else:
        st.info("当前没有非零市场冲击。")
    return adjustments


def _equity_market_shock_adjustments(data, pct: float) -> list[Adjustment]:
    if pct == 0:
        return []
    summary = build_asset_summary(data.kbqs, "资产类型")
    available = set(summary["资产类型"].astype(str))
    adjustments = []
    for asset_type, beta in EQUITY_MARKET_SHOCK_BETAS.items():
        if asset_type not in available:
            continue
        value = float(summary.loc[summary["资产类型"].astype(str) == asset_type, "认可价值"].sum())
        if value == 0:
            continue
        adjustments.append(
            Adjustment(
                dimension="资产类型",
                member=asset_type,
                change_pct=0.0,
                mode="price",
                change_amount=value * pct * beta / 100.0,
            )
        )
    return adjustments


def _bond_market_shock_adjustments(
    data,
    shock_bps: dict[tuple[str, str], float],
) -> tuple[list[Adjustment], pd.DataFrame]:
    adjustments = []
    rows = []
    for (asset_type, bucket), bp in shock_bps.items():
        if bp == 0:
            continue
        scoped = _bond_bucket_rows(data, asset_type)
        match = scoped[scoped["久期桶"].astype(str) == str(bucket)]
        if match.empty:
            continue
        basis_value = float(match.iloc[0]["利率风险资产价值"])
        duration = float(match.iloc[0].get("加权修正久期", float("nan")))
        coverage = float(match.iloc[0].get("久期覆盖率", 0.0))
        if not math.isfinite(duration) or coverage < 1 - 1e-9:
            raise ValueError(f"{asset_type} {bucket} 修正久期不完整，不能计算该组价格冲击")
        price_delta = -duration * bp / 10000.0 * basis_value
        adjustments.append(
            Adjustment(
                dimension="资产类型",
                member=asset_type,
                change_pct=0.0,
                mode="price",
                change_amount=price_delta,
                duration_bucket=bucket,
            )
        )
        rows.append(
            {
                "冲击类型": "收益率bp",
                "对象": asset_type,
                "久期桶": bucket,
                "冲击": bp,
                "估算久期": duration,
                "利率风险资产价值": basis_value,
                "估算价格变化": price_delta,
            }
        )
    return adjustments, pd.DataFrame(rows)


def _bond_bucket_rows(data, asset_type: str) -> pd.DataFrame:
    table = getattr(data, "interest_factor_table", pd.DataFrame())
    if table.empty:
        return pd.DataFrame()
    scoped = table[
        (table["资产类型"].astype(str) == asset_type)
        & (table["久期桶"].astype(str) != "存量平均")
        & (table["久期桶"].astype(str) != "久期待核对")
        & (pd.to_numeric(table["利率风险资产价值"], errors="coerce").fillna(0.0) > 0)
    ].copy()
    if scoped.empty:
        return scoped
    scoped["排序"] = scoped["久期桶"].map(_duration_bucket_order)
    return scoped.sort_values("排序")


def _duration_bucket_midpoint(bucket: str) -> float:
    mapping = {
        "<3年": 1.5,
        "3-5年": 4.0,
        "5-7年": 6.0,
        "7-10年": 8.5,
        "10-15年": 12.5,
        "15-30年": 22.5,
        "30年以上": 30.0,
    }
    return mapping.get(str(bucket), 0.0)


def _duration_bucket_order(bucket: str) -> int:
    order = {
        "<3年": 0,
        "3-5年": 1,
        "5-7年": 2,
        "7-10年": 3,
        "10-15年": 4,
        "15-30年": 5,
        "30年以上": 6,
    }
    return order.get(str(bucket), 99)


def _display_market_shock_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["估算价格变化", "利率风险资产价值"]:
        if col in out.columns:
            out[col] = out[col].map(lambda value: "" if pd.isna(value) else _fmt_money(float(value)))
    if "冲击" in out.columns:
        out["冲击"] = out.apply(
            lambda row: f"{float(row['冲击']):+.2f}%" if row["冲击类型"] == "股市涨跌" else f"{float(row['冲击']):+.1f} bp",
            axis=1,
        )
    if "估算久期" in out.columns:
        out["估算久期"] = out["估算久期"].map(lambda value: "" if pd.isna(value) else f"{float(value):.1f}")
    return out


def _duration_options(data, asset_type: str) -> list[str]:
    table = getattr(data, "interest_factor_table", pd.DataFrame())
    if table.empty:
        return []
    scoped = table[table["资产类型"].astype(str) == str(asset_type)]
    if scoped.empty:
        return []
    available = set(scoped["久期桶"].astype(str).tolist())
    preferred = ["存量平均", "<3年", "3-5年", "5-7年", "7-10年", "10-15年", "15-30年", "30年以上"]
    return [item for item in preferred if item in available]


def _render_result(result) -> None:
    st.subheader("当前生效情景的测算结果")
    comparison = pd.DataFrame(
        [
            {
                "指标": key,
                "基准": result.baseline[key],
                "情景": result.scenario[key],
                "变化": result.scenario[key] - result.baseline[key],
            }
            for key in result.baseline
        ]
    )
    cols = _metric_columns("scenario_metrics")
    for col, name in zip(cols, ["综合偿付能力充足率", "核心偿付能力充足率", "实际资本", "最低资本"]):
        delta = result.scenario[name] - result.baseline[name]
        ratio = "充足率" in name
        value = _fmt_pct(result.scenario[name]) if ratio else f"{result.scenario[name] / 1e8:,.2f}"
        change = (_fmt_pct_delta(delta) if ratio else _fmt_money_delta(delta)) if abs(delta) > 1e-9 else None
        label = name.replace("偿付能力", "") if ratio else f"{name}（亿元）"
        col.metric(label, value, change, delta_color="normal" if ratio else "off")
    with st.expander("查看全部指标对比"):
        st.dataframe(_format_metric_comparison(comparison), width="stretch", hide_index=True)


def _render_detail_tabs(data, result) -> None:
    tabs = st.tabs(["生效调整", "瀑布分析", "贡献分析", "因子假设", "暴露变化", "基准分账户资本", "基准原始报表"])
    with tabs[0]:
        if result.adjustment_summary.empty:
            st.info("当前没有非零情景调整。")
        else:
            st.dataframe(
                _display_adjustment_summary(data, result.adjustment_summary),
                width="stretch",
                hide_index=True,
            )
    with tabs[1]:
        _render_waterfall_analysis(result)
    with tabs[2]:
        st.dataframe(_display_money_df(result.contribution_summary), width="stretch", hide_index=True)
    with tabs[3]:
        st.dataframe(_display_rate_df(result.risk_rates), width="stretch", hide_index=True)
    with tabs[4]:
        st.info("利率风险情景不再把固收资产简单作为正向暴露处理；新增国债、地方政府债等会按资产端利率风险抵减因子降低寿险利率风险最低资本。")
        st.dataframe(_display_money_df(result.exposure_summary), width="stretch", hide_index=True)
    with tabs[5]:
        st.dataframe(_display_report_money_df(data.account_capital), width="stretch", hide_index=True)
    with tabs[6]:
        st.dataframe(_display_report_money_df(data.s01), width="stretch", hide_index=True)
        st.dataframe(_display_report_money_df(data.s05), width="stretch", hide_index=True)


def _render_waterfall_analysis(result) -> None:
    metric = st.radio(
        "充足率指标",
        ["综合偿付能力充足率", "核心偿付能力充足率"],
        horizontal=True,
        key="waterfall_metric",
    )
    ratio_df = _build_ratio_waterfall(result, metric)
    capital_df = _build_capital_waterfall(result)
    st.subheader("充足率变化拆解")
    st.altair_chart(_waterfall_chart(ratio_df, "百分点"), width="stretch")
    st.dataframe(_display_waterfall_df(ratio_df, "pct"), width="stretch", hide_index=True)

    st.subheader("最低资本变化拆解")
    st.altair_chart(_waterfall_chart(capital_df, "亿元"), width="stretch")
    st.dataframe(_display_waterfall_df(capital_df, "money"), width="stretch", hide_index=True)

    st.subheader("风险子项贡献排名")
    ranking = _risk_contribution_ranking(result.contribution_summary)
    if ranking.empty:
        st.info("当前情景没有风险子项最低资本变化。")
    else:
        st.altair_chart(_ranking_chart(ranking), width="stretch")
        st.dataframe(_display_money_df(ranking), width="stretch", hide_index=True)


def _build_ratio_waterfall(result, metric: str) -> pd.DataFrame:
    numerator_key = "实际资本" if metric == "综合偿付能力充足率" else "核心资本"
    baseline_numerator = float(result.baseline[numerator_key])
    scenario_numerator = float(result.scenario[numerator_key])
    baseline_minimum = float(result.baseline["最低资本"])
    scenario_minimum = float(result.scenario["最低资本"])
    baseline_ratio = float(result.baseline[metric]) * 100.0
    capital_only_ratio = _safe_div(scenario_numerator, baseline_minimum) * 100.0
    scenario_ratio = float(result.scenario[metric]) * 100.0
    rows = [
        ("基准充足率", "total", baseline_ratio),
        ("资本变化影响", "relative", capital_only_ratio - baseline_ratio),
        ("最低资本变化影响", "relative", scenario_ratio - capital_only_ratio),
        ("情景充足率", "total", scenario_ratio),
    ]
    return _waterfall_steps(rows)


def _build_capital_waterfall(result) -> pd.DataFrame:
    baseline_minimum = float(result.baseline["最低资本"])
    scenario_minimum = float(result.scenario["最低资本"])
    summary = result.contribution_summary
    market_delta = _risk_delta(summary, "市场风险合计")
    credit_delta = _risk_delta(summary, "信用风险合计")
    residual = scenario_minimum - baseline_minimum - market_delta - credit_delta
    rows = [
        ("基准最低资本", "total", baseline_minimum / 100000000.0),
        ("市场风险变化", "relative", market_delta / 100000000.0),
        ("信用风险变化", "relative", credit_delta / 100000000.0),
        ("相关矩阵/乘数等", "relative", residual / 100000000.0),
        ("情景最低资本", "total", scenario_minimum / 100000000.0),
    ]
    return _waterfall_steps(rows)


def _waterfall_steps(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    cumulative = 0.0
    out = []
    for idx, (label, kind, value) in enumerate(rows):
        if kind == "total":
            start = 0.0
            end = value
            cumulative = value
        else:
            start = cumulative
            end = cumulative + value
            cumulative = end
        out.append(
            {
                "序号": idx,
                "项目": label,
                "类型": kind,
                "变化": value,
                "起点": min(start, end),
                "终点": max(start, end),
                "标签位置": end,
                "标签": f"{value:,.2f}" if kind == "total" else f"{value:+,.2f}",
                "方向": "合计" if kind == "total" else "增加" if value >= 0 else "减少",
            }
        )
    return pd.DataFrame(out)


def _waterfall_chart(df: pd.DataFrame, unit_label: str) -> alt.Chart:
    order = df["项目"].tolist()
    bars = (
        alt.Chart(df)
        .mark_bar(size=46)
        .encode(
            x=alt.X("项目:N", sort=order, title=None),
            y=alt.Y("起点:Q", title=unit_label),
            y2="终点:Q",
            color=alt.Color(
                "方向:N",
                scale=alt.Scale(
                    domain=["增加", "减少", "合计"],
                    range=["#3F7C72", "#A65252", "#395B7D"],
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("项目:N"),
                alt.Tooltip("变化:Q", format=",.2f"),
                alt.Tooltip("方向:N"),
            ],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(dy=-8, fontSize=12, color="#31333f")
        .encode(
            x=alt.X("项目:N", sort=order),
            y=alt.Y("终点:Q"),
            text=alt.Text("标签:N"),
        )
    )
    return (bars + labels).properties(height=320)


def _ranking_chart(df: pd.DataFrame) -> alt.Chart:
    chart_df = df.copy()
    chart_df["变化亿元"] = chart_df["最低资本变化"] / 100000000.0
    chart_df = chart_df.sort_values("变化亿元")
    return (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            y=alt.Y("风险类型:N", sort=chart_df["风险类型"].tolist(), title=None),
            x=alt.X("变化亿元:Q", title="亿元"),
            color=alt.condition(
                alt.datum["变化亿元"] >= 0,
                alt.value("#3F7C72"),
                alt.value("#A65252"),
            ),
            tooltip=[
                alt.Tooltip("风险类型:N"),
                alt.Tooltip("变化亿元:Q", format=",.2f"),
            ],
        )
        .properties(height=300)
    )


def _risk_contribution_ranking(summary: pd.DataFrame) -> pd.DataFrame:
    out = summary[~summary["风险类型"].astype(str).str.endswith("合计")].copy()
    out = out[out["最低资本变化"].abs() > 1e-6]
    if out.empty:
        return out
    return out.sort_values("最低资本变化", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def _risk_delta(summary: pd.DataFrame, risk_type: str) -> float:
    match = summary.loc[summary["风险类型"] == risk_type, "最低资本变化"]
    if match.empty:
        return 0.0
    return float(match.iloc[0])


def _display_waterfall_df(df: pd.DataFrame, unit: str) -> pd.DataFrame:
    out = df[["项目", "类型", "变化", "标签位置"]].copy()
    if unit == "pct":
        out["变化"] = out["变化"].map(lambda value: f"{value:+.2f} 个百分点")
        out["标签位置"] = out["标签位置"].map(lambda value: f"{value:.2f}%")
        out = out.rename(columns={"标签位置": "结果"})
    else:
        out["变化"] = out["变化"].map(lambda value: f"{value:+,.2f} 亿元")
        out["标签位置"] = out["标签位置"].map(lambda value: f"{value:,.2f} 亿元")
        out = out.rename(columns={"标签位置": "结果"})
    return out


def _format_metric_comparison(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ratio_mask = out["指标"].str.contains("充足率")
    for col in ["基准", "情景", "变化"]:
        out[col] = out[col].astype(object)
        out.loc[ratio_mask, col] = out.loc[ratio_mask, col].map(_fmt_pct_delta if col == "变化" else _fmt_pct)
        out.loc[~ratio_mask, col] = out.loc[~ratio_mask, col].map(_fmt_money)
    return out


def _display_money_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _dedupe_columns(df.copy())
    for col in out.columns:
        if pd.api.types.is_numeric_dtype(out[col]):
            if _is_ratio_column(str(col)):
                out[col] = out[col].map(_fmt_pct)
            else:
                out[col] = out[col].map(_fmt_money)
    return out


def _display_report_money_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _dedupe_columns(df.copy())
    text_columns = {"行次", "项目"}
    item_series = out["项目"].astype(str) if "项目" in out.columns else pd.Series("", index=out.index)
    ratio_rows = item_series.str.contains("充足率|比率|比例", na=False)
    for col in out.columns:
        if str(col) in text_columns:
            continue
        numeric = pd.to_numeric(out[col], errors="coerce")
        if numeric.notna().sum() == 0:
            continue
        formatted = out[col].astype(object)
        money_mask = numeric.notna() & ~ratio_rows
        pct_mask = numeric.notna() & ratio_rows
        formatted.loc[money_mask] = numeric.loc[money_mask].map(_fmt_money)
        formatted.loc[pct_mask] = numeric.loc[pct_mask].map(_fmt_pct)
        out[col] = formatted
    return out


def _display_adjustment_summary(data, df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if {"对象", "久期桶"}.issubset(out.columns):
        out["久期桶"] = out.apply(
            lambda row: row["久期桶"] if _duration_options(data, str(row["对象"])) else "不适用",
            axis=1,
        )
    return _display_money_df(out)


def _sortable_money_df(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, st.column_config.Column]]:
    out = _dedupe_columns(df.copy())
    column_config: dict[str, st.column_config.Column] = {}
    for col in out.columns:
        if not pd.api.types.is_numeric_dtype(out[col]):
            continue
        if _is_ratio_column(str(col)):
            out[col] = out[col] * 100.0
            column_config[col] = st.column_config.NumberColumn(format="%.2f%%")
        else:
            out[col] = out[col] / 100000000.0
            column_config[col] = st.column_config.NumberColumn(format="%.2f 亿元")
    return out, column_config


def _display_rate_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _display_money_df(df)
    for col in df.columns:
        if "因子" in str(col) or str(col) == "单位资本率":
            out[col] = df[col].map(lambda value: "" if pd.isna(value) else _fmt_pct(value))
    return out


def _dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    seen: dict[str, int] = {}
    columns = []
    for col in df.columns:
        name = str(col)
        count = seen.get(name, 0)
        if count:
            columns.append(f"{name}_{count + 1}")
        else:
            columns.append(name)
        seen[name] = count + 1
    df.columns = columns
    return df


def _is_ratio_column(column_name: str) -> bool:
    ratio_names = ("充足率", "变化率", "变化比例", "所需变化比例", "单位资本率")
    return any(name in column_name for name in ratio_names)


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _fmt_money(value: float) -> str:
    return f"{value / 100000000:,.2f} 亿元"


def _fmt_money_delta(value: float) -> str:
    return f"{value / 100000000:+,.2f} 亿元"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_pct_delta(value: float) -> str:
    return f"{value * 100:+.2f} 个百分点"


if __name__ == "__main__":
    main()
