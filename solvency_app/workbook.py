from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pandas as pd


SHEET_PREFIXES = {
    "s01": "S01_",
    "s05": "S05_",
    "kbqs": "KBQS_V_",
    "fls05acc": "FLS05ACC_",
}

OPTIONAL_SHEET_PREFIXES = {
    "mc_result": "MC_RESULT_",
}

EXPOSURE_COLUMNS = [
    "认可价值",
    "利率风险暴露",
    "利差风险暴露",
    "交易对手风险暴露",
    "权益价格风险暴露",
    "房地产风险暴露",
    "汇率风险暴露",
]


class WorkbookValidationError(ValueError):
    """Raised when an uploaded workbook is not a supported solvency workbook."""


@dataclass(frozen=True)
class BaselineMetrics:
    admitted_assets: float
    admitted_liabilities: float
    actual_capital: float
    core_tier1_capital: float
    core_tier2_capital: float
    core_capital: float
    minimum_capital: float
    quantitative_minimum_capital: float
    control_risk_minimum_capital: float
    additional_capital: float
    core_solvency_ratio: float
    comprehensive_solvency_ratio: float


@dataclass(frozen=True)
class WorkbookData:
    metrics: BaselineMetrics
    s01: pd.DataFrame
    s05: pd.DataFrame
    kbqs: pd.DataFrame
    account_capital: pd.DataFrame
    source_name: str


def load_workbook_data(source: str | Path | BinaryIO) -> WorkbookData:
    excel = pd.ExcelFile(source, engine="openpyxl")
    sheets = _resolve_sheets(excel.sheet_names)

    s01 = _read_report_sheet(excel, sheets["s01"])
    s05 = _read_report_sheet(excel, sheets["s05"])
    kbqs = _read_kbqs_sheet(excel, sheets["kbqs"])
    optional_sheets = _resolve_optional_sheets(excel.sheet_names)
    if "mc_result" in optional_sheets:
        kbqs = _enrich_interest_risk_from_mc_result(
            kbqs, _read_mc_result_sheet(excel, optional_sheets["mc_result"])
        )
    account_capital = _read_account_capital_sheet(excel, sheets["fls05acc"])
    metrics = _extract_metrics(s01)
    source_name = getattr(source, "name", None) or str(source)

    return WorkbookData(
        metrics=metrics,
        s01=s01,
        s05=s05,
        kbqs=kbqs,
        account_capital=account_capital,
        source_name=source_name,
    )


