from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.coverage import (
    canonical_rule_coverage,
    migration_coverage,
    migration_integrity,
)
from engine.semantic.integrity import static_sign_policy_audit
from engine.transformers._internal.dart_filings import normalize_account_name


RULES = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_v2.yaml"
CANONICAL = PROJECT_ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
OPERATIONAL = PROJECT_ROOT / "deliverables" / "semantic_rule_engine_coverage.json"
HISTORICAL = PROJECT_ROOT / "deliverables" / "historical_semantic_audit_2000_2012.json"
CORRECTION_SUMMARY = PROJECT_ROOT / "deliverables" / "semantic_correction_ledger_v3_summary.json"
CORRECTION_LEDGER = PROJECT_ROOT / "deliverables" / "semantic_correction_ledger_v3.csv"
VALUE_INTEGRITY = PROJECT_ROOT / "deliverables" / "semantic_value_integrity_audit.json"
OUTPUT = PROJECT_ROOT / "deliverables" / "semantic_rule_engine_v3_coverage.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_ledger(path: Path) -> dict[str, Any]:
    required = {
        "old_fact_id",
        "old_semantics",
        "new_fact_id",
        "new_semantics",
        "reason",
        "source_hash",
        "migration_version",
    }
    old_ids: set[str] = set()
    new_ids: set[str] = set()
    duplicate_old = 0
    duplicate_new = 0
    missing_required = 0
    row_count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing_columns = sorted(required - set(reader.fieldnames or ()))
        if missing_columns:
            raise ValueError(
                f"semantic correction ledger missing columns: {', '.join(missing_columns)}"
            )
        for row in reader:
            row_count += 1
            missing_required += int(any(not str(row.get(key) or "") for key in required))
            old_id = str(row.get("old_fact_id") or "")
            new_id = str(row.get("new_fact_id") or "")
            duplicate_old += int(old_id in old_ids)
            duplicate_new += int(new_id in new_ids)
            old_ids.add(old_id)
            new_ids.add(new_id)
    return {
        "row_count": row_count,
        "unique_old_fact_id_count": len(old_ids),
        "unique_new_fact_id_count": len(new_ids),
        "duplicate_old_fact_id_count": duplicate_old,
        "duplicate_new_fact_id_count": duplicate_new,
        "same_old_new_fact_id_count": len(old_ids & new_ids),
        "missing_required_field_row_count": missing_required,
        "valid": not any(
            (duplicate_old, duplicate_new, missing_required, len(old_ids & new_ids))
        ),
    }


