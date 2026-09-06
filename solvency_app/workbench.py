"""Small, testable state contract for explicit scenario application."""
from __future__ import annotations

import math

import pandas as pd

from .scenario import Adjustment


EDITOR_COLUMNS = ["启用", "动作", "资产类型", "输入方式", "数值", "债券久期"]


def empty_editor(asset: str) -> pd.DataFrame:
    return pd.DataFrame([[True, "加减仓", asset, "亿元", 0.0, "存量平均"]], columns=EDITOR_COLUMNS)


def editor_adjustments(rows: pd.DataFrame, data) -> list[Adjustment]:
    result = []
    values = data.kbqs.groupby("资产类型")["认可价值"].sum().to_dict()
    for index, row in rows.iterrows():
        if pd.isna(row.get("启用")) or not bool(row["启用"]):
            continue
        value = row.get("数值")
        if pd.isna(value):
            raise ValueError(f"第 {index + 1} 条情景缺少数值")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"第 {index + 1} 条情景数值无效")
        if value == 0:
            continue
        asset = row.get("资产类型")
        if asset not in values or values[asset] <= 0:
            raise ValueError(f"第 {index + 1} 条情景没有可用存量资产，暂不支持零持仓建仓")
        mode, unit = row.get("动作"), row.get("输入方式")
        if mode not in ("加减仓", "价格变化") or unit not in ("亿元", "%"):
            raise ValueError(f"第 {index + 1} 条情景缺少动作或输入方式")
        bucket = row.get("债券久期")
        bucket = "存量平均" if pd.isna(bucket) else str(bucket)
        table = data.interest_factor_table
        available = table.loc[table["资产类型"] == asset, "久期桶"].tolist() if not table.empty else []
        if bucket != "存量平均" and bucket not in available:
            raise ValueError(f"{asset} 不支持 {bucket}，请改为存量平均或有效久期分组")
        amount = value * 1e8 if unit == "亿元" else values[asset] * value / 100
        # Validate sequential adjustments using the same basis as the engine.
        if amount < -values[asset] - 0.01:
            raise ValueError(f"{asset} 的减仓或跌价超过剩余认可价值，请调小幅度")
        values[asset] += amount
        result.append(Adjustment("资产类型", str(asset), value if unit == "%" else 0.0,
                                 "position" if mode == "加减仓" else "price",
                                 value * 1e8 if unit == "亿元" else 0.0, bucket))
    return result


def editor_from_adjustments(adjustments: list[Adjustment]) -> pd.DataFrame:
    return pd.DataFrame([
        {"启用": True, "动作": "加减仓" if item.mode == "position" else "价格变化", "资产类型": item.member,
         "输入方式": "亿元" if item.change_amount else "%",
         "数值": item.change_amount / 1e8 if item.change_amount else item.change_pct,
         "债券久期": item.duration_bucket}
        for item in adjustments
    ], columns=EDITOR_COLUMNS)


def active_plan_rows(adjustments: list[Adjustment]) -> pd.DataFrame:
    rows = editor_from_adjustments(adjustments).drop(columns="启用")
    return rows.rename(columns={"数值": "生效数值", "输入方式": "单位"})
