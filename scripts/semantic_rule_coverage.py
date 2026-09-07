from __future__ import annotations

import argparse
from collections import Counter
import csv
from decimal import Decimal
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.coverage import (
    MAX_COVERAGE_AMOUNT_MAGNITUDE,
    build_coverage_report,
    load_factor_coverage,
    observed_mapping_coverage,
    write_coverage_report,
)
from engine.semantic.manifest import resolve_rule_bundle
from engine.transformers._internal.dart_filings import (
    RuleEngine,
    amount_to_int,
    extract_rows_from_dart_html,
    normalize_account_name,
)


RULES = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_current.yaml"
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


def _finite_absolute_amount(value: object) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        amount = Decimal(text)
    except Exception:
        return None
    magnitude = abs(amount)
    if not amount.is_finite() or magnitude > MAX_COVERAGE_AMOUNT_MAGNITUDE:
        return None
    return magnitude


def materialized_mapping_coverage(
    input_dir: str | Path,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    max_files: int | None = None,
) -> dict[str, object]:
    """Measure current canonical outputs without replaying potentially stale debug rows."""
    paths = sorted(
        path
        for path in Path(input_dir).glob("kr_normalized_*.csv")
        if not path.name.endswith(".debug.csv")
    )
    if max_files is not None:
        paths = paths[:max_files]
    row_count = 0
    valid_amount_row_count = 0
    excluded_invalid_amount_row_count = 0
    mapped_row_count = 0
    total_amount = Decimal(0)
    mapped_amount = Decimal(0)
    statement_type_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                period = str(row.get("period", "") or "")
                match = re.match(r"^(\d{4})", period)
                year = int(match.group(1)) if match else None
                if start_year is not None and (year is None or year < start_year):
                    continue
                if end_year is not None and (year is None or year > end_year):
                    continue
                row_count += 1
                year_counts[str(year) if year is not None else "UNKNOWN"] += 1
                statement_type_counts[str(row.get("statement_type", "UNKNOWN") or "UNKNOWN")] += 1
                mapped = str(row.get("canonical_account_id", "") or "") not in {
                    "",
                    "UNMAPPED",
                }
                if mapped:
                    mapped_row_count += 1
                amount = _finite_absolute_amount(
                    row.get("raw_amount", row.get("amount", ""))
                )
                if amount is None:
                    excluded_invalid_amount_row_count += 1
                    continue
                valid_amount_row_count += 1
                total_amount += amount
                if mapped:
                    mapped_amount += amount
    row_pct = 100.0 * mapped_row_count / row_count if row_count else 0.0
    amount_pct = (
        float(Decimal(100) * mapped_amount / total_amount) if total_amount else 0.0
    )
    return {
        "coverage_input_kind": "materialized_canonical",
        "input_dir": str(Path(input_dir).resolve()),
        "start_year": start_year,
        "end_year": end_year,
        "file_count": len(paths),
        "row_count": row_count,
        "valid_amount_row_count": valid_amount_row_count,
        "excluded_invalid_amount_row_count": excluded_invalid_amount_row_count,
        "baseline_mapped_row_count": mapped_row_count,
        "v2_mapped_row_count": mapped_row_count,
        "baseline_mapped_row_pct": row_pct,
        "v2_mapped_row_pct": row_pct,
        "total_absolute_amount": str(total_amount),
        "baseline_mapped_absolute_amount_pct": amount_pct,
        "v2_mapped_absolute_amount_pct": amount_pct,
        "statement_type_row_counts": dict(sorted(statement_type_counts.items())),
        "year_row_counts": dict(sorted(year_counts.items())),
        "legacy_replay_mapped_row_count": None,
        "legacy_replay_mapped_row_pct": None,
        "legacy_replay_mapped_absolute_amount_pct": None,
        "changed_mapping_count": None,
        "newly_mapped_row_count": None,
        "provenance_note": (
            "Counts materialized canonical outputs for the requested period; "
            "legacy replay requires fresh debug rows and is intentionally omitted."
        ),
    }


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
    parser.add_argument(
        "--materialized-normalized",
        action="store_true",
        help="Measure canonical CSV outputs and ignore potentially stale debug files.",
    )
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
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
    if (
        args.start_year is not None
        and args.end_year is not None
        and args.start_year > args.end_year
    ):
        raise ValueError("start-year must not exceed end-year")
    observed = {}
    if not args.skip_observed:
        if args.materialized_normalized:
            observed = materialized_mapping_coverage(
                args.normalized_dir,
                start_year=args.start_year,
                end_year=args.end_year,
                max_files=args.max_files,
            )
        else:
            observed = observed_mapping_coverage(
                args.normalized_dir,
                engine,
                legacy_mapping_engine=legacy_engine,
                max_files=args.max_files,
                progress=True,
            )
    resolved_rules = resolve_rule_bundle(args.rules)
    version_match = re.fullmatch(r"semantic_kr_v(\d+)\.yaml", resolved_rules.name)
    semantic_engine_version = int(version_match.group(1)) if version_match else 4
    version_prefix = f"v{semantic_engine_version}"
    if semantic_engine_version >= 5 and observed:
        observed["semantic_engine_version"] = semantic_engine_version
        observed[f"{version_prefix}_mapped_row_count"] = observed["v2_mapped_row_count"]
        observed[f"{version_prefix}_mapped_row_pct"] = observed["v2_mapped_row_pct"]
        observed[f"{version_prefix}_mapped_absolute_amount_pct"] = observed[
            "v2_mapped_absolute_amount_pct"
        ]
    factor = load_factor_coverage(args.factor_summary, args.factor_detail)
    report = build_coverage_report(
        bundle_path=args.rules,
        canonical_path=args.canonical,
        project_root=PROJECT_ROOT,
        text_normalizer=normalize_account_name,
        observed=observed,
        factor=factor,
    )
    report["semantic_engine_version"] = semantic_engine_version
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
                "observed_mapping_pct": observed.get(
                    f"{version_prefix}_mapped_row_pct",
                    observed.get(
                        "v5_mapped_row_pct",
                        observed.get(
                            "v4_mapped_row_pct",
                            observed.get(
                                "v3_mapped_row_pct",
                                observed.get("v2_mapped_row_pct"),
                            ),
                        ),
                    ),
                ),
                "factor_coverage_pct": factor_report.get("coverage_pct"),
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
