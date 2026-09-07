from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_factors import TECHNICAL_FACTORS
from engine.transformers._internal.factor_metrics import (
    CONSENSUS_FACTOR_COLUMNS,
    DIVIDEND_FACTOR_COLUMNS,
    KR_EXPLICITLY_INAPPLICABLE_FACTOR_COLUMNS,
    KR_PRICE_TO_TARGET_PRICE_FACTOR,
    REAL_CONSENSUS_FACTOR_COLUMNS,
    market_applicable_factor_columns,
    preferred_factor_columns,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOAD_REPORT = (
    ROOT / "deliverables" / "kr_historical_2002_2012_factor_load_report.json"
)
DEFAULT_OUTPUT_JSON = (
    ROOT / "deliverables" / "kr_historical_2002_2012_factor_gap_analysis.json"
)
DEFAULT_OUTPUT_CSV = (
    ROOT / "deliverables" / "kr_historical_2002_2012_factor_gap_analysis.csv"
)
DEFAULT_TARGETS = DATA_LAKE.meta("kr_historical_2002_2012_factor_targets.csv")
DEFAULT_METADATA = DATA_LAKE.silver("dart", "kr_report_metadata.csv")
RAW_STATEMENT_ROOT = DATA_LAKE.bronze("dart", "finance-statement")

# These factor IDs were finite in the baseline solely because missing inputs
# were replaced by zero in annual, valuation, or WACC calculations.  The
# runtime calculation has been fixed; this set lets a pre-fix persisted load
# be audited without crediting unsupported finite values as coverage.
BASELINE_ZERO_IMPUTATION_FALSE_POSITIVES = frozenset(
    {
        "bps",
        "sps",
        "cps",
        "fcff",
        "fcfe",
        "bpr",
        "spr",
        "cpr",
        "fcfpr",
        "sharehold_net_buyback_yield",
        "sharehold_return",
        "shareholder_yield",
        "enterprise_value",
        "wacc_equity_weight",
        "wacc_debt_weight",
        "wacc",
    }
)


def classify_factor_primary_cause(
    factor_id: str,
    *,
    materialized_ids: set[str],
    false_positive_ids: set[str],
    market_inapplicable_ids: set[str],
    source_outside_period_ids: set[str],
    source_sparse_ids: set[str],
    financial_dependency_ids: set[str],
) -> str:
    factor_id = str(factor_id)
    if factor_id in market_inapplicable_ids:
        return "NOT_APPLICABLE_MARKET"
    if factor_id in materialized_ids and factor_id in false_positive_ids:
        return "MATERIALIZED_WITH_ZERO_IMPUTATION_RISK"
    if factor_id in materialized_ids:
        return "MATERIALIZED_WITH_SOURCE_EVIDENCE"
    if factor_id in source_outside_period_ids:
        return "SOURCE_OUTSIDE_TARGET_PERIOD"
    if factor_id in source_sparse_ids:
        return "UPSTREAM_SOURCE_SPARSE"
    if factor_id in financial_dependency_ids:
        return "FILING_PROVENANCE_OR_CANONICAL_INPUT_MISSING"
    return "FORMULA_OR_IMPLEMENTATION_GAP"


def _materialized_ids(load_report: dict[str, Any]) -> set[str]:
    return {
        str(factor_id)
        for row in load_report.get("source", [])
        for factor_id in row.get("factor_ids", [])
    }


def _raw_statement_inventory(target_symbols: set[str]) -> dict[str, Any]:
    pattern = re.compile(r"\((\d{4})\.(\d{1,2})\)")
    by_year: Counter[int] = Counter()
    by_month: Counter[int] = Counter()
    securities: set[str] = set()
    file_count = 0
    for symbol in sorted(target_symbols):
        directory = RAW_STATEMENT_ROOT / symbol
        if not directory.exists():
            continue
        found = False
        for path in directory.glob("finance_statement_*.html"):
            match = pattern.search(path.name)
            if not match:
                continue
            year, month = int(match.group(1)), int(match.group(2))
            if not 2001 <= year <= 2012 or month not in {3, 6, 9, 12}:
                continue
            file_count += 1
            by_year[year] += 1
            by_month[month] += 1
            found = True
        if found:
            securities.add(symbol)
    return {
        "security_count": len(securities),
        "file_count": file_count,
        "by_year": {str(key): value for key, value in sorted(by_year.items())},
        "by_month": {str(key): value for key, value in sorted(by_month.items())},
    }


def _metadata_inventory(path: Path, target_symbols: set[str]) -> dict[str, Any]:
    if not path.exists():
        return {"row_count": 0, "historical_row_count": 0, "security_count": 0}
    frame = pd.read_csv(path, dtype={"stock_code": str})
    frame["stock_code"] = frame.get("stock_code", "").astype(str).str.zfill(6)
    frame["report_date"] = pd.to_datetime(frame.get("report_date"), errors="coerce")
    historical = frame.loc[
        frame["stock_code"].isin(target_symbols)
        & frame["report_date"].between("2001-01-01", "2012-12-31")
    ]
    return {
        "row_count": len(frame),
        "historical_row_count": len(historical),
        "security_count": int(historical["stock_code"].nunique()),
        "min_report_date": str(historical["report_date"].min().date())
        if not historical.empty
        else None,
        "max_report_date": str(historical["report_date"].max().date())
        if not historical.empty
        else None,
    }


def _baseline_evidence_table(load_report: dict[str, Any]) -> str:
    table = str(load_report.get("backup_table", "")).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(
            "baseline zero-imputation audit requires a safe backup table "
            "identifier from the load report"
        )
    return table


def _false_positive_cell_counts(
    factor_ids: Iterable[str],
    *,
    table: str,
) -> dict[str, int]:
    ids = sorted(set(factor_ids))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("unsafe baseline backup table identifier")
    client = get_clickhouse_client()
    result = client.query(
        f"""
SELECT baseline.factor_id, count()
FROM
(
    SELECT security_id, trade_date, factor_id, financial_basis
    FROM {table} FINAL
    WHERE startsWith(security_id, 'SEC_KR_')
      AND financial_basis = 'annual'
      AND trade_date >= toDate('2002-01-01')
      AND trade_date <= toDate('2012-12-31')
      AND factor_id IN {{factor_ids:Array(String)}}
      AND isFinite(factor_value)
) AS baseline
LEFT ANTI JOIN
(
    SELECT security_id, trade_date, factor_id, financial_basis
    FROM fact_daily_factors FINAL
    WHERE startsWith(security_id, 'SEC_KR_')
      AND financial_basis = 'annual'
      AND trade_date >= toDate('2002-01-01')
      AND trade_date <= toDate('2012-12-31')
      AND factor_id IN {{factor_ids:Array(String)}}
      AND isFinite(factor_value)
) AS current
USING (security_id, trade_date, factor_id, financial_basis)
GROUP BY baseline.factor_id
ORDER BY baseline.factor_id
""".strip(),
        parameters={"factor_ids": ids},
    )
    return {str(factor_id): int(count) for factor_id, count in result.result_rows}


def build_gap_analysis(
    load_report: dict[str, Any],
    *,
    target_symbols: set[str],
    metadata_path: Path,
    with_clickhouse: bool,
) -> tuple[dict[str, Any], pd.DataFrame]:
    global_contract = preferred_factor_columns()
    kr_contract = market_applicable_factor_columns("kr")
    materialized = _materialized_ids(load_report)
    false_positive = materialized & set(BASELINE_ZERO_IMPUTATION_FALSE_POSITIVES)
    market_inapplicable = set(KR_EXPLICITLY_INAPPLICABLE_FACTOR_COLUMNS)
    source_outside = set(DIVIDEND_FACTOR_COLUMNS) | set(CONSENSUS_FACTOR_COLUMNS)
    source_sparse = set(REAL_CONSENSUS_FACTOR_COLUMNS) | {
        KR_PRICE_TO_TARGET_PRICE_FACTOR
    }
    financial_dependency = (
        set(global_contract)
        - market_inapplicable
        - source_outside
        - source_sparse
        - set(TECHNICAL_FACTORS)
    )

    factor_rows: list[dict[str, Any]] = []
    for factor_id in global_contract:
        cause = classify_factor_primary_cause(
            factor_id,
            materialized_ids=materialized,
            false_positive_ids=false_positive,
            market_inapplicable_ids=market_inapplicable,
            source_outside_period_ids=source_outside,
            source_sparse_ids=source_sparse,
            financial_dependency_ids=financial_dependency,
        )
        factor_rows.append(
            {
                "factor_id": factor_id,
                "kr_applicable": factor_id in set(kr_contract),
                "materialized_in_baseline": factor_id in materialized,
                "primary_cause": cause,
            }
        )
    factor_frame = pd.DataFrame(factor_rows)
    cause_counts = factor_frame["primary_cause"].value_counts().sort_index().to_dict()

    annual_summary = next(
        row
        for row in load_report["source_coverage_summary"]
        if row["financial_basis"] == "annual"
    )
    price_rows = int(annual_summary["possible_factor_cell_count"]) // len(global_contract)
    baseline_finite_cells = int(annual_summary["finite_factor_cell_count"])
    baseline_evidence_table = (
        _baseline_evidence_table(load_report)
        if with_clickhouse and false_positive
        else None
    )
    false_cell_counts = (
        _false_positive_cell_counts(
            false_positive,
            table=baseline_evidence_table,
        )
        if baseline_evidence_table is not None
        else {}
    )
    false_cell_count = sum(false_cell_counts.values())
    honest_finite_cells_upper_bound = baseline_finite_cells - false_cell_count
    kr_possible_cells = price_rows * len(kr_contract)

    analysis = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"start_date": "2002-01-01", "end_date": "2012-12-31"},
        "contracts": {
            "global_factor_count": len(global_contract),
            "kr_applicable_factor_count": len(kr_contract),
            "kr_inapplicable_factor_count": len(market_inapplicable),
            "kr_inapplicable_factor_ids": sorted(market_inapplicable),
        },
        "baseline": {
            "price_row_count": price_rows,
            "materialized_factor_count_reported": len(materialized),
            "global_factor_id_coverage_pct_reported": round(
                100 * len(materialized) / len(global_contract), 6
            ),
            "kr_factor_id_coverage_pct_reported": round(
                100 * len(materialized - market_inapplicable) / len(kr_contract), 6
            ),
            "finite_factor_cell_count_reported": baseline_finite_cells,
            "global_possible_factor_cell_count": price_rows * len(global_contract),
            "global_factor_cell_coverage_pct_reported": round(
                100 * baseline_finite_cells / (price_rows * len(global_contract)), 6
            ),
            "kr_possible_factor_cell_count": kr_possible_cells,
            "kr_factor_cell_coverage_pct_reported": round(
                100 * baseline_finite_cells / kr_possible_cells, 6
            ),
            "zero_imputation_risk_factor_ids": sorted(false_positive),
            "zero_imputation_evidence_table": (
                baseline_evidence_table
            ),
            "confirmed_removed_candidate_cell_counts": false_cell_counts,
            "confirmed_removed_candidate_cell_count": false_cell_count,
            "baseline_honest_finite_cell_count_upper_bound": (
                honest_finite_cells_upper_bound
            ),
            "baseline_honest_kr_factor_cell_coverage_pct_upper_bound": round(
                100 * honest_finite_cells_upper_bound / kr_possible_cells, 6
            ),
            "evidence_note": (
                "Counts are a conservative lower bound: finite baseline cells "
                "in zero-imputation-risk factors that have no finite key in the "
                "PIT-safe rebuilt annual table. The remaining baseline cells "
                "are an upper bound on honest coverage, not an accuracy claim."
            ),
        },
        "primary_factor_id_cause_counts": {
            str(key): int(value) for key, value in cause_counts.items()
        },
        "source_inventory": {
            "report_metadata": _metadata_inventory(metadata_path, target_symbols),
            "raw_statements": _raw_statement_inventory(target_symbols),
            "historical_shares_recovery": {
                "source": "marcap Stocks and Marcap columns",
                "path": str(
                    DATA_LAKE.silver(
                        "krx", "shares", "kr_historical_2002_2012_shares.csv"
                    )
                ),
            },
        },
        "confirmed_defects": [
            "long-range DART metadata searches were not windowed",
            "DART 3.x node/node1 financial sections were ignored",
            "CP949 console diagnostics could abort parseable filings",
            "missing financial/shareholder inputs were converted to finite zero factors",
            "missing debt/cash inputs produced market-cap-only enterprise value and all-equity WACC",
            "future-dated or implausible country ERP values could leak into historical cost of equity",
            "historical marcap Stocks/Marcap fields were not normalized into factor inputs",
        ],
    }
    return analysis, factor_frame


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit KR historical factor gaps.")
    parser.add_argument("--load-report", type=Path, default=DEFAULT_LOAD_REPORT)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--with-clickhouse", action="store_true")
    args = parser.parse_args()

    load_report = json.loads(args.load_report.read_text(encoding="utf-8"))
    targets = pd.read_csv(args.targets, dtype=str)
    target_symbols = set(targets["symbol"].astype(str).str.zfill(6))
    analysis, factor_frame = build_gap_analysis(
        load_report,
        target_symbols=target_symbols,
        metadata_path=args.metadata,
        with_clickhouse=args.with_clickhouse,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    factor_frame.to_csv(args.output_csv, index=False, encoding="utf-8-sig")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
