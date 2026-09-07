from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from engine.semantic.coverage import (
    canonical_rule_coverage,
    migration_coverage,
    migration_integrity,
)
from engine.semantic.manifest import resolve_rule_bundle, validate_rule_manifest
from engine.transformers._internal.dart_filings import normalize_account_name


ROOT = Path(__file__).resolve().parents[1]
RULE_ALIAS = ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_current.yaml"
MANIFEST = ROOT / "data-lake" / "meta" / "rules" / "semantic_rule_manifest.json"
CANONICAL = ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
GOLDEN_CORPUS = ROOT / "data-lake" / "meta" / "rules" / "semantic_golden_contract_v6.jsonl"
GOLDEN_EVAL = ROOT / "deliverables" / "semantic_golden_contract_v6_evaluation.json"
OPERATIONAL = ROOT / "deliverables" / "semantic_rule_engine_v6_operational_coverage.json"
HISTORICAL = ROOT / "deliverables" / "historical_semantic_audit_2000_2012_v6.json"
COMPANY_YEAR = ROOT / "deliverables" / "company_year_financial_completeness_v6.json"
FACTOR_BASELINE = ROOT / "deliverables" / "factor_coverage_v4_baseline.json"
FACTOR_CURRENT = ROOT / "data-lake" / "gold" / "factor_coverage" / "factor_coverage_summary.json"
CAPEX_ANNUAL = ROOT / "deliverables" / "capex_factor_drift_v5.json"
CAPEX_DAILY = ROOT / "deliverables" / "capex_daily_portfolio_drift_v5.json"
HISTORICAL_FACTOR_REPORT = (
    ROOT / "deliverables" / "kr_historical_2002_2012_factor_load_report_v2.json"
)
HISTORICAL_FACTOR_GAP = (
    ROOT / "deliverables" / "kr_historical_2002_2012_factor_gap_analysis.json"
)
DIVIDEND_PIT = ROOT / "data-lake" / "meta" / "kr_dividend_pit_events_audit.json"
FINANCIAL_PIT = ROOT / "data-lake" / "meta" / "kr_annual_financial_availability_audit.json"
OUTPUT = ROOT / "deliverables" / "semantic_rule_engine_v6_coverage.json"


