from __future__ import annotations

import math

import altair as alt
import pandas as pd
import streamlit as st

from solvency_app.policies import load_policy_overlays
from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.target import solve_target_change
from solvency_app.workbook import (
    WorkbookSource,
    WorkbookValidationError,
    discover_workbook_sources,
    find_workbook_source,
    latest_workbook_source,
    load_baseline_metrics,
    load_workbook_data,
)


st.set_page_config(page_title="偿付能力资产配置情景测算", layout="wide")
WORKBOOK_CACHE_VERSION = 3


def main() -> None:
    st.title("偿付能力资产配置情景测算")
    st.caption("基于现有底稿反推口径的情景估算，不替代监管报送系统或完整偿二代复算引擎。")
    st.caption("风险模型：包含权益类/混合类资管产品映射和底稿风险暴露兜底。")

    sources = discover_workbook_sources()
    if not sources:
        st.error("origin stats 目录下没有找到可用的月度 Excel 底稿。")
        return

    source = _render_workbook_selector(sources)
    _sync_selected_workbook_state(source)
    history_df, history_errors = _load_history_metrics(_history_source_specs(sources), WORKBOOK_CACHE_VERSION)

    try:
        data = _load_data(source.path, source.modified_time_ns, WORKBOOK_CACHE_VERSION)
    except WorkbookValidationError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.exception(exc)
        return

    _render_baseline(data, history_df, source)
    _render_history_trend(history_df, source, history_errors)
    policy = _render_policy_controls()
    adjustments = _render_scenario_controls(data, policy, source)
    result = run_scenario(data, adjustments, policy)
    _render_result(result)
    _render_detail_tabs(data, result)


@st.cache_data(show_spinner="正在解析底稿...")
def _load_data(source, _mtime_ns: int, _cache_version: int):
    return load_workbook_data(source)


@st.cache_data(show_spinner="正在读取历史指标...")
def _load_history_metrics(source_specs: tuple[tuple[str, int, str, str, str, str, str, int], ...], _cache_version: int):
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

    cols = st.columns([1.1, 1.2, 3])
    selected_month = cols[0].selectbox(
        "报告月份",
        month_options,
        index=month_options.index(st.session_state["selected_report_month"]),
        key="selected_report_month",
    )

    month_sources = [source for source in sources if source.report_month == selected_month]
    month_sources = sorted(month_sources, key=lambda source: source.sort_key)
    timepoint_options = [source.timepoint_label for source in month_sources]
    default_timepoint = timepoint_options[-1]
    if st.session_state.get("selected_timepoint") not in timepoint_options:
        st.session_state["selected_timepoint"] = default_timepoint
    selected_timepoint = cols[1].selectbox(
        "底稿时点",
        timepoint_options,
        index=timepoint_options.index(st.session_state["selected_timepoint"]),
        key="selected_timepoint",
    )

    source = find_workbook_source(sources, selected_month, selected_timepoint)
    cols[2].metric("当前测算底稿", f"{source.report_month} / {source.timepoint_label}")
    st.caption(f"当前文件：{source.path.name}；origin stats 中共 {len(sources)} 个可用底稿。")
    return source


def _sync_selected_workbook_state(source: WorkbookSource) -> None:
    previous_key = st.session_state.get("active_workbook_source_key")
    if previous_key and previous_key != source.source_key:
        _clear_target_solver_cache()
    st.session_state["active_workbook_source_key"] = source.source_key


def _clear_target_solver_cache() -> None:
    for key in ["target_solver_signature", "target_solver_rows"]:
        st.session_state.pop(key, None)