def build_report() -> dict[str, Any]:
    operational = _read_json(OPERATIONAL)
    historical = _read_json(HISTORICAL)
    correction = _read_json(CORRECTION_SUMMARY)
    value_integrity = _read_json(VALUE_INTEGRITY)
    observed = operational["observed_mapping_coverage"]
    factor_actual = operational["factor_coverage"]
    historical_statements = historical["financial_statements"]
    dependency = historical["factor_dependencies"]
    migration = migration_coverage(RULES)
    canonical = canonical_rule_coverage(
        RULES, CANONICAL, text_normalizer=normalize_account_name
    )
    source_integrity = migration_integrity(RULES, project_root=PROJECT_ROOT)
    sign_audit = static_sign_policy_audit(RULES)
    ledger = _validate_ledger(CORRECTION_LEDGER)
    v3_count = int(
        observed.get("v3_mapped_row_count", observed["v2_mapped_row_count"])
    )
    v3_pct = float(observed.get("v3_mapped_row_pct", observed["v2_mapped_row_pct"]))
    v3_amount_pct = float(
        observed.get(
            "v3_mapped_absolute_amount_pct",
            observed["v2_mapped_absolute_amount_pct"],
        )
    )
    return {
        "semantic_engine_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_language": {
            "engine": "arcana-financial-semantic-v3",
            "execution": "typed deterministic match-capture-constraint-emit using spaCy indexes",
            "hierarchical_context_dimensions": historical[
                "hierarchical_context_policy"
            ]["dimensions"],
            "production_ml_or_llm_matching": False,
        },
        "rule_migration": {
            "source_rule_count": migration.total_source_rules,
            "migrated_source_rule_count": migration.migrated_source_rules,
            "coverage_pct": migration.coverage_pct,
            "source_hash_integrity": source_integrity,
        },
        "canonical_rule_coverage": canonical,
        "operational_statement_coverage": {
            "input_file_count": observed["file_count"],
            "row_count": observed["row_count"],
            "v3_mapped_row_count": v3_count,
            "v3_row_coverage_pct": v3_pct,
            "v3_monetary_coverage_pct": v3_amount_pct,
            "legacy_replay_mapped_row_count": observed[
                "legacy_replay_mapped_row_count"
            ],
            "legacy_replay_row_coverage_pct": observed[
                "legacy_replay_mapped_row_pct"
            ],
            "row_coverage_delta_pct_points": observed[
                "mapping_pct_point_delta"
            ],
            "newly_mapped_row_count": observed["newly_mapped_row_count"],
            "changed_mapping_count": observed["changed_mapping_count"],
            "excluded_invalid_amount_row_count": observed[
                "excluded_invalid_amount_row_count"
            ],
            "accounting_regime_metadata": observed[
                "accounting_regime_row_counts"
            ],
        },
        "historical_2000_2012": {
            "sample_method": historical_statements["sample_method"],
            "coverage": historical_statements["coverage"],
            "regime_file_counts": historical_statements["regime_file_counts"],
            "years": historical_statements["years"],
            "unmapped_taxonomy": historical_statements["unmapped_taxonomy"],
            "invariant_status_counts": historical_statements[
                "invariant_status_counts"
            ],
            "invariant_review_examples": historical_statements[
                "invariant_review_examples"
            ],
            "provenance_completeness_pct": historical_statements[
                "provenance_completeness_pct"
            ],
            "invalid_unit_factor_row_count": historical_statements[
                "invalid_unit_factor_row_count"
            ],
            "canonical_direction_mismatch_count": historical_statements[
                "canonical_direction_mismatch_count"
            ],
            "ambiguous_auto_emit_count": historical_statements[
                "ambiguous_auto_emit_count"
            ],
            "availability": historical["availability"],
            "accounting_regime_policy": historical["accounting_regime_policy"],
            "hierarchical_context_policy": historical[
                "hierarchical_context_policy"
            ],
        },
        "disclosure_normalization": {
            "financial_notes": historical["financial_notes"],
            "business_content": historical["business_content"],
            "policy": "all visible paragraphs/tables enter IR; semantic candidates never auto-emit without an approved golden rule",
        },
        "factor_coverage": {
            "actual_materialized_cells": {
                "as_of_date": factor_actual["as_of_date"],
                "row_count": factor_actual["row_count"],
                "factor_count": factor_actual["factor_count"],
                "covered_cells": factor_actual["covered_cells"],
                "total_cells": factor_actual["total_cells"],
                "coverage_pct": factor_actual["coverage_pct"],
                "lowest_factors": factor_actual["lowest_factors"],
            },
            "historical_input_dependency": {
                "factor_count": dependency["factor_count"],
                "financial_dependency_factor_count": dependency[
                    "financial_dependency_factor_count"
                ],
                "covered_factor_count": dependency["covered_factor_count"],
                "coverage_pct": dependency["factor_input_coverage_pct"],
            },
            "core_economic_concepts": historical["core_economic_concepts"],
        },
        "semantic_integrity": {
            "sign_policy": sign_audit,
            "observed_value_corpus": value_integrity["observed_corpus"],
            "correction_ledger": ledger,
            "capex_correction_summary": correction,
            "accounting_identity_policy": "candidate evidence only; scope/period/currency/unit/basis mismatches and missing required facts are NOT_TESTABLE",
        },
        "limitations": [
            "The operational corpus predates v3 metadata, so accounting regime and sector are UNKNOWN there; v3 context gates become active when those fields are supplied.",
            "No local PIT security-context registry was available for the 2000-2012 run, so unknown sectors were not guessed.",
            "Actual materialized factor-cell coverage is the existing snapshot as-of date and is distinct from dependency coverage after semantic remapping.",
            "Without a human-labelled gold corpus, these audits measure coverage and invariant evidence, not a statistically valid precision/recall rate.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the consolidated semantic engine v3 coverage report."
    )
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
                "migration_coverage_pct": report["rule_migration"][
                    "coverage_pct"
                ],
                "canonical_rule_coverage_pct": report[
                    "canonical_rule_coverage"
                ]["coverage_pct"],
                "operational_row_coverage_pct": report[
                    "operational_statement_coverage"
                ]["v3_row_coverage_pct"],
                "historical_monetary_coverage_pct": report[
                    "historical_2000_2012"
                ]["coverage"]["monetary_coverage_pct"],
                "actual_factor_cell_coverage_pct": report["factor_coverage"][
                    "actual_materialized_cells"
                ]["coverage_pct"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
