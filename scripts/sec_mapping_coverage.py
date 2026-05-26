from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_FOCUS_IDS = [
    "REVENUE",
    "COGS",
    "GROSS_PROFIT",
    "SGNA",
    "RND",
    "OPERATING_INCOME",
    "TAX_EXPENSE",
    "PBT",
    "NET_INCOME",
    "TOTAL_ASSETS",
    "TOTAL_LIABILITIES",
    "TOTAL_EQUITY",
    "CASH_AND_EQUIVALENTS",
    "SHORT_TERM_DEBT",
    "LONG_TERM_DEBT",
    "CFO",
    "CAPEX_PPE",
    "INT_PAID",
    "TAX_PAID",
    "DIV_PAID",
    "BUYBACK",
]


@dataclass(frozen=True)
class CoverageReport:
    canonical_coverage: list[dict[str, object]]
    source_contribution: list[dict[str, object]]
    symbol_year_count: int
    symbol_count: int
    row_count: int


def _normalized_files(input_dir: str | Path) -> list[Path]:
    root = Path(input_dir)
    return sorted(
        path
        for path in root.glob("us_normalized_*.csv")
        if not path.name.endswith(".debug.csv")
    )


def _debug_files(input_dir: str | Path) -> list[Path]:
    return sorted(Path(input_dir).glob("us_normalized_*.debug.csv"))


def _symbol_from_normalized_name(path: Path) -> str:
    name = path.name
    return name.removeprefix("us_normalized_").removesuffix(".debug.csv").removesuffix(".csv")


def _read_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def _annual_key(symbol: str, row: dict[str, str]) -> str | None:
    fiscal_year = str(row.get("fiscal_year", "")).strip()
    fiscal_month = str(row.get("fiscal_month", "")).strip()
    if not fiscal_year or fiscal_month != "12":
        return None
    return f"{symbol}|{fiscal_year}"


def _baseline_coverage_by_id(path: str | Path | None) -> dict[str, float]:
    if not path:
        return {}
    baseline_path = Path(path)
    if not baseline_path.exists():
        return {}
    values: dict[str, float] = {}
    for row in _read_csv_rows(baseline_path):
        canonical_id = str(row.get("canonical_id", "")).strip()
        if not canonical_id:
            continue
        try:
            values[canonical_id] = float(row.get("coverage_pct", ""))
        except ValueError:
            continue
    return values


def build_coverage_report(
    input_dir: str | Path,
    *,
    focus_ids: list[str] | None = None,
    baseline_csv: str | Path | None = None,
) -> CoverageReport:
    focus = list(dict.fromkeys(focus_ids or DEFAULT_FOCUS_IDS))
    focus_set = set(focus)
    annual_keys: set[str] = set()
    symbols: set[str] = set()
    covered_by_id = {canonical_id: set() for canonical_id in focus}
    row_count = 0

    for path in _normalized_files(input_dir):
        symbol = _symbol_from_normalized_name(path)
        symbols.add(symbol)
        for row in _read_csv_rows(path):
            row_count += 1
            key = _annual_key(symbol, row)
            if key is None:
                continue
            annual_keys.add(key)
            canonical_id = str(row.get("canonical_account_id", "")).strip()
            if canonical_id in covered_by_id:
                covered_by_id[canonical_id].add(key)

    baseline = _baseline_coverage_by_id(baseline_csv)
    denominator = len(annual_keys)
    canonical_coverage: list[dict[str, object]] = []
    for canonical_id in focus:
        covered_count = len(covered_by_id[canonical_id])
        missing_count = denominator - covered_count
        coverage_pct = (covered_count / denominator * 100.0) if denominator else 0.0
        row: dict[str, object] = {
            "canonical_id": canonical_id,
            "covered_symbol_years": covered_count,
            "missing_symbol_years": missing_count,
            "total_symbol_years": denominator,
            "coverage_pct": round(coverage_pct, 4),
        }
        if canonical_id in baseline:
            row["baseline_coverage_pct"] = round(baseline[canonical_id], 4)
            row["delta_pct"] = round(coverage_pct - baseline[canonical_id], 4)
        canonical_coverage.append(row)

    source_keys: dict[tuple[str, str], set[str]] = {}
    source_rows: dict[tuple[str, str], int] = {}
    for path in _debug_files(input_dir):
        symbol = _symbol_from_normalized_name(path)
        for row in _read_csv_rows(path):
            canonical_id = str(row.get("canonical_account_id", "")).strip()
            if canonical_id not in focus_set:
                continue
            source = str(row.get("source", "")).strip() or "unknown"
            source_key = (canonical_id, source)
            source_rows[source_key] = source_rows.get(source_key, 0) + 1
            annual_key = _annual_key(symbol, row)
            if annual_key is not None:
                source_keys.setdefault(source_key, set()).add(annual_key)

    source_contribution = [
        {
            "canonical_id": canonical_id,
            "source": source,
            "covered_symbol_years": len(source_keys.get((canonical_id, source), set())),
            "row_count": source_rows.get((canonical_id, source), 0),
        }
        for canonical_id, source in sorted(source_rows)
    ]

    return CoverageReport(
        canonical_coverage=canonical_coverage,
        source_contribution=source_contribution,
        symbol_year_count=denominator,
        symbol_count=len(symbols),
        row_count=row_count,
    )


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_coverage_report(report: CoverageReport, output_dir: str | Path) -> list[Path]:
    root = Path(output_dir)
    coverage_path = root / "canonical_coverage.csv"
    source_path = root / "source_contribution.csv"
    _write_csv(coverage_path, report.canonical_coverage)
    _write_csv(source_path, report.source_contribution)
    return [coverage_path, source_path]


def main() -> None:
    parser = argparse.ArgumentParser(description="Report US SEC canonical mapping coverage.")
    parser.add_argument("--input-dir", default="data-lake/silver/sec/normalized")
    parser.add_argument("--out-dir", default="data-lake/silver/sec/coverage")
    parser.add_argument("--baseline-csv", default="")
    parser.add_argument("--focus-ids", default=",".join(DEFAULT_FOCUS_IDS))
    args = parser.parse_args()

    focus_ids = [value.strip() for value in args.focus_ids.split(",") if value.strip()]
    report = build_coverage_report(
        args.input_dir,
        focus_ids=focus_ids,
        baseline_csv=args.baseline_csv or None,
    )
    written = write_coverage_report(report, args.out_dir)
    print(
        "SEC mapping coverage "
        f"symbols={report.symbol_count}, symbol_years={report.symbol_year_count}, "
        f"rows={report.row_count}, written={','.join(str(path) for path in written)}"
    )


if __name__ == "__main__":
    main()