def _render_baseline(data, history_df: pd.DataFrame, source: WorkbookSource) -> None:
    st.subheader("基准指标")
    metrics = data.metrics
    previous = _previous_period_metrics(history_df, source)
    cols = st.columns(5)
    cols[0].metric("认可资产", _fmt_money(metrics.admitted_assets), _history_money_delta(previous, "认可资产", metrics.admitted_assets))
    cols[1].metric("实际资本", _fmt_money(metrics.actual_capital), _history_money_delta(previous, "实际资本", metrics.actual_capital))
    cols[2].metric("最低资本", _fmt_money(metrics.minimum_capital), _history_money_delta(previous, "最低资本", metrics.minimum_capital))
    cols[3].metric(
        "核心偿付能力充足率",
        _fmt_pct(metrics.core_solvency_ratio),
        _history_ratio_delta(previous, "核心偿付能力充足率", metrics.core_solvency_ratio),
    )
    cols[4].metric(
        "综合偿付能力充足率",
        _fmt_pct(metrics.comprehensive_solvency_ratio),
        _history_ratio_delta(previous, "综合偿付能力充足率", metrics.comprehensive_solvency_ratio),
    )


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
    _render_history_snapshot(trend, source)

    ratio_tab, capital_tab = st.tabs(["充足率趋势", "资本驱动"])
    with ratio_tab:
        st.altair_chart(_history_ratio_chart(trend, source), use_container_width=True)
    with capital_tab:
        st.altair_chart(_history_capital_chart(trend, source), use_container_width=True)

    st.dataframe(
        _display_history_df(trend),
        use_container_width=True,
        hide_index=True,
        height=_history_table_height(len(trend)),
    )


def _render_history_snapshot(trend: pd.DataFrame, source: WorkbookSource) -> None:
    current, previous = _history_current_and_previous(trend, source)
    if current is None:
        return
    actual_capital = float(current["实际资本"])
    minimum_capital = float(current["最低资本"])
    capital_buffer = actual_capital - minimum_capital
    previous_buffer = None
    if previous is not None:
        previous_buffer = float(previous["实际资本"]) - float(previous["最低资本"])

    cols = st.columns(5)
    cols[0].metric("趋势观察点", f"{current['报告月份']} / {current['底稿时点']}")
    cols[1].metric(
        "综合充足率",
        _fmt_pct(float(current["综合偿付能力充足率"])),
        _history_ratio_delta(previous, "综合偿付能力充足率", float(current["综合偿付能力充足率"])),
    )
    cols[2].metric(
        "核心充足率",
        _fmt_pct(float(current["核心偿付能力充足率"])),
        _history_ratio_delta(previous, "核心偿付能力充足率", float(current["核心偿付能力充足率"])),
    )
    cols[3].metric(
        "实际资本",
        _fmt_money(actual_capital),
        _history_money_delta(previous, "实际资本", actual_capital),
    )
    cols[4].metric(
        "资本缓冲",
        _fmt_money(capital_buffer),
        None if previous_buffer is None else _fmt_money_delta(capital_buffer - previous_buffer),
    )


def _history_current_and_previous(trend: pd.DataFrame, source: WorkbookSource) -> tuple[pd.Series | None, pd.Series | None]:
    if trend.empty:
        return None, None
    selected = trend[trend["source_key"] == source.source_key]
    current = selected.iloc[-1] if not selected.empty else trend.iloc[-1]
    previous = trend[trend["报告月末"].astype(str) < str(current["报告月末"])]
    if previous.empty:
        return current, None
    return current, previous.sort_values("报告月末").iloc[-1]


