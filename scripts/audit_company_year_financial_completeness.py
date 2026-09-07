from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pandas as pd

from engine.semantic import (
    CompanyYearCompletenessAuditor,
    FactorDependencyGraph,
    MissingFactCause,
    summarize_company_year_completeness,
    resolve_rule_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data-lake" / "silver" / "dart" / "normalized"
DEFAULT_CSV = ROOT / "deliverables" / "company_year_financial_completeness_v6.csv"
DEFAULT_JSON = ROOT / "deliverables" / "company_year_financial_completeness_v6.json"
FACTOR_SOURCE = ROOT / "scripts" / "calculate_factor_coverage.js"
RULE_ALIAS = ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_current.yaml"
_FILE_RE = re.compile(r"^kr_normalized_(\d{6})\.csv$")


def build_report(input_dir: Path) -> tuple[pd.DataFrame, dict[str, object]]:
    buckets: dict[tuple[str, int], dict[str, set[str]]] = defaultdict(
        lambda: {"concepts": set(), "statements": set()}
    )
    input_file_count = 0
    input_row_count = 0
    valid_annual_fact_count = 0
    for path in sorted(input_dir.glob("kr_normalized_*.csv")):
        match = _FILE_RE.match(path.name)
        if not match:
            continue
        input_file_count += 1
        security_id = f"SEC_KR_{match.group(1)}"
        for chunk in pd.read_csv(
            path,
            usecols=[
                "canonical_account_id",
                "statement_type",
                "fiscal_year",
                "fiscal_month",
                "normalized_amount",
            ],
            dtype={
                "canonical_account_id": str,
                "statement_type": str,
            },
            chunksize=100_000,
            low_memory=False,
        ):
            input_row_count += len(chunk)
            chunk["fiscal_year"] = pd.to_numeric(chunk["fiscal_year"], errors="coerce")
            chunk["fiscal_month"] = pd.to_numeric(chunk["fiscal_month"], errors="coerce")
            chunk["normalized_amount"] = pd.to_numeric(
                chunk["normalized_amount"], errors="coerce"
            )
            chunk = chunk.loc[
                chunk["fiscal_year"].notna()
                & chunk["fiscal_month"].eq(12)
                & chunk["normalized_amount"].notna()
            ]
            valid_annual_fact_count += len(chunk)
            for year, year_rows in chunk.groupby("fiscal_year", sort=False):
                bucket = buckets[(security_id, int(year))]
                bucket["concepts"].update(
                    value
                    for value in year_rows["canonical_account_id"].dropna().astype(str)
                    if value and value != "UNMAPPED"
                )
                bucket["statements"].update(
                    "IS" if value == "CIS" else value
                    for value in year_rows["statement_type"].dropna().astype(str).str.upper()
                    if value
                )

    graph = FactorDependencyGraph.from_javascript(FACTOR_SOURCE)
    auditor = CompanyYearCompletenessAuditor(graph)
    records = []
    missing_causes = Counter({cause.value: 0 for cause in MissingFactCause})
    for (security_id, fiscal_year), bucket in sorted(buckets.items()):
        fact_rows = [
            {"canonical_account_id": concept, "statement_type": ""}
            for concept in bucket["concepts"]
        ] + [
            {"canonical_account_id": None, "statement_type": statement}
            for statement in bucket["statements"]
        ]
        result = auditor.assess(security_id, fiscal_year, fact_rows)
        for missing in result["missing_core_facts"]:
            expected_statement = next(
                statement
                for statement, required in {
                    "BS": {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"},
                    "IS": {"REVENUE", "OPERATING_INCOME", "NET_INCOME"},
                    "CF": {"CFO", "CFI", "CFF"},
                }.items()
                if missing in required
            )
            cause = (
                MissingFactCause.CONCEPT_NOT_REPORTED
                if expected_statement in bucket["statements"]
                else MissingFactCause.STATEMENT_INCOMPLETE
            )
            missing_causes[cause.value] += 1
        records.append(result)

    flat_rows = []
    for row in records:
        flat_rows.append(
            {
                "security_id": row["company_id"],
                "fiscal_year": row["fiscal_year"],
                **{
                    f"{statement.lower()}_present": row["statement_presence"][statement]
                    for statement in ("BS", "IS", "CF")
                },
                **{
                    f"{statement.lower()}_core_complete": row[
                        "statement_core_complete"
                    ][statement]
                    for statement in ("BS", "IS", "CF")
                },
                "core_fact_present_count": row["core_fact_present_count"],
                "core_fact_expected_count": row["core_fact_expected_count"],
                "core_fact_coverage_pct": row["core_fact_coverage_pct"],
                "missing_core_facts": "|".join(row["missing_core_facts"]),
                "factor_ready_count": row["factor_ready_count"],
                "factor_count": row["factor_count"],
                "factor_ready_pct": row["factor_ready_pct"],
                "invariant_testable_count": row["invariant_testable_count"],
                "invariant_count": row["invariant_count"],
                "company_year_complete": row["company_year_complete"],
            }
        )
    frame = pd.DataFrame(flat_rows)
    summary = summarize_company_year_completeness(records)
    by_year = []
    if not frame.empty:
        for year, year_rows in frame.groupby("fiscal_year"):
            by_year.append(
                {
                    "fiscal_year": int(year),
                    "company_count": int(len(year_rows)),
                    "complete_company_count": int(year_rows["company_year_complete"].sum()),
                    "average_core_fact_coverage_pct": float(
                        year_rows["core_fact_coverage_pct"].mean()
                    ),
                    "average_factor_ready_pct": float(year_rows["factor_ready_pct"].mean()),
                    "invariant_testable_company_count": int(
                        year_rows["invariant_testable_count"].gt(0).sum()
                    ),
                }
            )
    resolved_rule_path = resolve_rule_bundle(RULE_ALIAS)
    version_match = re.fullmatch(r"semantic_kr_v(\d+)\.yaml", resolved_rule_path.name)
    semantic_engine_version = int(version_match.group(1)) if version_match else 4
    report = {
        "semantic_engine_version": semantic_engine_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "input_file_count": input_file_count,
        "input_row_count": input_row_count,
        "valid_annual_fact_count": valid_annual_fact_count,
        "summary": summary,
        "missing_core_fact_taxonomy": {
            "total_count": sum(missing_causes.values()),
            "cause_counts": dict(missing_causes),
        },
        "by_year": by_year,
        "field_contract": {
            "statement_presence": "at least one valid annual fact in BS/IS/CF",
            "statement_core_complete": "all three statement-specific anchor facts present",
            "factor_ready": "at least one executable alternative dependency path is present",
            "invariant_testable": "all facts for at least one conservative accounting equation are present",
        },
    }
    return frame, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit company-year statement, core, factor, and invariant completeness."
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args()
    frame, report = build_report(args.input_dir)
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.csv, index=False, encoding="utf-8")
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"csv": str(args.csv), "output": str(args.output), **report["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