def _read(path: Path, *, optional: bool = False) -> dict[str, Any]:
    if optional and not path.exists():
        return {"status": "NOT_AVAILABLE", "path": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_test_suite() -> str:
    digest = sha256()
    for path in sorted((ROOT / "tests").glob("test_semantic_rule_engine_v*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_report() -> dict[str, Any]:
    manifest = _read(MANIFEST)
    validate_rule_manifest(manifest, path=MANIFEST)
    bundle = resolve_rule_bundle(MANIFEST)
    bundle_hash = sha256(bundle.read_bytes()).hexdigest()
    golden_hash = sha256(GOLDEN_CORPUS.read_bytes()).hexdigest()
    test_suite_hash = _hash_test_suite()
    manifest_checks = {
        "bundle_hash_matches": manifest["hash"] == f"sha256:{bundle_hash}",
        "golden_corpus_hash_matches": manifest["golden_corpus_hash"]
        == f"sha256:{golden_hash}",
        "test_suite_hash_matches": manifest["test_suite_hash"]
        == f"sha256:{test_suite_hash}",
    }

    migration = migration_coverage(RULE_ALIAS)
    source_rule_total = (
        migration.mapping_source_rules
        + migration.context_source_rules
        + migration.comment_source_rules
        + migration.sign_policy_source_entries
    )
    migrated_rule_total = (
        migration.mapping_migrated_rules
        + migration.context_migrated_rules
        + migration.comment_migrated_rules
        + migration.sign_policy_migrated_entries
    )
    historical = _read(HISTORICAL)
    operational = _read(OPERATIONAL)
    dependency = historical["factor_dependencies"]
    baseline = _read(FACTOR_BASELINE)
    current = _read(FACTOR_CURRENT)
    historical_factor = _read(HISTORICAL_FACTOR_REPORT)
    historical_factor_gap = _read(HISTORICAL_FACTOR_GAP)
    cell_delta = int(current["covered_cells"]) - int(baseline["covered_cells"])
    report = {
        "semantic_engine_version": 6,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_bundle": {
            "manifest": manifest,
            "resolved_bundle": str(bundle),
            "bundle_sha256": bundle_hash,
            "golden_corpus_sha256": golden_hash,
            "test_suite_sha256": test_suite_hash,
            "manifest_checks": manifest_checks,
        },
        "rule_migration": {
            "source_rule_count": source_rule_total,
            "migrated_source_rule_count": migrated_rule_total,
            "coverage_pct": 100.0 * migrated_rule_total / source_rule_total,
            "source_integrity": migration_integrity(
                RULE_ALIAS, project_root=ROOT
            ),
        },
        "canonical_rule_coverage": canonical_rule_coverage(
            RULE_ALIAS, CANONICAL, text_normalizer=normalize_account_name
        ),
        "operational_account_mapping_coverage": operational[
            "observed_mapping_coverage"
        ],
        "factor_dependency_coverage": dependency,
        "factor_cell_coverage": {
            "v4_reported_baseline": baseline,
            "v6_pit_safe": current,
            "covered_cell_delta": cell_delta,
            "coverage_pct_delta": float(current["coverage_pct"])
            - float(baseline["coverage_pct"]),
            "comparison_note": "The v6 primary result uses disclosure dates and no financial period-end fallback; it is stricter than the v4 baseline. A decrease is an accuracy correction, not a regression.",
        },
        "historical_2002_2012_factor_coverage": historical_factor,
        "historical_factor_gap_audit": historical_factor_gap,
        "point_in_time_inputs": {
            "financial": _read(FINANCIAL_PIT),
            "dividend": _read(DIVIDEND_PIT),
        },
        "company_year_completeness": _read(COMPANY_YEAR),
        "historical_2000_2012": historical,
        "golden_corpus": _read(GOLDEN_EVAL),
        "capex_drift": {
            "annual_counterfactual": _read(CAPEX_ANNUAL),
            "daily_materialized_portfolio": _read(CAPEX_DAILY, optional=True),
            "evidence_version": 5,
            "inheritance_note": "v6 does not change v5 CAPEX direction semantics; the hash-scoped v5 evidence is inherited unchanged.",
        },
        "hard_gates": {
            "all_manifest_hashes_match": all(manifest_checks.values()),
            "all_legacy_rules_migrated": source_rule_total == migrated_rule_total,
            "canonical_catalog_fully_covered": canonical_rule_coverage(
                RULE_ALIAS, CANONICAL, text_normalizer=normalize_account_name
            )["coverage_pct"]
            == 100.0,
            "executable_factor_dependencies_fully_covered": dependency[
                "factor_input_coverage_pct"
            ]
            == 100.0,
            "historical_factor_targets_fully_processed": historical_factor[
                "all_targets_processed"
            ],
            "historical_factor_snapshots_equal_source": historical_factor[
                "all_year_basis_snapshots_equal"
            ],
            "narrative_automatic_promotions": 0,
            "independent_accuracy_claim": False,
        },
        "limitations": [
            "The 961-case golden corpus is an authoritative rule-contract regression set, not independently relabelled out-of-sample data.",
            "2000-2012 results cover locally present filings only; years with no local filing remain source_not_ingested rather than inferred.",
            "Invariant REVIEW remains candidate evidence and never auto-remaps an account.",
            "Coverage percentages retain their own denominators and are never multiplied across stages.",
        ],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build semantic rule engine v6 evidence report.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "migration_coverage_pct": report["rule_migration"]["coverage_pct"],
                "canonical_rule_coverage_pct": report["canonical_rule_coverage"]["coverage_pct"],
                "factor_dependency_coverage_pct": report["factor_dependency_coverage"]["factor_input_coverage_pct"],
                "factor_cell_coverage_pct": report["factor_cell_coverage"]["v6_pit_safe"]["coverage_pct"],
                "hard_gates": report["hard_gates"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
