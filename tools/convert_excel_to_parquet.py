"""Convert local Excel source workbooks into Streamlit-ready Parquet bundles."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from solvency_app.workbook import EXCEL_SOURCE_DIR, PARQUET_SOURCE_DIR, save_workbook_as_parquet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=EXCEL_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=PARQUET_SOURCE_DIR)
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        type=Path,
        help="只转换指定 Excel；可重复传入。默认转换 source-dir 下全部 .xlsx。",
    )
    args = parser.parse_args()

    source_files = args.files or sorted(args.source_dir.glob("*.xlsx"))
    if not source_files:
        parser.error(f"没有找到 Excel 输入: {args.source_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for source in source_files:
        destination = args.output_dir / f"{source.stem}.parquet"
        saved = save_workbook_as_parquet(source, destination)
        print(f"转换完成: {source.name} -> {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
