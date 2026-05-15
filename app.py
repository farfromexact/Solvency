from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from solvency_app.policies import load_policy_overlays
from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.target import solve_target_change
from solvency_app.workbook import WorkbookValidationError, load_workbook_data


st.set_page_config(page_title="偿付能力资产配置情景测算", layout="wide")


def main() -> None:
    st.title("偿付能力资产配置情景测算")
    st.caption("基于现有底稿反推口径的情景估算，不替代监管报送系统或完整偿二代复算引擎。")

    source = _resolve_default_workbook()
    if source is None:
        st.error("当前目录没有找到唯一的 Excel 底稿。请在 repo 根目录只保留一个 .xlsx 底稿文件。")
        return
    st.caption(f"当前底稿：{source.name}")

    try:
        data = _load_data(source)
    except WorkbookValidationError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.exception(exc)
        return

    _render_baseline(data)
    policy = _render_policy_controls()
    adjustments = _render_scenario_controls(data, policy)
    result = run_scenario(data, adjustments, policy)
    _render_result(result)
    _render_detail_tabs(data, result)


@st.cache_data(show_spinner="正在解析底稿...")
def _load_data(source):
    return load_workbook_data(source)


def _resolve_default_workbook() -> Path | None:
    workbooks = sorted(Path(".").glob("*.xlsx"))
    workbooks = [path for path in workbooks if not path.name.startswith("~$")]
    if len(workbooks) != 1:
        return None
    return workbooks[0]


def _render_baseline(data) -> None:
    st.subheader("基准指标")
    metrics = data.metrics
    cols = st.columns(5)
    cols[0].metric("认可资产", _fmt_money(metrics.admitted_assets))
    cols[1].metric("实际资本", _fmt_money(metrics.actual_capital))
    cols[2].metric("最低资本", _fmt_money(metrics.minimum_capital))
    cols[3].metric("核心偿付能力充足率", _fmt_pct(metrics.core_solvency_ratio))
    cols[4].metric("综合偿付能力充足率", _fmt_pct(metrics.comprehensive_solvency_ratio))


def _render_scenario_controls(data, policy: PolicyParameters) -> list[Adjustment]:
    st.subheader("情景模块")
    position_tab, price_tab, target_tab, base_tab = st.tabs(["加仓/减仓/建仓", "上涨/下跌", "目标倒推", "基准资产暴露"])
    adjustments: list[Adjustment] = []

    with position_tab:
        st.caption("用于模拟买入、卖出、建仓或减仓。按选中资产类型现有结构同比调整风险暴露；债券类资产可选择久期 bucket。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="position", key_prefix="position"))

    with price_tab:
        st.caption("用于模拟资产价格上涨或下跌。估值变动默认进入实际资本和核心资本，同时按暴露变化估算最低资本影响。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="price", key_prefix="price"))

    with target_tab:
        _render_target_solver(data, policy)

    with base_tab:
        summary = build_asset_summary(data.kbqs, "资产类型")
        st.dataframe(_display_money_df(summary), use_container_width=True, height=360)

    return adjustments


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
    with st.form("target_solver_form"):
        summary = build_asset_summary(data.kbqs, "资产类型")
        options = summary["资产类型"].astype(str).tolist()
        cols = st.columns([1.5, 1.5, 3, 1.3])
        metric = cols[0].radio(
            "目标指标",
            ["综合偿付能力充足率", "核心偿付能力充足率"],
            horizontal=True,
            key="target_metric",
        )
        target_ratio = cols[1].segmented_control(
            "目标充足率",
            options=[1.0, 1.2, 1.5],
            default=1.2,
            format_func=_fmt_pct,
            key="target_ratio_choice",
        )
        target_delta = (float(target_ratio) - float(run_scenario(data, [], policy).scenario[metric])) * 100.0
        asset_type = cols[2].selectbox("资产类型", options, key="target_asset_type")
        duration_options = _duration_options(data, asset_type)
        duration_bucket = "存量平均"
        if duration_options:
            duration_bucket = cols[3].selectbox("债券久期", duration_options, key="target_duration_bucket")
        else:
            cols[3].metric("债券久期", "不适用")
        submitted = st.form_submit_button("开始倒推")

    policy_signature = (
        policy.minimum_capital_multiplier,
        policy.market_risk_multiplier,
        policy.credit_risk_multiplier,
        policy.sync_actual_capital_with_assets,
    )
    input_signature = (metric, float(target_ratio), asset_type, duration_bucket, policy_signature)
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
    st.dataframe(_format_metric_comparison(comparison), use_container_width=True, hide_index=True)


def _render_detail_tabs(data, result) -> None:
    tabs = st.tabs(["情景输入", "贡献分析", "因子假设", "暴露变化", "分账户资本", "原始报表"])
    with tabs[0]:
        if result.adjustment_summary.empty:
            st.info("当前没有非零情景调整。")
        else:
            st.dataframe(_display_money_df(result.adjustment_summary), use_container_width=True, hide_index=True)
    with tabs[1]:
        st.dataframe(_display_money_df(result.contribution_summary), use_container_width=True, hide_index=True)
    with tabs[2]:
        st.dataframe(_display_rate_df(result.risk_rates), use_container_width=True, hide_index=True)
    with tabs[3]:
        st.info("利率风险情景不再把固收资产简单作为正向暴露处理；新增国债、地方政府债等会按资产端利率风险抵减因子降低寿险利率风险最低资本。")
        st.dataframe(_display_money_df(result.exposure_summary), use_container_width=True, hide_index=True)
    with tabs[4]:
        st.dataframe(_display_money_df(data.account_capital), use_container_width=True, hide_index=True)
    with tabs[5]:
        st.dataframe(_display_money_df(data.s01), use_container_width=True, hide_index=True)
        st.dataframe(_display_money_df(data.s05), use_container_width=True, hide_index=True)


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


def _fmt_money(value: float) -> str:
    return f"{value / 100000000:,.2f} 亿元"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_pct_delta(value: float) -> str:
    return f"{value * 100:+.2f} pct"


if __name__ == "__main__":
    main()
