"""Non-additive holding scopes and traceable look-through groups."""
from __future__ import annotations

import pandas as pd


def layer_values(frame):
    return pd.to_numeric(frame.get("交易结构层级", pd.Series(index=frame.index, dtype=float)), errors="coerce")


def surface_holdings(frame: pd.DataFrame) -> pd.DataFrame:
    # No deduplication by security code: separate accounts/lots are legitimate.
    return frame.loc[layer_values(frame).eq(0)].copy()


def tree_inventory(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    if "资产树标识符" not in work:
        return pd.DataFrame()
    work = work[work["资产树标识符"].notna() & work["资产树标识符"].astype(str).str.strip().ne("")]
    rows = []
    for (account, tree), group in work.groupby(["账户", "资产树标识符"], sort=False):
        layers = layer_values(group)
        root = group[layers.eq(0)]
        rows.append({"账户": account, "资产树标识符": str(tree), "表层记录数": len(root),
                     "表层资产": str(root.iloc[0].get("证券名称", "")) if len(root) == 1 else "待核查",
                     "表层价值": root["认可价值"].sum() if len(root) == 1 else float("nan"),
                     "底层记录数": int(layers.gt(0).sum()), "最深层级": layers.max(),
                     "定位结果": "唯一表层" if len(root) == 1 else "无表层" if root.empty else "多个表层"})
    return pd.DataFrame(rows)


def distribution(frame: pd.DataFrame, dimension: str) -> pd.DataFrame:
    labels = frame[dimension].fillna("未提供").astype(str).str.strip().replace("", "未提供")
    result = frame.assign(**{dimension: labels}).groupby(dimension, dropna=False).agg(
        记录数=("认可价值", "size"), 认可价值=("认可价值", "sum")).reset_index()
    total = float(result["认可价值"].sum())
    result["占所选范围比例"] = result["认可价值"] / total if total > 0 else float("nan")
    return result.sort_values("认可价值", ascending=False)


def maturity_profile(frame: pd.DataFrame, report_date) -> pd.DataFrame:
    dates = pd.to_datetime(frame["到期日"], errors="coerce")
    days = (dates - pd.Timestamp(report_date)).dt.days
    labels = pd.cut(days, [-float("inf"), -1, 365, 3*365, 5*365, 10*365, float("inf")],
                    labels=["已到期 / 待核对", "1年以内", "1-3年", "3-5年", "5-10年", "10年以上"])
    work = frame.assign(到期分组=labels.astype(object).fillna("无到期日 / 未提供"))
    return distribution(work, "到期分组")
