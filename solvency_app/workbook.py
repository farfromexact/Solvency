from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
import os
from pathlib import Path
import re
from typing import BinaryIO
import math
import uuid

import pandas as pd

from .calibration import RISK_COLUMNS, build_calibration


WORKBOOK_SOURCE_DIR = Path("data")
PARQUET_SOURCE_DIR = WORKBOOK_SOURCE_DIR
EXCEL_SOURCE_DIR = Path("origin stats")
PARQUET_FORMAT_VERSION = "solvency_workbook_bundle_v1"
PARQUET_TABLES = ("s01", "s05", "kbqs", "mc_detail", "cal_detail", "account_capital")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
WORKBOOK_FILENAME_RE = re.compile(
    r"^(?P<company_code>[^_]+)_(?P<report_date>\d{8})_(?P<timepoint_date>\d{8})(?P<version_suffix>[A-Za-z]\w*)?\.(?P<extension>xlsx|parquet)$",
    re.IGNORECASE,
)

SHEET_PREFIXES = {
    "s01": "S01_",
    "s05": "S05_",
    "kbqs": "KBQS_V_",
    "fls05acc": "FLS05ACC_",
    "mc_result": "MC_RESULT_",
    "cal_detail": "CAL_DETAIL_",
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
class WorkbookSource:
    company_code: str
    report_date: date
    timepoint_date: date
    version_suffix: str
    path: Path
    modified_time_ns: int

    @property
    def report_month(self) -> str:
        return self.report_date.strftime("%Y-%m")

    @property
    def report_date_label(self) -> str:
        return self.report_date.isoformat()

    @property
    def timepoint_label(self) -> str:
        label = self.timepoint_date.isoformat()
        if self.version_suffix:
            label = f"{label} {self.version_suffix}"
        return label

    @property
    def source_key(self) -> str:
        return f"{self.report_date.isoformat()}|{self.timepoint_date.isoformat()}|{self.version_suffix}|{self.path.name}"

    @property
    def sort_key(self) -> tuple[date, date, int, str, str]:
        return (
            self.report_date,
            self.timepoint_date,
            _version_rank(self.version_suffix),
            self.version_suffix,
            self.path.name,
        )


@dataclass(frozen=True)
class UploadedParquetResult:
    """Result returned after a validated Parquet bundle is installed."""

    source: WorkbookSource
    replaced_existing: bool
    size_bytes: int


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
    interest_factor_table: pd.DataFrame
    spread_factor_table: pd.DataFrame
    account_capital: pd.DataFrame
    source_name: str
    risk_factor_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    calibration_checks: pd.DataFrame = field(default_factory=pd.DataFrame)
    cal_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    mc_detail: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass(frozen=True)
class ReportSnapshot:
    """Small reporting snapshot; does not parse investment-detail sheets."""

    metrics: BaselineMetrics
    s01: pd.DataFrame
    s05: pd.DataFrame
    source_name: str


def load_report_snapshot(source: str | Path | BinaryIO) -> ReportSnapshot:
    if _source_extension(source) == ".parquet":
        tables = _read_parquet_tables(source, required=("s01", "s05"))
        source_name = _source_name(source)
        s01 = tables["s01"]
        s05 = tables["s05"]
        return ReportSnapshot(_extract_metrics(s01), s01, s05, source_name)

    with pd.ExcelFile(source, engine="openpyxl") as excel:
        s01 = _read_report_sheet(excel, _resolve_sheet(excel.sheet_names, "S01_"))
        s05 = _read_report_sheet(excel, _resolve_sheet(excel.sheet_names, "S05_"))
    return ReportSnapshot(_extract_metrics(s01), s01, s05, getattr(source, "name", None) or str(source))


def discover_workbook_sources(
    folder: str | Path = WORKBOOK_SOURCE_DIR,
    *,
    include_excel: bool | None = None,
) -> list[WorkbookSource]:
    source_dir = Path(folder)
    if not source_dir.exists():
        return []
    if include_excel is None:
        include_excel = source_dir.resolve() != Path(WORKBOOK_SOURCE_DIR).resolve()

    sources: list[WorkbookSource] = []
    patterns = ["*.parquet"] + (["*.xlsx"] if include_excel else [])
    for pattern in patterns:
        for path in source_dir.glob(pattern):
            if path.name.startswith("~$"):
                continue
            try:
                sources.append(parse_workbook_source(path))
            except WorkbookValidationError:
                continue
    return sorted(sources, key=lambda source: source.sort_key)


def parse_workbook_source(path: str | Path) -> WorkbookSource:
    workbook_path = Path(path)
    match = WORKBOOK_FILENAME_RE.match(workbook_path.name)
    if match is None:
        raise WorkbookValidationError(f"无法从文件名解析报告月份和底稿时点: {workbook_path.name}")

    report_date = _parse_yyyymmdd(match.group("report_date"), workbook_path.name)
    timepoint_date = _parse_yyyymmdd(match.group("timepoint_date"), workbook_path.name)
    version_suffix = match.group("version_suffix") or ""
    modified_time_ns = workbook_path.stat().st_mtime_ns if workbook_path.exists() else 0
    return WorkbookSource(
        company_code=match.group("company_code"),
        report_date=report_date,
        timepoint_date=timepoint_date,
        version_suffix=version_suffix,
        path=workbook_path,
        modified_time_ns=modified_time_ns,
    )


def latest_workbook_source(sources: list[WorkbookSource]) -> WorkbookSource | None:
    if not sources:
        return None
    return max(sources, key=lambda source: source.sort_key)


def find_workbook_source(
    sources: list[WorkbookSource],
    report_month: str,
    timepoint_label: str,
) -> WorkbookSource:
    for source in sources:
        if source.report_month == report_month and source.timepoint_label == timepoint_label:
            return source
    raise WorkbookValidationError(f"未找到报告月份 {report_month}、底稿时点 {timepoint_label} 的底稿")


def load_workbook_data(source: str | Path | BinaryIO) -> WorkbookData:
    if _source_extension(source) == ".parquet":
        return _load_workbook_from_parquet(source)

    with pd.ExcelFile(source, engine="openpyxl") as excel:
        return _load_workbook_from_excel(excel, source)


def _load_workbook_from_excel(excel: pd.ExcelFile, source) -> WorkbookData:
    tables = _read_excel_tables(excel, strict_exposures=True)
    return _build_workbook_data(
        **tables,
        source_name=_source_name(source),
    )


def _load_workbook_from_parquet(source: str | Path | BinaryIO) -> WorkbookData:
    tables = _read_parquet_tables(source, required=PARQUET_TABLES)
    return _build_workbook_data(
        s01=tables["s01"],
        s05=tables["s05"],
        kbqs=tables["kbqs"],
        mc_result=tables["mc_detail"],
        cal_detail=tables["cal_detail"],
        account_capital=tables["account_capital"],
        source_name=_source_name(source),
    )


def _read_excel_tables(
    excel: pd.ExcelFile,
    *,
    strict_exposures: bool,
) -> dict[str, pd.DataFrame]:
    sheets = _resolve_sheets(excel.sheet_names)
    return {
        "s01": _read_report_sheet(excel, sheets["s01"]),
        "s05": _read_report_sheet(excel, sheets["s05"]),
        "kbqs": _read_kbqs_sheet(
            excel,
            sheets["kbqs"],
            strict_exposures=strict_exposures,
        ),
        "mc_result": _read_mc_result_sheet(excel, sheets["mc_result"]),
        "cal_detail": _read_cal_detail_sheet(excel, sheets["cal_detail"]),
        "account_capital": _read_account_capital_sheet(excel, sheets["fls05acc"]),
    }


def _build_workbook_data(
    *,
    s01: pd.DataFrame,
    s05: pd.DataFrame,
    kbqs: pd.DataFrame,
    mc_result: pd.DataFrame,
    cal_detail: pd.DataFrame,
    account_capital: pd.DataFrame,
    source_name: str,
) -> WorkbookData:
    kbqs = kbqs.copy()
    kbqs["来源文件"] = source_name
    _validate_kbqs_exposures(kbqs)
    interest_factor_table = _build_interest_factor_table(mc_result)
    if interest_factor_table.empty:
        raise WorkbookValidationError("MC_RESULT_资产端利率风险明细表无法反推利率风险抵减因子")
    kbqs = _enrich_interest_risk_from_mc_result(kbqs, mc_result)
    spread_factor_table = _build_spread_factor_table(cal_detail)
    metrics = _extract_metrics(s01)
    risk_factor_table, calibration_checks = build_calibration(cal_detail, kbqs, s05)

    return WorkbookData(
        metrics=metrics,
        s01=s01,
        s05=s05,
        kbqs=kbqs,
        interest_factor_table=interest_factor_table,
        spread_factor_table=spread_factor_table,
        account_capital=account_capital,
        source_name=source_name,
        risk_factor_table=risk_factor_table,
        calibration_checks=calibration_checks,
        cal_detail=cal_detail,
        mc_detail=mc_result,
    )


def _validate_kbqs_exposures(kbqs: pd.DataFrame) -> None:
    sheet_name = str(kbqs["来源工作表"].iloc[0]) if "来源工作表" in kbqs and not kbqs.empty else "KBQS_V_"
    for col in EXPOSURE_COLUMNS:
        if col not in kbqs:
            raise WorkbookValidationError(f"{sheet_name} 缺少必要字段: {col}")
        numeric = pd.to_numeric(kbqs[col], errors="coerce")
        invalid = numeric.isna() | ~numeric.map(math.isfinite)
        if invalid.any():
            if "来源行" in kbqs:
                rows = ", ".join(str(value) for value in kbqs.loc[invalid, "来源行"].head(8).tolist())
            else:
                rows = ", ".join(str(index + 2) for index in kbqs.index[invalid][:8])
            raise WorkbookValidationError(
                f"{sheet_name} 的 {col} 有 {int(invalid.sum())} 条空值或无效数值，"
                f"Excel 行号 {rows}；未自动补零，请核查原始底稿"
            )
        kbqs[col] = numeric


def load_baseline_metrics(source: str | Path | BinaryIO) -> BaselineMetrics:
    if _source_extension(source) == ".parquet":
        s01 = _read_parquet_tables(source, required=("s01",))["s01"]
        return _extract_metrics(s01)

    with pd.ExcelFile(source, engine="openpyxl") as excel:
        sheet_name = _resolve_sheet(excel.sheet_names, SHEET_PREFIXES["s01"])
        s01 = _read_report_sheet(excel, sheet_name)
    return _extract_metrics(s01)


def save_workbook_as_parquet(
    source: str | Path,
    destination: str | Path | None = None,
) -> Path:
    """Parse one local Excel workbook and atomically write a Parquet bundle."""

    source_path = Path(source)
    if source_path.suffix.lower() != ".xlsx":
        raise WorkbookValidationError("Excel 转换输入必须是 .xlsx 文件")
    if not source_path.exists():
        raise WorkbookValidationError(f"找不到 Excel 输入文件: {source_path}")

    destination_path = Path(destination) if destination is not None else PARQUET_SOURCE_DIR / f"{source_path.stem}.parquet"
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.parent / f".{destination_path.stem}.{uuid.uuid4().hex}.parquet"
    try:
        with pd.ExcelFile(source_path, engine="openpyxl") as excel:
            tables = _read_excel_tables(excel, strict_exposures=False)
        _write_parquet_bundle(tables, source_path.name, temporary_path)
        os.replace(temporary_path, destination_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return destination_path


def install_uploaded_parquet(
    filename: str,
    content: bytes,
    folder: str | Path = WORKBOOK_SOURCE_DIR,
) -> UploadedParquetResult:
    """Validate and atomically install one processed Parquet bundle."""

    if not isinstance(filename, str) or not filename.strip():
        raise WorkbookValidationError("上传文件名不能为空")
    if filename != filename.strip():
        raise WorkbookValidationError("上传文件名不能以空格开头或结尾")
    if filename.startswith("~$") or filename.startswith("."):
        raise WorkbookValidationError("不能上传临时或隐藏文件")
    if "/" in filename or "\\" in filename or Path(filename).name != filename:
        raise WorkbookValidationError("上传文件名不能包含目录路径")
    if not filename.lower().endswith(".parquet"):
        raise WorkbookValidationError("只支持已处理的 .parquet 月度数据")

    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise WorkbookValidationError("上传内容不是有效的二进制文件")
    payload = bytes(content)
    size_bytes = len(payload)
    if size_bytes == 0:
        raise WorkbookValidationError("上传文件为空")
    if size_bytes > MAX_UPLOAD_BYTES:
        raise WorkbookValidationError(
            f"上传文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )

    source_dir = Path(folder)
    if not source_dir.exists() or not source_dir.is_dir():
        raise WorkbookValidationError(f"Parquet 数据目录不存在或不可用: {source_dir}")
    source_dir = source_dir.resolve()
    target_path = (source_dir / filename).resolve()
    if target_path.parent != source_dir:
        raise WorkbookValidationError("上传文件名不能指向数据目录之外")
    parse_workbook_source(target_path)

    replaced_existing = target_path.exists()
    temporary_path = source_dir / f".{target_path.stem}.{uuid.uuid4().hex}.parquet"
    try:
        with temporary_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        _read_parquet_tables(temporary_path, required=PARQUET_TABLES)
        load_workbook_data(temporary_path)
        os.replace(temporary_path, target_path)
        installed_source = parse_workbook_source(target_path)
        return UploadedParquetResult(installed_source, replaced_existing, size_bytes)
    except WorkbookValidationError:
        raise
    except Exception as exc:
        raise WorkbookValidationError(f"Parquet 数据校验失败: {exc}") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _source_extension(source: str | Path | BinaryIO) -> str:
    if isinstance(source, (str, Path)):
        return Path(source).suffix.lower()
    name = getattr(source, "name", None)
    return Path(str(name)).suffix.lower() if name else ""


def _source_name(source: str | Path | BinaryIO) -> str:
    return str(getattr(source, "name", None) or source)


def _write_parquet_bundle(
    tables: dict[str, pd.DataFrame],
    source_filename: str,
    destination: str | Path,
) -> None:
    rows = []
    for table_name in PARQUET_TABLES:
        frame = tables[table_name if table_name != "mc_detail" else "mc_result"]
        rows.append(
            {
                "format_version": PARQUET_FORMAT_VERSION,
                "source_filename": source_filename,
                "table_name": table_name,
                "payload_json": frame.to_json(
                    orient="split",
                    date_format="iso",
                    force_ascii=False,
                    default_handler=str,
                ),
            }
        )
    pd.DataFrame(rows).to_parquet(
        destination,
        engine="pyarrow",
        compression="zstd",
        index=False,
    )


def _read_parquet_tables(
    source: str | Path | BinaryIO,
    *,
    required: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    try:
        bundle = pd.read_parquet(source, engine="pyarrow")
    except Exception as exc:
        raise WorkbookValidationError("文件不是有效的 Parquet 数据包") from exc

    required_columns = {"format_version", "source_filename", "table_name", "payload_json"}
    missing_columns = required_columns.difference(bundle.columns)
    if missing_columns:
        raise WorkbookValidationError(
            "Parquet 数据包缺少必要字段: " + ", ".join(sorted(missing_columns))
        )
    versions = {str(value) for value in bundle["format_version"].dropna().tolist()}
    if versions != {PARQUET_FORMAT_VERSION}:
        raise WorkbookValidationError("Parquet 数据包版本不受当前应用支持")

    tables: dict[str, pd.DataFrame] = {}
    for row in bundle.itertuples(index=False):
        table_name = str(row.table_name)
        if table_name in tables:
            raise WorkbookValidationError(f"Parquet 数据包包含重复逻辑表: {table_name}")
        try:
            payload = json.loads(str(row.payload_json))
            tables[table_name] = pd.DataFrame(
                payload["data"],
                columns=payload["columns"],
                index=payload.get("index"),
            )
        except Exception as exc:
            raise WorkbookValidationError(f"Parquet 逻辑表 {table_name} 无法恢复") from exc

    missing_tables = set(required).difference(tables)
    if missing_tables:
        raise WorkbookValidationError(
            "Parquet 数据包缺少必要逻辑表: " + ", ".join(sorted(missing_tables))
        )
    return tables


def _parse_yyyymmdd(raw: str, source_name: str) -> date:
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError as exc:
        raise WorkbookValidationError(f"{source_name} 包含无效日期: {raw}") from exc


def _version_rank(version_suffix: str) -> int:
    if not version_suffix:
        return 0
    match = re.fullmatch(r"[vV](\d+)", version_suffix)
    if match:
        return int(match.group(1))
    return 1


def _resolve_sheet(sheet_names: list[str], prefix: str) -> str:
    match = next((name for name in sheet_names if name.startswith(prefix)), None)
    if match is None:
        raise WorkbookValidationError(f"工作簿缺少必要 sheet: {prefix}")
    return match


def _resolve_sheets(sheet_names: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, prefix in SHEET_PREFIXES.items():
        try:
            resolved[key] = _resolve_sheet(sheet_names, prefix)
        except WorkbookValidationError:
            missing.append(prefix)
    if missing:
        raise WorkbookValidationError(
            "工作簿缺少必要 sheet: " + ", ".join(missing)
        )
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


def _read_kbqs_sheet(
    excel: pd.ExcelFile,
    sheet_name: str,
    *,
    strict_exposures: bool = True,
) -> pd.DataFrame:
    df = pd.read_excel(excel, sheet_name=sheet_name, header=0)
    required = {"账户", "资产类型", *EXPOSURE_COLUMNS}
    missing = required.difference(df.columns)
    if missing:
        raise WorkbookValidationError(
            f"{sheet_name} 缺少必要字段: " + ", ".join(sorted(missing))
        )

    keep = ["表名", "账户", "证券名称", "证券代码", "资产类型", *EXPOSURE_COLUMNS,
            "穿透情况", "交易结构层级", "资产树标识符", "交易对手", "产品发行人",
            "币种分类", "投资市场", "所在国家_地区", "到期日", "境外风险暴露"]
    existing = [col for col in keep if col in df.columns]
    df = df[existing].copy()
    df["账户"] = df["账户"].fillna("未分类账户").astype(str).str.strip()
    df["资产类型"] = df["资产类型"].fillna("未分类资产").astype(str).str.strip()
    for col in EXPOSURE_COLUMNS:
        numeric = pd.to_numeric(df[col], errors="coerce")
        invalid = numeric.isna() | ~numeric.map(math.isfinite)
        if invalid.any() and strict_exposures:
            rows = ", ".join(str(index + 2) for index in df.index[invalid][:8])
            raise WorkbookValidationError(
                f"{sheet_name} 的 {col} 有 {int(invalid.sum())} 条空值或无效数值，"
                f"Excel 行号 {rows}；未自动补零，请核查原始底稿"
            )
        df[col] = numeric
    df["原始利率风险暴露"] = df["利率风险暴露"]
    df["来源工作表"] = sheet_name
    df["来源行"] = df.index + 2
    return df


def _read_mc_result_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(excel, sheet_name=sheet_name, header=1)
    required = {"账户", "资产类型", "账面价值净价", "应收利息", "资产端利率风险MC"}
    missing = required.difference(df.columns)
    if missing:
        return pd.DataFrame()
    keep = [
        "账户",
        "资产类型",
        "证券名称",
        "证券代码",
        "资产ID",
        "资产树标识符",
        "交易结构层级",
        "到期日",
        "账面价值净价",
        "应收利息",
        "修正久期",
        "PV基础",
        "资产端利率风险MC",
    ]
    keep = [col for col in keep if col in df.columns]
    df = df[keep].copy()
    df["账户"] = df["账户"].fillna("未分类账户").astype(str).str.strip()
    df["资产类型"] = df["资产类型"].fillna("未分类资产").astype(str).str.strip()
    for col in ["账面价值净价", "应收利息", "修正久期", "PV基础", "资产端利率风险MC"]:
        if col not in df.columns:
            df[col] = float("nan") if col == "修正久期" else 0.0
        numeric = pd.to_numeric(df[col], errors="coerce")
        df[col] = numeric if col == "修正久期" else numeric.fillna(0.0)
    df["利率风险资产价值"] = df["账面价值净价"] + df["应收利息"]
    df["来源工作表"] = sheet_name
    df["来源行"] = df.index + 3
    return df


def _build_interest_factor_table(mc_result: pd.DataFrame) -> pd.DataFrame:
    if mc_result.empty:
        return pd.DataFrame()

    df = mc_result.copy()
    df = df[df["利率风险资产价值"] > 0].copy()
    if df.empty:
        return pd.DataFrame()

    valid_duration = df["修正久期"].map(math.isfinite) & df["修正久期"].ge(0)
    df["久期桶"] = _duration_bucket(df["修正久期"])
    df.loc[~valid_duration, "久期桶"] = "久期待核对"
    df["久期有效价值"] = df["利率风险资产价值"].where(valid_duration, 0.0)
    df["久期加权值"] = (df["修正久期"] * df["利率风险资产价值"]).where(valid_duration, 0.0)
    rows = []

    by_asset = (
        df.groupby("资产类型", dropna=False)[["利率风险资产价值", "PV基础", "资产端利率风险MC", "久期有效价值", "久期加权值"]]
        .sum()
        .reset_index()
    )
    by_asset["久期桶"] = "存量平均"
    rows.append(by_asset)

    by_bucket = (
        df.groupby(["资产类型", "久期桶"], dropna=False)[["利率风险资产价值", "PV基础", "资产端利率风险MC", "久期有效价值", "久期加权值"]]
        .sum()
        .reset_index()
    )
    rows.append(by_bucket)

    out = pd.concat(rows, ignore_index=True)
    out["利率风险抵减因子"] = _safe_series_div(out["资产端利率风险MC"], out["利率风险资产价值"])
    out["PV口径抵减因子"] = _safe_series_div(out["资产端利率风险MC"], out["PV基础"])
    out["加权修正久期"] = out["久期加权值"] / out["久期有效价值"].replace(0, float("nan"))
    out["久期覆盖率"] = out["久期有效价值"] / out["利率风险资产价值"]
    out["来源/口径"] = "MC_RESULT_资产端利率风险明细表反推"
    return out[
        [
            "资产类型",
            "久期桶",
            "利率风险资产价值",
            "PV基础",
            "资产端利率风险MC",
            "利率风险抵减因子",
            "PV口径抵减因子",
            "加权修正久期",
            "久期覆盖率",
            "来源/口径",
        ]
    ]


def _duration_bucket(duration: pd.Series) -> pd.Series:
    bins = [-float("inf"), 3, 5, 7, 10, 15, 30, float("inf")]
    labels = ["<3年", "3-5年", "5-7年", "7-10年", "10-15年", "15-30年", "30年以上"]
    return pd.cut(duration, bins=bins, labels=labels, right=False).astype(str)


def _read_cal_detail_sheet(excel: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    upper = pd.read_excel(excel, sheet_name=sheet_name, header=None, nrows=1).iloc[0].ffill()
    df = pd.read_excel(excel, sheet_name=sheet_name, header=1)
    required = {"资产类型", "认可价值", "风险暴露.1", "RF.1", "MC.1"}
    missing = required.difference(df.columns)
    if missing:
        return pd.DataFrame()
    for risk, (mc, exposure, flag, _) in RISK_COLUMNS.items():
        for col in (mc, exposure):
            if col not in df or str(upper.iloc[df.columns.get_loc(col)]).strip() != risk:
                raise WorkbookValidationError(f"{sheet_name} 的 {col} 风险表头与 {risk} 不符，拒绝按列位置猜测")
    keep = ["资产类型", "认可价值", "RF.1", "账户类别", "资产名称", "资产代码", "资产ID", "资产树标识",
            "交易结构层级", "穿透情况", "修正久期", "资产信用评级", "发行主体信用评级",
            "境内申万一级行业", "境外GICS行业分类", "币种", "投资市场", "到期日", "源表名"]
    keep += [col for mc, ex, flag, _ in RISK_COLUMNS.values() for col in (mc, ex, flag)]
    df = df[[col for col in keep if col in df]].copy()
    # The first data row contains units, not an asset. Never drop a labelled asset
    # merely because its value is invalid; the calibration controls must catch it.
    df = df[df["资产类型"].notna()].copy()
    df["资产类型"] = df["资产类型"].fillna("未分类资产").astype(str).str.strip()
    df["认可价值"] = pd.to_numeric(df["认可价值"], errors="coerce")
    for original, alias in {"风险暴露.1": "利差风险暴露", "RF.1": "利差风险RF", "MC.1": "利差风险MC"}.items():
        df[alias] = pd.to_numeric(df[original], errors="coerce").fillna(0.0)
    df["来源工作表"] = sheet_name
    df["来源行"] = df.index + 3
    return df


def _build_spread_factor_table(cal_detail: pd.DataFrame) -> pd.DataFrame:
    if cal_detail.empty:
        return pd.DataFrame()
    df = cal_detail[cal_detail["利差风险暴露"] > 0].copy()
    if df.empty:
        return pd.DataFrame()
    out = (
        df.groupby("资产类型", dropna=False)[["认可价值", "利差风险暴露", "利差风险MC"]]
        .sum()
        .reset_index()
    )
    out["利差风险因子"] = _safe_series_div(out["利差风险MC"], out["利差风险暴露"])
    out["来源/口径"] = "CAL_DETAIL_最低资本明细表反推"
    return out[["资产类型", "认可价值", "利差风险暴露", "利差风险MC", "利差风险因子", "来源/口径"]]


def _enrich_interest_risk_from_mc_result(kbqs: pd.DataFrame, mc_result: pd.DataFrame) -> pd.DataFrame:
    if mc_result.empty:
        return kbqs

    enriched = kbqs.copy()
    by_account_asset = (
        mc_result.groupby(["账户", "资产类型"], dropna=False)[["利率风险资产价值", "PV基础", "资产端利率风险MC"]]
        .sum()
        .reset_index()
    )
    by_account_asset["账户资产PV口径利率风险率"] = _safe_series_div(
        by_account_asset["资产端利率风险MC"], by_account_asset["PV基础"]
    )
    by_account_asset["账户资产资产价值口径利率风险率"] = _safe_series_div(
        by_account_asset["资产端利率风险MC"], by_account_asset["利率风险资产价值"]
    )

    by_asset = (
        mc_result.groupby("资产类型", dropna=False)[["利率风险资产价值", "PV基础", "资产端利率风险MC"]]
        .sum()
        .reset_index()
    )
    by_asset["资产类型PV口径利率风险率"] = _safe_series_div(
        by_asset["资产端利率风险MC"], by_asset["PV基础"]
    )
    by_asset["资产类型资产价值口径利率风险率"] = _safe_series_div(
        by_asset["资产端利率风险MC"], by_asset["利率风险资产价值"]
    )

    enriched = enriched.merge(
        by_account_asset[
            [
                "账户",
                "资产类型",
                "账户资产PV口径利率风险率",
                "账户资产资产价值口径利率风险率",
            ]
        ],
        on=["账户", "资产类型"],
        how="left",
    ).merge(
        by_asset[
            [
                "资产类型",
                "资产类型PV口径利率风险率",
                "资产类型资产价值口径利率风险率",
            ]
        ],
        on="资产类型",
        how="left",
    )

    rate = (
        enriched["账户资产PV口径利率风险率"]
        .fillna(enriched["资产类型PV口径利率风险率"])
        .fillna(enriched["账户资产资产价值口径利率风险率"])
        .fillna(enriched["资产类型资产价值口径利率风险率"])
        .fillna(0.0)
    )
    enriched["利率风险暴露"] = enriched["认可价值"] * rate
    return enriched.drop(
        columns=[
            "账户资产PV口径利率风险率",
            "账户资产资产价值口径利率风险率",
            "资产类型PV口径利率风险率",
            "资产类型资产价值口径利率风险率",
        ]
    )


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

    core_capital = _required(values, "核心一级资本") + _required(values, "核心二级资本")
    minimum_capital = _required(values, "最低资本")
    if minimum_capital <= 0:
        raise WorkbookValidationError("S01 最低资本必须大于 0，不能计算充足率")
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
    if value is None or not math.isfinite(value):
        raise WorkbookValidationError(f"S01 缺少必要指标或数值无效: {key}")
    return value


def _to_float(value: object) -> float:
    if pd.isna(value):
        return float("nan")
    try:
        return float(value)
    except (ValueError, TypeError):
        return float("nan")


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _safe_series_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, pd.NA)
    return (numerator / denominator).fillna(0.0)
