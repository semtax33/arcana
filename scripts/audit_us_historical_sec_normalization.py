from __future__ import annotations

"""Audit the frozen US SEC normalization scope before ClickHouse mutation."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from engine.core.paths import DATA_LAKE, statement_symbol_name


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_NORMALIZED_DIR = DATA_LAKE.silver("sec", "normalized")
DEFAULT_METADATA_PATH = DATA_LAKE.silver("sec", "us_report_metadata.csv")
DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "us_historical_2006_2016_normalization_audit.json"
)
NORMALIZED_KEY = ["fiscal_year", "fiscal_month", "canonical_account_id"]
VALID_SOURCES = {
    "filing_xbrl",
    "companyfacts_primary",
    "companyfacts_alternate",
    "companyfacts_label",
    "notes",
    "edgartools",
    "derived_formula",
}
VALID_REPORT_NAMES = {"10-K", "10-Q", "10-K/A", "10-Q/A"}


def read_target_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = {
            str(row.get("security_id") or "").strip().removeprefix("SEC_US_").upper()
            for row in csv.DictReader(stream)
        }
    return sorted(symbol for symbol in symbols if symbol)


def _read_selected(path: Path, columns: set[str]) -> pd.DataFrame:
    try:
        return pd.read_csv(
            path,
            usecols=lambda column: column in columns,
            low_memory=False,
        )
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _scope(frame: pd.DataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    years = pd.to_numeric(frame.get("fiscal_year"), errors="coerce")
    return frame.loc[years.between(start_year, end_year, inclusive="both")].copy()


def _key_set(frame: pd.DataFrame) -> set[tuple[int, int, str]]:
    years = pd.to_numeric(frame["fiscal_year"], errors="coerce")
    months = pd.to_numeric(frame["fiscal_month"], errors="coerce")
    accounts = frame["canonical_account_id"].fillna("").astype(str).str.strip()
    return {
        (int(year), int(month), account)
        for year, month, account in zip(years, months, accounts, strict=True)
        if pd.notna(year) and pd.notna(month) and account
    }


def _symbol_audit(
    symbol: str,
    *,
    normalized_dir: Path,
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    path = normalized_dir / statement_symbol_name(symbol, market="us")
    if not path.exists():
        return {
            "symbol": symbol,
            "status": "gap",
            "normalized_rows": 0,
            "source_rows": {},
            "violations": [],
        }

    required = {*NORMALIZED_KEY, "normalized_amount"}
    frame = _read_selected(path, required)
    missing = sorted(required - set(frame.columns))
    violations: list[str] = []
    if missing:
        violations.append(f"normalized_missing_columns:{','.join(missing)}")
        return {
            "symbol": symbol,
            "status": "invalid",
            "normalized_rows": 0,
            "source_rows": {},
            "violations": violations,
        }

    frame = _scope(frame, start_year, end_year)
    if frame.empty:
        return {
            "symbol": symbol,
            "status": "gap",
            "normalized_rows": 0,
            "source_rows": {},
            "violations": [],
        }

    years = pd.to_numeric(frame["fiscal_year"], errors="coerce")
    months = pd.to_numeric(frame["fiscal_month"], errors="coerce")
    accounts = frame["canonical_account_id"].fillna("").astype(str).str.strip()
    amounts = pd.to_numeric(frame["normalized_amount"], errors="coerce")
    if (~months.between(1, 12, inclusive="both")).any():
        violations.append("invalid_fiscal_month")
    if accounts.eq("").any():
        violations.append("blank_canonical_account_id")
    if amounts.map(lambda value: not math.isfinite(value) if pd.notna(value) else True).any():
        violations.append("non_finite_normalized_amount")
    keyed = pd.DataFrame(
        {
            "fiscal_year": years,
            "fiscal_month": months,
            "canonical_account_id": accounts,
        }
    )
    if keyed.duplicated(NORMALIZED_KEY, keep=False).any():
        violations.append("duplicate_canonical_period_key")

    debug_path = path.with_suffix(".debug.csv")
    source_rows: Counter[str] = Counter()
    if not debug_path.exists():
        violations.append("missing_debug_provenance")
    else:
        debug_required = {*NORMALIZED_KEY, "source"}
        debug = _read_selected(debug_path, debug_required)
        missing_debug = sorted(debug_required - set(debug.columns))
        if missing_debug:
            violations.append(f"debug_missing_columns:{','.join(missing_debug)}")
        else:
            debug = _scope(debug, start_year, end_year)
            sources = debug["source"].fillna("").astype(str).str.strip()
            source_rows.update(sources)
            unknown_sources = sorted(set(sources) - VALID_SOURCES)
            if unknown_sources:
                violations.append(f"unknown_sources:{','.join(unknown_sources)}")
            normalized_keys = _key_set(frame)
            debug_keys = _key_set(debug)
            if normalized_keys != debug_keys:
                violations.append(
                    "debug_normalized_key_mismatch:"
                    f"missing={len(normalized_keys - debug_keys)},"
                    f"extra={len(debug_keys - normalized_keys)}"
                )

    return {
        "symbol": symbol,
        "status": "invalid" if violations else "ok",
        "normalized_rows": len(frame),
        "source_rows": dict(sorted(source_rows.items())),
        "violations": violations,
    }


def _metadata_audit(
    path: Path,
    *,
    symbols: set[str],
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    required = {
        "security_id",
        "stock_code",
        "fiscal_year",
        "fiscal_month",
        "period_end_date",
        "report_date",
        "rcept_no",
        "report_name",
        "source_type",
    }
    if not path.exists():
        return {
            "rows": 0,
            "symbols": 0,
            "violations": ["metadata_file_missing"],
            "max_report_lag_days": None,
        }
    frame = _read_selected(path, required)
    missing = sorted(required - set(frame.columns))
    if missing:
        return {
            "rows": 0,
            "symbols": 0,
            "violations": [f"metadata_missing_columns:{','.join(missing)}"],
            "max_report_lag_days": None,
        }

    frame["stock_code"] = frame["stock_code"].fillna("").astype(str).str.strip().str.upper()
    frame = frame.loc[frame["stock_code"].isin(symbols)].copy()
    frame = _scope(frame, start_year, end_year)
    violations: list[str] = []
    if frame.empty:
        violations.append("metadata_scope_empty")
        return {
            "rows": 0,
            "symbols": 0,
            "violations": violations,
            "max_report_lag_days": None,
        }

    months = pd.to_numeric(frame["fiscal_month"], errors="coerce")
    if (~months.between(1, 12, inclusive="both")).any():
        violations.append("metadata_invalid_fiscal_month")
    for column in ("security_id", "stock_code", "rcept_no", "report_name", "source_type"):
        if frame[column].fillna("").astype(str).str.strip().eq("").any():
            violations.append(f"metadata_blank_{column}")
    if (~frame["security_id"].fillna("").astype(str).str.startswith("SEC_US_")).any():
        violations.append("metadata_non_us_security_id")
    if (
        ~frame["report_name"]
        .fillna("")
        .astype(str)
        .str.upper()
        .isin(VALID_REPORT_NAMES)
    ).any():
        violations.append("metadata_unsupported_report_name")
    period_end = pd.to_datetime(frame["period_end_date"], errors="coerce")
    report_date = pd.to_datetime(frame["report_date"], errors="coerce")
    if period_end.isna().any():
        violations.append("metadata_missing_period_end_date")
    if report_date.isna().any():
        violations.append("metadata_missing_report_date")
    lag = (report_date - period_end).dt.days
    if (lag.dropna() < 0).any():
        violations.append("metadata_report_date_before_period_end")
    duplicate_key = ["stock_code", "fiscal_year", "fiscal_month", "source_type"]
    if frame.duplicated(duplicate_key, keep=False).any():
        violations.append("metadata_duplicate_period_key")
    max_lag = int(lag.max()) if lag.notna().any() else None
    return {
        "rows": len(frame),
        "symbols": int(frame["stock_code"].nunique()),
        "violations": violations,
        "max_report_lag_days": max_lag,
        "report_lag_over_370_rows": int((lag > 370).sum()),
    }


def audit_normalization(
    symbols: Iterable[str],
    *,
    normalized_dir: Path,
    metadata_path: Path,
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    normalized_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    symbol_results = [
        _symbol_audit(
            symbol,
            normalized_dir=normalized_dir,
            start_year=start_year,
            end_year=end_year,
        )
        for symbol in normalized_symbols
    ]
    source_rows: Counter[str] = Counter()
    violations: list[dict[str, str]] = []
    gaps: list[str] = []
    normalized_rows = 0
    ok_symbols: set[str] = set()
    for result in symbol_results:
        normalized_rows += int(result["normalized_rows"])
        source_rows.update(result["source_rows"])
        if result["status"] == "gap":
            gaps.append(result["symbol"])
        elif result["status"] == "ok":
            ok_symbols.add(result["symbol"])
        for reason in result["violations"]:
            violations.append({"scope": result["symbol"], "reason": reason})

    metadata = _metadata_audit(
        metadata_path,
        symbols=set(normalized_symbols),
        start_year=start_year,
        end_year=end_year,
    )
    for reason in metadata["violations"]:
        violations.append({"scope": "report_metadata", "reason": reason})
    metadata_symbols = set()
    if metadata_path.exists():
        metadata_frame = _read_selected(metadata_path, {"stock_code", "fiscal_year"})
        if {"stock_code", "fiscal_year"}.issubset(metadata_frame.columns):
            metadata_frame["stock_code"] = (
                metadata_frame["stock_code"].fillna("").astype(str).str.strip().str.upper()
            )
            metadata_frame = _scope(metadata_frame, start_year, end_year)
            metadata_symbols = set(metadata_frame["stock_code"]) & set(normalized_symbols)
    missing_metadata = sorted(ok_symbols - metadata_symbols)
    for symbol in missing_metadata:
        violations.append({"scope": symbol, "reason": "normalized_rows_without_report_metadata"})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": "us",
        "start_year": start_year,
        "end_year": end_year,
        "target_count": len(normalized_symbols),
        "normalized_symbol_count": len(ok_symbols),
        "normalized_row_count": normalized_rows,
        "explicit_gap_count": len(gaps),
        "explicit_gap_symbols": gaps,
        "source_row_counts": dict(sorted(source_rows.items())),
        "metadata": metadata,
        "missing_metadata_symbols": missing_metadata,
        "violation_count": len(violations),
        "violations": violations,
        "passed": not violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--normalized-dir", type=Path, default=DEFAULT_NORMALIZED_DIR)
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()
    if args.start_year > args.end_year:
        raise ValueError("start-year must not be after end-year")
    report = audit_normalization(
        read_target_symbols(args.target_path),
        normalized_dir=args.normalized_dir,
        metadata_path=args.metadata_path,
        start_year=args.start_year,
        end_year=args.end_year,
    )
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "[AUDIT] "
        f"passed={report['passed']}, targets={report['target_count']:,}, "
        f"normalized={report['normalized_symbol_count']:,}, "
        f"gaps={report['explicit_gap_count']:,}, rows={report['normalized_row_count']:,}, "
        f"violations={report['violation_count']:,}, report={args.report_path}",
        flush=True,
    )
    if not report["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
