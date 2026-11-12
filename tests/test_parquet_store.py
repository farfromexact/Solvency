from pathlib import Path
import shutil

import pytest

from solvency_app.workbook import (
    WorkbookValidationError,
    discover_workbook_sources,
    install_uploaded_parquet,
    load_workbook_data,
)


ROOT = Path(__file__).resolve().parents[1]
EXCEL_LATEST = ROOT / "origin stats" / "1000_20260930_20261013.xlsx"
PARQUET_LATEST = ROOT / "data" / "1000_20260930_20261013.parquet"
UPLOAD_TEST_ROOT = ROOT / "tests" / ".parquet_upload_test_workspace"


def test_default_runtime_source_is_parquet_only():
    sources = discover_workbook_sources()
    assert len(sources) == 9
    assert all(source.path.suffix.lower() == ".parquet" for source in sources)
    assert sources[-1].report_month == "2026-09"


def test_latest_parquet_matches_excel_calculation_inputs():
    excel = load_workbook_data(EXCEL_LATEST)
    parquet = load_workbook_data(PARQUET_LATEST)

    assert parquet.metrics.actual_capital == pytest.approx(excel.metrics.actual_capital)
    assert parquet.metrics.minimum_capital == pytest.approx(excel.metrics.minimum_capital)
    assert parquet.metrics.comprehensive_solvency_ratio == pytest.approx(excel.metrics.comprehensive_solvency_ratio)
    assert len(parquet.kbqs) == len(excel.kbqs) == 10827
    assert parquet.kbqs["认可价值"].sum() == pytest.approx(excel.kbqs["认可价值"].sum())


def test_parquet_upload_validates_and_replaces_atomically():
    shutil.rmtree(UPLOAD_TEST_ROOT, ignore_errors=True)
    target_dir = UPLOAD_TEST_ROOT / "data"
    target_dir.mkdir(parents=True)
    try:
        payload = PARQUET_LATEST.read_bytes()
        result = install_uploaded_parquet(PARQUET_LATEST.name, payload, target_dir)

        assert result.source.report_month == "2026-09"
        assert result.replaced_existing is False
        assert result.source.path.stat().st_size == len(payload)

        replaced = install_uploaded_parquet(PARQUET_LATEST.name, payload, target_dir)
        assert replaced.replaced_existing is True
        assert not list(target_dir.glob(".*.parquet"))
    finally:
        shutil.rmtree(UPLOAD_TEST_ROOT, ignore_errors=True)


def test_incomplete_historical_parquet_keeps_the_existing_data_quality_gate():
    source = ROOT / "data" / "1000_20260228_20260312.parquet"
    with pytest.raises(WorkbookValidationError, match="汇率风险暴露 有 4 条.*Excel 行号"):
        load_workbook_data(source)
