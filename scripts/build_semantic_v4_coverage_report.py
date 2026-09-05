from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.coverage import canonical_rule_coverage, migration_coverage, migration_integrity
from engine.semantic.integrity import static_sign_policy_audit
from engine.semantic.manifest import resolve_rule_bundle
from engine.semantic.quality import CoverageWaterfall
from engine.transformers._internal.dart_filings import normalize_account_name


RULE_ALIAS = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_current.yaml"
MANIFEST = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_rule_manifest.json"
CANONICAL = PROJECT_ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
OPERATIONAL = PROJECT_ROOT / "deliverables" / "semantic_rule_engine_coverage.json"
HISTORICAL = PROJECT_ROOT / "deliverables" / "historical_semantic_audit_2000_2012.json"
FACTOR_DRIFT = PROJECT_ROOT / "deliverables" / "capex_factor_drift_v4.json"
FACTOR_SUMMARY = PROJECT_ROOT / "data-lake" / "gold" / "factor_coverage" / "factor_coverage_summary.json"
OUTPUT = PROJECT_ROOT / "deliverables" / "semantic_rule_engine_v4_coverage.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _document_counts(historical: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
    sampled = parsed = failed = 0
    for section_name in ("financial_statements", "financial_notes", "business_content"):
        section = historical.get(section_name, {})
        years = section.get("years", {})
        for row in years.values():
            sampled += int(row.get("sample_file_count", 0))
            parsed += int(row.get("parsed_file_count", 0))
            failed += int(row.get("parse_failure_count", 0))
    return sampled, parsed, {"parse_failure": failed}


def build_report() -> dict[str, Any]:
    operational = _read(OPERATIONAL)
    observed = operational["observed_mapping_coverage"]
    historical = _read(HISTORICAL)
    factor_drift = _read(FACTOR_DRIFT)
    factor_actual = operational.get("factor_coverage") or _read(FACTOR_SUMMARY)
    dependency = historical["factor_dependencies"]
    migration = migration_coverage(RULE_ALIAS)
    canonical = canonical_rule_coverage(RULE_ALIAS, CANONICAL, text_normalizer=normalize_account_name)
    bundle = resolve_rule_bundle(RULE_ALIAS)
    manifest = _read(MANIFEST)

    sampled_documents, parsed_documents, document_losses = _document_counts(historical)
    waterfall = CoverageWaterfall()
    waterfall.add_stage(
        "document_ir",
        usable=parsed_documents,
        total=sampled_documents,
        unit="sampled_documents",
        loss_reasons=document_losses,
    )
    waterfall.add_stage(
        "reported_fact",
        usable=int(observed.get("valid_amount_row_count", 0)),
        total=int(observed.get("row_count", 0)),
        unit="operational_rows",
        loss_reasons={"invalid_amount": int(observed.get("excluded_invalid_amount_row_count", 0))},
    )
    mapped_count = int(observed.get("v4_mapped_row_count", observed.get("v3_mapped_row_count", 0)))
    waterfall.add_stage(
        "canonical_fact",
        usable=mapped_count,
        total=int(observed.get("row_count", 0)),
        unit="operational_rows",
        loss_reasons={"unmapped": int(observed.get("row_count", 0)) - mapped_count},
    )
    waterfall.add_stage(
        "harmonized_fact",
        usable=int(observed.get("harmonized_ready_row_count", 0)),
        total=mapped_count,
        unit="mapped_operational_rows",
        loss_reasons=observed.get("harmonization_blockers", {}),
    )
    waterfall.add_stage(
        "factor_input",
        usable=int(dependency.get("covered_factor_count", 0)),
        total=int(dependency.get("financial_dependency_factor_count", 0)),
        unit="financial_factors",
        loss_reasons={
            "missing_required_canonical_input": int(dependency.get("financial_dependency_factor_count", 0))
            - int(dependency.get("covered_factor_count", 0))
        },
    )
    waterfall.add_stage(
        "materialized_factor_cell",
        usable=int(factor_actual.get("covered_cells", 0)),
        total=int(factor_actual.get("total_cells", 0)),
        unit="stock_date_factor_cells",
        loss_reasons={"not_materialized_or_source_missing": int(factor_actual.get("total_cells", 0)) - int(factor_actual.get("covered_cells", 0))},
    )

    note_guard = historical.get("financial_notes", {}).get("ambiguity_clusters", {}).get("precision_guard", {})
    business_guard = historical.get("business_content", {}).get("ambiguity_clusters", {}).get("precision_guard", {})
    invariant = historical.get("financial_statements", {}).get("invariant_testability", {})
    return {
        "semantic_engine_version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_bundle": {
            "active_alias": str(RULE_ALIAS),
            "manifest": manifest,
            "resolved_bundle": str(bundle),
            "verified_sha256": sha256(bundle.read_bytes()).hexdigest(),
            "source_integrity": migration_integrity(RULE_ALIAS, project_root=PROJECT_ROOT),
        },
        "rule_migration": {
            "source_rule_count": migration.total_source_rules,
            "migrated_source_rule_count": migration.migrated_source_rules,
            "coverage_pct": migration.coverage_pct,
        },
        "canonical_rule_coverage": canonical,
        "coverage_waterfall": waterfall.report(),
        "coverage_stratification": {
            "operational": observed.get("coverage_stratification", {}),
            "historical_2000_2012": historical.get("financial_statements", {}).get("coverage_stratification", {}),
        },
        "accounting_identity_validation": invariant,
        "precision_guards": {
            "sign_policy": static_sign_policy_audit(RULE_ALIAS),
            "narrative_false_semantic_emit_count": int(note_guard.get("false_semantic_emit_count", 0)) + int(business_guard.get("false_semantic_emit_count", 0)),
            "narrative_automatic_promotion_count": int(note_guard.get("automatic_promotion_count", 0)) + int(business_guard.get("automatic_promotion_count", 0)),
            "promotion_policy": "Only reviewed deterministic rules with independent golden tests may be promoted.",
        },
        "capex_factor_drift": factor_drift,
        "factor_coverage": {
            "dependency": dependency,
            "actual_materialized_cells": factor_actual,
        },
        "limitations": [
            "Coverage percentages retain their own units and are never multiplied across unlike denominators.",
            "Invariant REVIEW is candidate evidence, not an automatic remap.",
            "Precision/recall cannot be statistically estimated until a human-labelled gold corpus is supplied.",
            "Operational debug rows created before v4 usually lack scope/regime metadata, so strict harmonization readiness intentionally fails closed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the semantic engine v4 quality and coverage report.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "migration_coverage_pct": report["rule_migration"]["coverage_pct"],
        "canonical_rule_coverage_pct": report["canonical_rule_coverage"]["coverage_pct"],
        "factor_dependency_coverage_pct": report["factor_coverage"]["dependency"]["factor_input_coverage_pct"],
        "factor_cell_coverage_pct": report["factor_coverage"]["actual_materialized_cells"]["coverage_pct"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
