from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.coverage import (
    build_coverage_report,
    load_factor_coverage,
    observed_mapping_coverage,
    write_coverage_report,
)
from engine.transformers._internal.dart_filings import (
    RuleEngine,
    amount_to_int,
    extract_rows_from_dart_html,
    normalize_account_name,
)


RULES = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_v2.yaml"
CANONICAL = PROJECT_ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
SIGN_POLICY = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "sign_policy_common.yaml"
LEGACY_RULES = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "kr_mapping.yaml"
NORMALIZED = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
FACTOR_SUMMARY = PROJECT_ROOT / "data-lake" / "gold" / "factor_coverage" / "factor_coverage_summary.json"
FACTOR_DETAIL = PROJECT_ROOT / "data-lake" / "gold" / "factor_coverage" / "kr_factor_coverage_all_stocks.csv"
OUTPUT = PROJECT_ROOT / "deliverables" / "semantic_rule_engine_coverage.json"
HISTORICAL_SAMPLE = (
    PROJECT_ROOT
    / "data-lake"
    / "bronze"
    / "dart"
    / "finance-statement"
    / "017670"
    / "finance_statement_(2008.12).html"
)


def historical_k_gaap_validation(
    path: Path,
    engine: RuleEngine,
    legacy_engine: RuleEngine,
) -> dict[str, object]:
    if not path.exists():
        return {"available": False, "source_file": str(path)}
    rows = extract_rows_from_dart_html(path, "SK텔레콤", "2008.12")
    v2_results = [engine.map_row(row) for row in rows]
    legacy_results = [legacy_engine.map_row(row) for row in rows]
    total_amount = sum(
        (Decimal(abs(amount_to_int(row.get("raw_amount")))) for row in rows),
        Decimal(0),
    )
    v2_amount = sum(
        (
            Decimal(abs(amount_to_int(row.get("raw_amount"))))
            for row, result in zip(rows, v2_results)
            if result.canonical_account_id != "UNMAPPED"
        ),
        Decimal(0),
    )
    legacy_amount = sum(
        (
            Decimal(abs(amount_to_int(row.get("raw_amount"))))
            for row, result in zip(rows, legacy_results)
            if result.canonical_account_id != "UNMAPPED"
        ),
        Decimal(0),
    )
    v2_mapped = sum(
        result.canonical_account_id != "UNMAPPED" for result in v2_results
    )
    legacy_mapped = sum(
        result.canonical_account_id != "UNMAPPED" for result in legacy_results
    )
    row_count = len(rows)
    return {
        "available": True,
        "source_file": str(path.resolve()),
        "company_code": "017670",
        "period": "2008.12",
        "row_count": row_count,
        "statement_type_row_counts": dict(
            sorted(Counter(row.get("statement_type", "UNKNOWN") for row in rows).items())
        ),
        "accounting_regime_row_counts": dict(
            sorted(Counter(row.get("accounting_regime", "UNKNOWN") for row in rows).items())
        ),
        "document_dialect_row_counts": dict(
            sorted(Counter(row.get("document_dialect", "UNKNOWN") for row in rows).items())
        ),
        "accounting_regime_confidence": (
            rows[0].get("accounting_regime_confidence", 0.0) if rows else 0.0
        ),
        "accounting_regime_evidence": (
            rows[0].get("accounting_regime_evidence", "[]") if rows else "[]"
        ),
        "legacy_mapped_row_count": legacy_mapped,
        "legacy_mapped_row_pct": 100.0 * legacy_mapped / row_count if row_count else 0.0,
        "v2_mapped_row_count": v2_mapped,
        "v2_mapped_row_pct": 100.0 * v2_mapped / row_count if row_count else 0.0,
        "mapping_pct_point_delta": (
            100.0 * (v2_mapped - legacy_mapped) / row_count if row_count else 0.0
        ),
        "legacy_mapped_absolute_amount_pct": (
            float(Decimal(100) * legacy_amount / total_amount) if total_amount else 0.0
        ),
        "v2_mapped_absolute_amount_pct": (
            float(Decimal(100) * v2_amount / total_amount) if total_amount else 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute Arcana semantic rule engine coverage")
    parser.add_argument("--rules", type=Path, default=RULES)
    parser.add_argument("--canonical", type=Path, default=CANONICAL)
    parser.add_argument("--normalized-dir", type=Path, default=NORMALIZED)
    parser.add_argument("--factor-summary", type=Path, default=FACTOR_SUMMARY)
    parser.add_argument("--factor-detail", type=Path, default=FACTOR_DETAIL)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--historical-sample", type=Path, default=HISTORICAL_SAMPLE)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--skip-observed", action="store_true")
    args = parser.parse_args()

    engine = RuleEngine.from_files(
        canonical_csv_path=args.canonical,
        rule_paths=[args.rules],
        sign_policy_path=SIGN_POLICY,
    )
    legacy_engine = RuleEngine.from_files(
        canonical_csv_path=args.canonical,
        rule_paths=[LEGACY_RULES],
        sign_policy_path=SIGN_POLICY,
    )
    observed = {}
    if not args.skip_observed:
        observed = observed_mapping_coverage(
            args.normalized_dir,
            engine,
            legacy_mapping_engine=legacy_engine,
            max_files=args.max_files,
        )
    factor = load_factor_coverage(args.factor_summary, args.factor_detail)
    report = build_coverage_report(
        bundle_path=args.rules,
        canonical_path=args.canonical,
        project_root=PROJECT_ROOT,
        text_normalizer=normalize_account_name,
        observed=observed,
        factor=factor,
    )
    report["historical_k_gaap_validation"] = historical_k_gaap_validation(
        args.historical_sample,
        engine,
        legacy_engine,
    )
    output = write_coverage_report(report, args.output)
    canonical = report["canonical_rule_coverage"]
    migration = report["migration"]
    factor_report = report.get("factor_coverage", {})
    print(
        json.dumps(
            {
                "output": str(output),
                "migration_coverage_pct": migration["coverage_pct"],
                "canonical_rule_coverage_pct": canonical["coverage_pct"],
                "observed_v2_mapping_pct": observed.get("v2_mapped_row_pct"),
                "factor_coverage_pct": factor_report.get("coverage_pct"),
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