def _resolve_sheets(sheet_names: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, prefix in SHEET_PREFIXES.items():
        match = next((name for name in sheet_names if name.startswith(prefix)), None)
        if match is None:
            missing.append(prefix)
        else:
            resolved[key] = match
    if missing:
        raise WorkbookValidationError(
            "工作簿缺少必要 sheet: " + ", ".join(missing)
        )
    return resolved


def _resolve_optional_sheets(sheet_names: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key, prefix in OPTIONAL_SHEET_PREFIXES.items():
        match = next((name for name in sheet_names if name.startswith(prefix)), None)
        if match is not None:
            resolved[key] = match
    return resolved


def _read_report_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(excel, sheet_name=sheet_name, header=None)
    header_idx = _find_header_row(raw, "项目")
    if header_idx is None:
        raise WorkbookValidationError(f"{sheet_name} 未找到包含“项目”的表头行")
    header = raw.iloc[header_idx].ffill()
    df = raw.iloc[header_idx + 1 :].copy()
    df.columns = [str(x).strip() for x in header]
    df = df.dropna(how="all")
    if "项目" in df.columns:
        df["项目"] = df["项目"].astype(str).str.strip()
    return df.reset_index(drop=True)


def _read_kbqs_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(excel, sheet_name=sheet_name, header=0)
    required = {"账户", "资产类型", *EXPOSURE_COLUMNS}
    missing = required.difference(df.columns)
    if missing:
        raise WorkbookValidationError(
            f"{sheet_name} 缺少必要字段: " + ", ".join(sorted(missing))
        )

    keep = ["表名", "账户", "证券名称", "证券代码", "资产类型", *EXPOSURE_COLUMNS]
    existing = [col for col in keep if col in df.columns]
    df = df[existing].copy()
    df["账户"] = df["账户"].fillna("未分类账户").astype(str).str.strip()
    df["资产类型"] = df["资产类型"].fillna("未分类资产").astype(str).str.strip()
    for col in EXPOSURE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def _read_mc_result_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(excel, sheet_name=sheet_name, header=1)
    required = {"账户", "资产类型", "账面价值净价", "应收利息", "资产端利率风险MC"}
    missing = required.difference(df.columns)
    if missing:
        return pd.DataFrame()
    keep = ["账户", "资产类型", "账面价值净价", "应收利息", "资产端利率风险MC"]
    df = df[keep].copy()
    df["账户"] = df["账户"].fillna("未分类账户").astype(str).str.strip()
    df["资产类型"] = df["资产类型"].fillna("未分类资产").astype(str).str.strip()
    for col in ["账面价值净价", "应收利息", "资产端利率风险MC"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["利率风险资产价值"] = df["账面价值净价"] + df["应收利息"]
    return df


def _enrich_interest_risk_from_mc_result(kbqs: pd.DataFrame, mc_result: pd.DataFrame) -> pd.DataFrame:
    if mc_result.empty:
        return kbqs

    enriched = kbqs.copy()
    by_account_asset = (
        mc_result.groupby(["账户", "资产类型"], dropna=False)[["利率风险资产价值", "资产端利率风险MC"]]
        .sum()
        .reset_index()
    )
    by_account_asset["账户资产利率风险率"] = _safe_series_div(
        by_account_asset["资产端利率风险MC"], by_account_asset["利率风险资产价值"]
    )

    by_asset = (
        mc_result.groupby("资产类型", dropna=False)[["利率风险资产价值", "资产端利率风险MC"]]
        .sum()
        .reset_index()
    )
    by_asset["资产类型利率风险率"] = _safe_series_div(
        by_asset["资产端利率风险MC"], by_asset["利率风险资产价值"]
    )

    enriched = enriched.merge(
        by_account_asset[["账户", "资产类型", "账户资产利率风险率"]],
        on=["账户", "资产类型"],
        how="left",
    ).merge(
        by_asset[["资产类型", "资产类型利率风险率"]],
        on="资产类型",
        how="left",
    )

    rate = enriched["账户资产利率风险率"].fillna(enriched["资产类型利率风险率"]).fillna(0.0)
    enriched["利率风险暴露"] = enriched["认可价值"] * rate
    return enriched.drop(columns=["账户资产利率风险率", "资产类型利率风险率"])


def _read_account_capital_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(excel, sheet_name=sheet_name, header=None)
    header_idx = _find_header_row(raw, "项目")
    if header_idx is None:
        raise WorkbookValidationError(f"{sheet_name} 未找到包含“项目”的表头行")
    header = raw.iloc[header_idx].ffill()
    df = raw.iloc[header_idx + 1 :].copy()
    df.columns = [str(x).strip() for x in header]
    df = df.dropna(how="all")
    if "项目" in df.columns:
        df["项目"] = df["项目"].astype(str).str.strip()
    return df.reset_index(drop=True)


def _find_header_row(raw: pd.DataFrame, required_label: str) -> int | None:
    for idx, row in raw.iterrows():
        if required_label in {str(value).strip() for value in row.dropna().tolist()}:
            return int(idx)
    return None


def _extract_metrics(s01: pd.DataFrame) -> BaselineMetrics:
    values = {}
    for _, row in s01.iterrows():
        item = str(row.get("项目", "")).strip()
        if item:
            values[item] = _to_float(row.get("期末数"))

    core_capital = values.get("核心一级资本", 0.0) + values.get("核心二级资本", 0.0)
    minimum_capital = _required(values, "最低资本")
    actual_capital = _required(values, "实际资本")
    return BaselineMetrics(
        admitted_assets=_required(values, "认可资产"),
        admitted_liabilities=_required(values, "认可负债"),
        actual_capital=actual_capital,
        core_tier1_capital=values.get("核心一级资本", 0.0),
        core_tier2_capital=values.get("核心二级资本", 0.0),
        core_capital=core_capital,
        minimum_capital=minimum_capital,
        quantitative_minimum_capital=_required(values, "量化风险最低资本"),
        control_risk_minimum_capital=values.get("控制风险最低资本", 0.0),
        additional_capital=values.get("附加资本", 0.0),
        core_solvency_ratio=_safe_div(core_capital, minimum_capital),
        comprehensive_solvency_ratio=_safe_div(actual_capital, minimum_capital),
    )


def _required(values: dict[str, float], key: str) -> float:
    value = values.get(key)
    if value is None:
        raise WorkbookValidationError(f"S01 缺少必要指标: {key}")
    return value


def _to_float(value: object) -> float:
    if pd.isna(value):
        return 0.0
    return float(value)


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _safe_series_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, pd.NA)
    return (numerator / denominator).fillna(0.0)
