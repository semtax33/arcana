from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from engine.core.paths import (
    fiscal_quarter_from_month,
    parse_statement_snapshot_filename,
    statement_symbol_name,
)


STATEMENT_PERIOD_COLUMNS = ["fiscal_year", "fiscal_month", "fiscal_quarter"]
VALID_STATEMENT_MONTHS = {3, 6, 9, 12}


def statement_output_columns(base_columns: Iterable[str]) -> list[str]:
    columns = list(base_columns)
    for column in STATEMENT_PERIOD_COLUMNS:
        if column not in columns:
            columns.append(column)
    return columns


def add_statement_period_columns(
    df: pd.DataFrame,
    year: int,
    month: int,
) -> pd.DataFrame:
    result = df.copy()
    result["fiscal_year"] = int(year)
    result["fiscal_month"] = int(month)
    result["fiscal_quarter"] = fiscal_quarter_from_month(int(month))
    return result


def coerce_statement_period_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    if ("fiscal_year" not in result.columns or "fiscal_month" not in result.columns) and "period" in result.columns:
        period = result["period"].astype(str).str.extract(r"(?P<year>\d{4})[._](?P<month>\d{1,2})")
        if "fiscal_year" not in result.columns:
            result["fiscal_year"] = period["year"]
        if "fiscal_month" not in result.columns:
            result["fiscal_month"] = period["month"]

    if "fiscal_year" not in result.columns:
        result["fiscal_year"] = pd.NA
    if "fiscal_month" not in result.columns:
        result["fiscal_month"] = pd.NA

    result["fiscal_year"] = pd.to_numeric(result["fiscal_year"], errors="coerce").astype("Int64")
    result["fiscal_month"] = pd.to_numeric(result["fiscal_month"], errors="coerce").astype("Int64")
    if "fiscal_quarter" not in result.columns:
        result["fiscal_quarter"] = ((result["fiscal_month"] - 1) // 3) + 1
    result["fiscal_quarter"] = pd.to_numeric(result["fiscal_quarter"], errors="coerce").astype("Int64")
    return result


def consolidated_statement_path(
    financial_dir: str | Path,
    stock_code: Any,
    *,
    market: str = "kr",
) -> Path:
    return Path(financial_dir) / statement_symbol_name(stock_code, market=market)


def consolidated_statement_debug_path(
    financial_dir: str | Path,
    stock_code: Any,
    *,
    market: str = "kr",
) -> Path:
    return consolidated_statement_path(financial_dir, stock_code, market=market).with_suffix(".debug.csv")


def legacy_statement_snapshot_files(
    stock_code: Any,
    financial_dir: str | Path,
    *,
    market: str = "kr",
    months: set[int] | None = None,
) -> list[Path]:
    months = months or VALID_STATEMENT_MONTHS
    paths_by_period: dict[tuple[int, int], Path] = {}

    for path in Path(financial_dir).glob(f"*normalized_{stock_code}_*.csv"):
        if ".debug" in path.name or ".validation" in path.name:
            continue
        meta = parse_statement_snapshot_filename(path)
        if meta is None:
            continue
        month = int(meta["month"])
        if month not in months:
            continue
        key = (int(meta["year"]), month)
        if key not in paths_by_period or path.name.startswith(f"{market}_"):
            paths_by_period[key] = path

    return sorted(
        paths_by_period.values(),
        key=lambda p: (
            int(parse_statement_snapshot_filename(p)["year"]),
            int(parse_statement_snapshot_filename(p)["month"]),
        ),
    )


def read_statement_period_frames(
    stock_code: Any,
    financial_dir: str | Path,
    *,
    market: str = "kr",
    months: set[int] | None = None,
) -> list[tuple[int, int, pd.DataFrame]]:
    months = months or VALID_STATEMENT_MONTHS
    consolidated_path = consolidated_statement_path(financial_dir, stock_code, market=market)
    if consolidated_path.exists():
        df = coerce_statement_period_columns(pd.read_csv(consolidated_path))
        df = df.dropna(subset=["fiscal_year", "fiscal_month"])
        df = df[df["fiscal_month"].astype(int).isin(months)].copy()
        frames: list[tuple[int, int, pd.DataFrame]] = []
        for (year, month), group in df.groupby(["fiscal_year", "fiscal_month"], sort=True):
            frames.append((int(year), int(month), group.copy()))
        return frames

    frames = []
    for path in legacy_statement_snapshot_files(stock_code, financial_dir, market=market, months=months):
        meta = parse_statement_snapshot_filename(path)
        if meta is None:
            continue
        year = int(meta["year"])
        month = int(meta["month"])
        df = add_statement_period_columns(pd.read_csv(path), year, month)
        frames.append((year, month, df))
    return frames


def consolidate_statement_snapshots(
    stock_code: Any,
    financial_dir: str | Path,
    *,
    market: str = "kr",
    columns: Iterable[str] | None = None,
    output_dir: str | Path | None = None,
) -> Path | None:
    financial_dir = Path(financial_dir)
    frames = []
    for path in legacy_statement_snapshot_files(stock_code, financial_dir, market=market):
        meta = parse_statement_snapshot_filename(path)
        if meta is None:
            continue
        df = pd.read_csv(path)
        if df.empty:
            continue
        frames.append(add_statement_period_columns(df, int(meta["year"]), int(meta["month"])))

    if not frames:
        return None

    output = pd.concat(frames, ignore_index=True)
    output = coerce_statement_period_columns(output)
    output = output.sort_values(["fiscal_year", "fiscal_month"], kind="stable")
    if columns is not None:
        output = output.reindex(columns=statement_output_columns(columns))

    destination_dir = Path(output_dir) if output_dir is not None else financial_dir
    path = consolidated_statement_path(destination_dir, stock_code, market=market)
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    return path


def consolidate_statement_debug_snapshots(
    stock_code: Any,
    financial_dir: str | Path,
    *,
    market: str = "kr",
    output_dir: str | Path | None = None,
) -> Path | None:
    financial_dir = Path(financial_dir)
    frames = []
    for path in legacy_statement_snapshot_files(stock_code, financial_dir, market=market):
        meta = parse_statement_snapshot_filename(path)
        if meta is None:
            continue

        debug_path = path.with_suffix(".debug.csv")
        if not debug_path.exists():
            continue

        df = pd.read_csv(debug_path)
        if df.empty:
            continue
        frames.append(add_statement_period_columns(df, int(meta["year"]), int(meta["month"])))

    if not frames:
        return None

    output = pd.concat(frames, ignore_index=True)
    output = coerce_statement_period_columns(output)
    output = output.sort_values(["fiscal_year", "fiscal_month"], kind="stable")

    destination_dir = Path(output_dir) if output_dir is not None else financial_dir
    path = consolidated_statement_debug_path(destination_dir, stock_code, market=market)
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    return path


def write_consolidated_statement_rows(
    rows: list[dict[str, Any]],
    stock_code: Any,
    financial_dir: str | Path,
    *,
    market: str = "kr",
    columns: Iterable[str],
) -> Path | None:
    if not rows:
        return None

    output = coerce_statement_period_columns(pd.DataFrame(rows))
    output = output.sort_values(["fiscal_year", "fiscal_month"], kind="stable")
    output = output.reindex(columns=statement_output_columns(columns))

    path = consolidated_statement_path(financial_dir, stock_code, market=market)
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    return path