def _history_ratio_chart(trend: pd.DataFrame, source: WorkbookSource) -> alt.Chart:
    chart_df = _history_ratio_chart_df(trend, source)
    month_order = trend["报告月份"].tolist()
    ratio_domain = _history_ratio_axis_domain(chart_df["充足率"])
    metric_colors = alt.Scale(
        domain=["综合偿付能力充足率", "核心偿付能力充足率"],
        range=["#2563eb", "#38bdf8"],
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
    for label, value, color in [
        ("最低监管线 100%", 100.0, "#fca5a5"),
        ("预警线 120%", 120.0, "#facc15"),
        ("舒适线 150%", 150.0, "#86efac"),
    ]:
        threshold_df = pd.DataFrame([{"报告月份": month_order[-1], "监管线": label, "充足率": value}])
        threshold_layers.extend(
            [
                alt.Chart(threshold_df)
                .mark_rule(color=color, strokeDash=[5, 5], strokeWidth=1.4, opacity=0.9)
                .encode(y=alt.Y("充足率:Q", scale=alt.Scale(domain=ratio_domain))),
                alt.Chart(threshold_df)
                .mark_text(color=color, align="left", dx=8, dy=-4, fontSize=12)
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
                scale=alt.Scale(domain=["实际资本", "最低资本", "量化风险最低资本"], range=["#2563eb", "#fb7185", "#f59e0b"]),
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
        .mark_tick(thickness=3, size=26, color="#111827")
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


def _render_scenario_controls(data, policy: PolicyParameters, source: WorkbookSource) -> list[Adjustment]:
    st.subheader("情景模块")
    st.caption(f"当前测算底稿：{source.report_month} / {source.timepoint_label}（{source.path.name}）")
    position_tab, price_tab, market_tab, target_tab, base_tab = st.tabs(
        ["加仓/减仓/建仓", "上涨/下跌", "市场冲击", "目标倒推", "基准资产暴露"]
    )
    adjustments: list[Adjustment] = []

    with position_tab:
        st.caption("用于模拟买入、卖出、建仓或减仓。按选中资产类型现有结构同比调整风险暴露；债券类资产可选择久期 bucket。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="position", key_prefix="position"))

    with price_tab:
        st.caption("用于模拟资产价格上涨或下跌。估值变动默认进入实际资本和核心资本，同时按暴露变化估算最低资本影响。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="price", key_prefix="price"))

    with market_tab:
        adjustments.extend(_render_market_shock_controls(data))

    with target_tab:
        _render_target_solver(data, policy)

    with base_tab:
        summary = build_asset_summary(data.kbqs, "资产类型")
        sortable_summary, column_config = _sortable_money_df(summary)
        st.dataframe(sortable_summary, column_config=column_config, use_container_width=True, height=360)

    return adjustments


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
        st.dataframe(_display_market_shock_df(pd.DataFrame(rows)), use_container_width=True, hide_index=True)
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
        duration = _duration_bucket_midpoint(bucket)
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


def _render_adjustment_rows(data, mode_name: str, key_prefix: str) -> list[Adjustment]:
    dimension = "资产类型"
    summary = build_asset_summary(data.kbqs, dimension)
    options = summary[dimension].astype(str).tolist()
    count = st.number_input(
        "情景条数",
        min_value=1,
        max_value=5,
        value=1,
        step=1,
        key=f"{key_prefix}_count",
    )
    input_mode = st.radio(
        "输入方式",
        ["比例", "金额"],
        horizontal=True,
        key=f"{key_prefix}_input_mode",
        help="金额单位为元；系统会按该对象现有认可价值反推变化比例。",
    )
    adjustments: list[Adjustment] = []
    for idx in range(int(count)):
        cols = st.columns([3, 1.3, 1.2, 1.2])
        member = cols[0].selectbox(
            f"对象 {idx + 1}",
            options,
            key=f"{key_prefix}_member_{dimension}_{idx}",
        )
        duration_bucket = "存量平均"
        duration_options = _duration_options(data, member)
        if duration_options:
            duration_bucket = cols[1].selectbox(
                "债券久期",
                duration_options,
                key=f"{key_prefix}_duration_{dimension}_{idx}",
                help="来自 MC_RESULT_资产端利率风险明细表；选择后用该资产类型在对应久期 bucket 的利率风险抵减因子。",
            )
            input_col = cols[2]
        else:
            input_col = cols[1]
        if input_mode == "比例":
            pct = input_col.number_input(
                "变化比例%",
                min_value=-100.0,
                max_value=500.0,
                value=0.0,
                step=1.0,
                key=f"{key_prefix}_pct_{dimension}_{idx}",
            )
            amount = 0.0
        else:
            amount_yi = input_col.number_input(
                "变化金额(亿元)",
                min_value=-10_000.0,
                max_value=10_000.0,
                value=0.0,
                step=1.0,
                format="%.2f",
                key=f"{key_prefix}_amount_{dimension}_{idx}",
            )
            amount = float(amount_yi) * 100000000.0
            pct = 0.0
        current_value = float(summary.loc[summary[dimension].astype(str) == member, "认可价值"].sum())
        cols[3].metric("当前认可价值", _fmt_money(current_value))
        adjustments.append(
            Adjustment(
                dimension=dimension,
                member=member,
                change_pct=float(pct),
                mode=mode_name,
                change_amount=float(amount),
                duration_bucket=duration_bucket,
            )
        )
    return adjustments


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


def _render_target_solver(data, policy: PolicyParameters) -> None:
    st.caption(
        "按目标偿付能力充足率倒推单一资产类型所需的最小变化金额，可返回加仓/上涨或减仓/下跌。点击按钮后计算，避免每次调整参数都重跑。"
        "加仓倒推只改变资产配置和最低资本链条；上涨/下跌按估值变动同步影响实际资本和核心资本。"
    )
    metric = st.radio(
        "目标指标",
        ["综合偿付能力充足率", "核心偿付能力充足率"],
        horizontal=True,
        key="target_metric",
    )
    baseline_ratio = float(run_scenario(data, [], policy).scenario[metric])
    st.markdown(
        """
        <style>
        .st-key-target_shortcuts .shortcut-label {
            color: rgb(49, 51, 63);
            font-size: 14px;
            font-weight: 400;
            line-height: 1.6;
            margin: 0 0 0.25rem;
        }
        .st-key-target_shortcuts div[data-testid="stHorizontalBlock"] {
            gap: 0;
        }
        .st-key-target_shortcuts div[data-testid="column"] {
            flex: 0 0 auto;
            width: auto !important;
            min-width: 0 !important;
        }
        .st-key-target_shortcuts button {
            min-width: 5.25rem;
            min-height: 2.3rem;
            border-radius: 0.45rem;
            font-weight: 400;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="target_shortcuts"):
        st.markdown('<div class="shortcut-label">快捷输入</div>', unsafe_allow_html=True)
        shortcut_cols = st.columns([0.58, 0.58, 0.58, 6], gap=None)
        for col, target in zip(shortcut_cols[:3], [1.0, 1.2, 1.5]):
            if col.button(_fmt_pct(target), key=f"target_shortcut_{int(target * 100)}"):
                st.session_state["target_delta_pct"] = round((target - baseline_ratio) * 100.0, 2)

    with st.form("target_solver_form"):
        summary = build_asset_summary(data.kbqs, "资产类型")
        options = summary["资产类型"].astype(str).tolist()
        cols = st.columns([1.4, 3, 1.3])
        target_delta = cols[0].number_input(
            "目标变化(pct)",
            min_value=-100.0,
            max_value=100.0,
            value=5.0,
            step=0.5,
            format="%.2f",
            key="target_delta_pct",
            help="按百分点处理，例如 5 表示从 129.70% 到 134.70%。上方快捷按钮只会填入这个数值，不会开始倒推。",
        )
        target_ratio = baseline_ratio + float(target_delta) / 100.0
        asset_type = cols[1].selectbox("资产类型", options, key="target_asset_type")
        duration_options = _duration_options(data, asset_type)
        duration_bucket = "存量平均"
        if duration_options:
            duration_bucket = cols[2].selectbox("债券久期", duration_options, key="target_duration_bucket")
        else:
            cols[2].metric("债券久期", "不适用")
        submitted = st.form_submit_button("开始倒推")

    policy_signature = (
        policy.minimum_capital_multiplier,
        policy.market_risk_multiplier,
        policy.credit_risk_multiplier,
        policy.sync_actual_capital_with_assets,
    )
    input_signature = (metric, float(target_ratio), float(target_delta), asset_type, duration_bucket, policy_signature)
    if submitted:
        results = [
            solve_target_change(
                data=data,
                asset_type=asset_type,
                metric=metric,
                target_delta_pct_points=float(target_delta),
                mode="position",
                duration_bucket=duration_bucket,
                policy=policy,
            ),
            solve_target_change(
                data=data,
                asset_type=asset_type,
                metric=metric,
                target_delta_pct_points=float(target_delta),
                mode="price",
                duration_bucket=duration_bucket,
                policy=policy,
            ),
        ]
        rows = []
        for result in results:
            if result.mode == "position":
                action = "加仓/建仓（配置口径）"
                replay_note = "正算复现需在加仓/减仓/建仓模块输入同一变化金额，并选择相同债券久期。"
            else:
                action = "上涨/下跌（估值变动）"
                replay_note = "正算复现需在上涨/下跌模块输入同一变化金额，并选择相同债券久期。"
            rows.append(
                {
                    "动作": action,
                    "状态": "有解" if result.solved else "无解",
                    "基准充足率": result.baseline_ratio,
                    "目标充足率": result.target_ratio,
                    "求解后充足率": result.achieved_ratio,
                    "所需变化金额": result.change_amount if result.solved else 0.0,
                    "所需变化比例": result.change_pct if result.solved else 0.0,
                    "最低资本变化": result.minimum_capital_delta,
                    "实际资本变化": result.actual_capital_delta,
                    "说明": result.reason,
                    "正算复现口径": replay_note,
                }
            )
        st.session_state["target_solver_signature"] = input_signature
        st.session_state["target_solver_rows"] = rows

    if "target_solver_rows" not in st.session_state:
        st.info("设置目标参数后点击“开始倒推”计算。")
        return
    if st.session_state.get("target_solver_signature") != input_signature:
        st.info("目标参数已变化，点击“开始倒推”刷新结果。")
        return
    st.dataframe(_display_money_df(pd.DataFrame(st.session_state["target_solver_rows"])), use_container_width=True, hide_index=True)


def _render_policy_controls() -> PolicyParameters:
    st.subheader("政策与口径参数")
    cols = st.columns(4)
    minimum_capital_multiplier = cols[0].number_input(
        "最低资本乘数",
        min_value=0.0,
        max_value=2.0,
        value=1.0,
        step=0.01,
        help="例如 0.95 可模拟最低资本按 95% 计算。",
    )
    market_multiplier = cols[1].number_input(
        "市场风险乘数", min_value=0.0, max_value=2.0, value=1.0, step=0.01
    )
    credit_multiplier = cols[2].number_input(
        "信用风险乘数", min_value=0.0, max_value=2.0, value=1.0, step=0.01
    )
    sync_actual = cols[3].checkbox("认可资产变化同步实际资本", value=False)
    with st.expander("政策 overlay 摘要", expanded=False):
        st.dataframe(load_policy_overlays(), use_container_width=True, hide_index=True)
    return PolicyParameters(
        minimum_capital_multiplier=float(minimum_capital_multiplier),
        market_risk_multiplier=float(market_multiplier),
        credit_risk_multiplier=float(credit_multiplier),
        sync_actual_capital_with_assets=bool(sync_actual),
    )


def _render_result(result) -> None:
    st.subheader("测算结果")
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
    cols = st.columns(5)
    cols[0].metric("情景最低资本", _fmt_money(result.scenario["最低资本"]), _fmt_money(result.scenario["最低资本"] - result.baseline["最低资本"]))
    cols[1].metric("情景实际资本", _fmt_money(result.scenario["实际资本"]), _fmt_money(result.scenario["实际资本"] - result.baseline["实际资本"]))
    cols[2].metric("情景核心充足率", _fmt_pct(result.scenario["核心偿付能力充足率"]), _fmt_pct_delta(result.scenario["核心偿付能力充足率"] - result.baseline["核心偿付能力充足率"]))
    cols[3].metric("情景综合充足率", _fmt_pct(result.scenario["综合偿付能力充足率"]), _fmt_pct_delta(result.scenario["综合偿付能力充足率"] - result.baseline["综合偿付能力充足率"]))
    cols[4].metric("量化最低资本", _fmt_money(result.scenario["量化风险最低资本"]), _fmt_money(result.scenario["量化风险最低资本"] - result.baseline["量化风险最低资本"]))
    if not result.adjustment_summary.empty:
        st.caption("本次非零情景调整")
        st.dataframe(_display_money_df(result.adjustment_summary), use_container_width=True, hide_index=True)
    st.dataframe(_format_metric_comparison(comparison), use_container_width=True, hide_index=True)


def _render_detail_tabs(data, result) -> None:
    tabs = st.tabs(["情景输入", "瀑布分析", "贡献分析", "因子假设", "暴露变化", "分账户资本", "原始报表"])
    with tabs[0]:
        if result.adjustment_summary.empty:
            st.info("当前没有非零情景调整。")
        else:
            st.dataframe(
                _display_adjustment_summary(data, result.adjustment_summary),
                use_container_width=True,
                hide_index=True,
            )
    with tabs[1]:
        _render_waterfall_analysis(result)
    with tabs[2]:
        st.dataframe(_display_money_df(result.contribution_summary), use_container_width=True, hide_index=True)
    with tabs[3]:
        st.dataframe(_display_rate_df(result.risk_rates), use_container_width=True, hide_index=True)
    with tabs[4]:
        st.info("利率风险情景不再把固收资产简单作为正向暴露处理；新增国债、地方政府债等会按资产端利率风险抵减因子降低寿险利率风险最低资本。")
        st.dataframe(_display_money_df(result.exposure_summary), use_container_width=True, hide_index=True)
    with tabs[5]:
        st.dataframe(_display_report_money_df(data.account_capital), use_container_width=True, hide_index=True)
    with tabs[6]:
        st.dataframe(_display_report_money_df(data.s01), use_container_width=True, hide_index=True)
        st.dataframe(_display_report_money_df(data.s05), use_container_width=True, hide_index=True)


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
    st.altair_chart(_waterfall_chart(ratio_df, "百分点"), use_container_width=True)
    st.dataframe(_display_waterfall_df(ratio_df, "pct"), use_container_width=True, hide_index=True)

    st.subheader("最低资本变化拆解")
    st.altair_chart(_waterfall_chart(capital_df, "亿元"), use_container_width=True)
    st.dataframe(_display_waterfall_df(capital_df, "money"), use_container_width=True, hide_index=True)

    st.subheader("风险子项贡献排名")
    ranking = _risk_contribution_ranking(result.contribution_summary)
    if ranking.empty:
        st.info("当前情景没有风险子项最低资本变化。")
    else:
        st.altair_chart(_ranking_chart(ranking), use_container_width=True)
        st.dataframe(_display_money_df(ranking), use_container_width=True, hide_index=True)


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
                    range=["#d8efe0", "#f4d6d6", "#d9e2f2"],
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
                alt.value("#d8efe0"),
                alt.value("#f4d6d6"),
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
        out["变化"] = out["变化"].map(lambda value: f"{value:+.2f} pct")
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
        out.loc[ratio_mask, col] = out.loc[ratio_mask, col].map(_fmt_pct)
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
            out[col] = df[col].map(_fmt_pct)
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
    return f"{value * 100:+.2f} pct"


if __name__ == "__main__":
    main()
