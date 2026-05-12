from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from solvency_app.policies import load_policy_overlays
from solvency_app.scenario import Adjustment, PolicyParameters, build_asset_summary, run_scenario
from solvency_app.workbook import WorkbookValidationError, load_workbook_data


DEFAULT_WORKBOOK = Path("1000_20251231_20260113v2.xlsx")


st.set_page_config(page_title="偿付能力资产配置情景测算", layout="wide")


def main() -> None:
    st.title("偿付能力资产配置情景测算")
    st.caption("基于现有底稿反推口径的情景估算，不替代监管报送系统或完整偿二代复算引擎。")

    with st.sidebar:
        st.header("数据导入")
        uploaded = st.file_uploader("上传月度偿付能力底稿", type=["xlsx"])
        use_default = st.checkbox("使用当前文件夹默认底稿", value=uploaded is None)
        source = uploaded if uploaded is not None else DEFAULT_WORKBOOK if use_default else None

    if source is None:
        st.info("请上传 Excel 底稿，或勾选使用当前文件夹默认底稿。")
        return

    try:
        data = _load_data(source)
    except WorkbookValidationError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.exception(exc)
        return

    _render_baseline(data)
    adjustments = _render_scenario_controls(data)
    policy = _render_policy_controls()
    result = run_scenario(data, adjustments, policy)
    _render_result(result)
    _render_detail_tabs(data, result)


@st.cache_data(show_spinner="正在解析底稿...")
def _load_data(source):
    return load_workbook_data(source)


def _render_baseline(data) -> None:
    st.subheader("基准指标")
    metrics = data.metrics
    cols = st.columns(5)
    cols[0].metric("认可资产", _fmt_money(metrics.admitted_assets))
    cols[1].metric("实际资本", _fmt_money(metrics.actual_capital))
    cols[2].metric("最低资本", _fmt_money(metrics.minimum_capital))
    cols[3].metric("核心偿付能力充足率", _fmt_pct(metrics.core_solvency_ratio))
    cols[4].metric("综合偿付能力充足率", _fmt_pct(metrics.comprehensive_solvency_ratio))


def _render_scenario_controls(data) -> list[Adjustment]:
    st.subheader("情景模块")
    position_tab, price_tab, base_tab = st.tabs(["加仓/减仓/建仓", "上涨/下跌", "基准资产暴露"])
    adjustments: list[Adjustment] = []

    with position_tab:
        st.caption("用于模拟买入、卖出、建仓或减仓。按选中资产类型/账户现有结构同比调整风险暴露。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="position", key_prefix="position"))

    with price_tab:
        st.caption("用于模拟资产价格上涨或下跌。估值变动默认进入实际资本和核心资本，同时按暴露变化估算最低资本影响。")
        adjustments.extend(_render_adjustment_rows(data, mode_name="price", key_prefix="price"))

    with base_tab:
        mode = st.radio("查看维度", ["资产类型", "账户"], horizontal=True, key="base_dimension")
        summary = build_asset_summary(data.kbqs, mode)
        st.dataframe(_display_money_df(summary), use_container_width=True, height=360)

    return adjustments


def _render_adjustment_rows(data, mode_name: str, key_prefix: str) -> list[Adjustment]:
    dimension = st.radio(
        "调整维度",
        ["资产类型", "账户"],
        horizontal=True,
        key=f"{key_prefix}_dimension",
    )
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
        cols = st.columns([3, 1.2, 1.2])
        member = cols[0].selectbox(
            f"对象 {idx + 1}",
            options,
            key=f"{key_prefix}_member_{dimension}_{idx}",
        )
        if input_mode == "比例":
            pct = cols[1].number_input(
                "变化比例%",
                min_value=-100.0,
                max_value=500.0,
                value=0.0,
                step=1.0,
                key=f"{key_prefix}_pct_{dimension}_{idx}",
            )
            amount = 0.0
        else:
            amount_wan = cols[1].number_input(
                "变化金额(万元)",
                min_value=-10_000_000.0,
                max_value=10_000_000.0,
                value=0.0,
                step=1000.0,
                key=f"{key_prefix}_amount_{dimension}_{idx}",
            )
            amount = float(amount_wan) * 10000.0
            pct = 0.0
        current_value = float(summary.loc[summary[dimension].astype(str) == member, "认可价值"].sum())
        cols[2].metric("当前认可价值", _fmt_money(current_value))
        adjustments.append(
            Adjustment(
                dimension=dimension,
                member=member,
                change_pct=float(pct),
                mode=mode_name,
                change_amount=float(amount),
            )
        )
    return adjustments


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
    ratio_names = ("变化率", "单位资本率", "核心偿付能力充足率", "综合偿付能力充足率")
    return any(name in column_name for name in ratio_names)


def _fmt_money(value: float) -> str:
    return f"{value / 10000:,.2f} 万元"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_pct_delta(value: float) -> str:
    return f"{value * 100:+.2f} pct"


if __name__ == "__main__":
    main()
