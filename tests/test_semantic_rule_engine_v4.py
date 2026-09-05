from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from engine.semantic.factor_drift import (
    DriftSeverity,
    classify_factor_drift,
    percentile_ranks,
)
from engine.semantic.invariants import (
    AccountingInvariantAuditor,
    InvariantContext,
    NotTestableReason,
    summarize_invariant_evidence,
)
from engine.semantic.manifest import RuleBundleIntegrityError, resolve_rule_bundle
from engine.semantic.narrative_clusters import cluster_narrative_candidates
from engine.semantic.quality import CoverageStratifier, CoverageWaterfall
from scripts.audit_capex_factor_drift import build_capex_factor_drift_report


pytestmark = pytest.mark.semantic


def test_invariant_evidence_explains_why_a_fact_set_is_not_testable() -> None:
    evidence = AccountingInvariantAuditor().audit(
        {"TOTAL_ASSETS": 100, "TOTAL_LIABILITIES": 40, "TOTAL_EQUITY": 60},
        context=InvariantContext(scope_consistent=False),
    )

    balance_sheet = next(
        item
        for item in evidence
        if item.invariant_id == "BS_ASSETS_EQUALS_LIABILITIES_PLUS_EQUITY"
    )
    summary = summarize_invariant_evidence(evidence)

    assert balance_sheet.not_testable_reason is NotTestableReason.INCOMPATIBLE_SCOPE
    assert summary["reason_counts"]["incompatible_scope"] == len(evidence)
    assert summary["testable_count"] == 0
    assert summary["testability_pct"] == 0.0


def test_missing_fact_has_a_closed_not_testable_reason() -> None:
    evidence = AccountingInvariantAuditor().audit(
        {"TOTAL_ASSETS": 100, "TOTAL_LIABILITIES": 40}
    )

    balance_sheet = next(
        item
        for item in evidence
        if item.invariant_id == "BS_ASSETS_EQUALS_LIABILITIES_PLUS_EQUITY"
    )

    assert balance_sheet.not_testable_reason is NotTestableReason.MISSING_REQUIRED_FACT


def test_coverage_is_stratified_without_mixing_row_and_amount_denominators() -> None:
    stratifier = CoverageStratifier(
        dimensions=("year", "accounting_regime", "statement_type", "scope")
    )
    stratifier.add(
        {
            "year": "2011",
            "accounting_regime": "K_IFRS",
            "statement_type": "BS",
            "scope": "CONSOLIDATED",
        },
        mapped=True,
        amount=Decimal("900"),
    )
    stratifier.add(
        {
            "year": "2011",
            "accounting_regime": "K_IFRS",
            "statement_type": "BS",
            "scope": "CONSOLIDATED",
        },
        mapped=False,
        amount=Decimal("100"),
    )

    report = stratifier.report()
    bucket = report["by_dimension"]["year"][0]

    assert bucket["value"] == "2011"
    assert bucket["row_count"] == 2
    assert bucket["mapped_row_count"] == 1
    assert bucket["row_coverage_pct"] == 50.0
    assert bucket["absolute_amount"] == "1000"
    assert bucket["mapped_absolute_amount"] == "900"
    assert bucket["amount_coverage_pct"] == 90.0
    assert report["joint"][0]["dimensions"]["scope"] == "CONSOLIDATED"


def test_coverage_waterfall_preserves_each_stage_unit_and_bottleneck() -> None:
    waterfall = CoverageWaterfall()
    waterfall.add_stage("document_ir", usable=8, total=10, unit="documents")
    waterfall.add_stage("reported_fact", usable=80, total=100, unit="facts")
    waterfall.add_stage(
        "harmonized_fact",
        usable=30,
        total=80,
        unit="facts",
        loss_reasons={"incompatible_scope": 40, "unknown_unit": 10},
    )

    report = waterfall.report()

    assert report["stages"][0]["coverage_pct"] == 80.0
    assert report["stages"][1]["unit"] == "facts"
    assert report["stages"][2]["lost_count"] == 50
    assert report["bottleneck"]["stage"] == "harmonized_fact"
    assert report["bottleneck"]["loss_reasons"]["incompatible_scope"] == 40


def test_rule_manifest_resolves_alias_and_rejects_mutated_bundle() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        bundle = root / "semantic_kr_v3.yaml"
        bundle.write_text("schema_version: 3\nprofile: kr\n", encoding="utf-8")
        import hashlib

        manifest = root / "semantic_rule_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "manifest_version": 1,
                    "active_bundle": "semantic_kr_v3.yaml",
                    "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "schema_version": 3,
                    "engine_version": 4,
                }
            ),
            encoding="utf-8",
        )
        alias = root / "semantic_kr_current.yaml"
        alias.write_text(
            "schema_version: 3\nalias_of: semantic_rule_manifest.json\n",
            encoding="utf-8",
        )

        assert resolve_rule_bundle(alias) == bundle.resolve()

        bundle.write_text("schema_version: 3\nprofile: changed\n", encoding="utf-8")
        with pytest.raises(RuleBundleIntegrityError):
            resolve_rule_bundle(manifest)


def test_factor_drift_detects_sign_and_material_rank_changes() -> None:
    assert (
        classify_factor_drift(Decimal("10"), Decimal("-10")).severity
        is DriftSeverity.SIGN_FLIP
    )
    result = classify_factor_drift(
        Decimal("100"), Decimal("80"), old_percentile=0.95, new_percentile=0.55
    )
    assert result.severity is DriftSeverity.RANKING_FLIP
    assert result.relative_change == Decimal("0.2")


def test_percentile_ranks_are_deterministic_for_ties_and_missing_values() -> None:
    ranks = percentile_ranks({"a": Decimal("10"), "b": Decimal("10"), "c": Decimal("30"), "d": None})

    assert ranks == {"a": 0.25, "b": 0.25, "c": 1.0, "d": None}


def test_narrative_clusters_keep_ambiguous_candidates_out_of_auto_emit() -> None:
    candidates = [
        {
            "source_type": "FINANCIAL_NOTES",
            "canonical_ids": ("CAPEX_PPE",),
            "matched_alias": "유형자산",
            "relation": "ACQUIRED",
            "period_role": "AMBIGUOUS",
            "scope": "UNKNOWN",
            "table_kind": "NARRATIVE",
            "context_eligible": True,
            "review_required": True,
            "auto_emit_eligible": False,
            "reasons": ("ambiguous_period",),
            "source_text": "당기와 전기 유형자산 취득액은 각각 10억원과 20억원이다.",
        },
        {
            "source_type": "BUSINESS_CONTENT",
            "canonical_ids": ("REVENUE", "OTHER_REVENUE"),
            "matched_alias": "매출",
            "relation": "REVENUE",
            "period_role": "CURRENT",
            "scope": "CONSOLIDATED",
            "table_kind": "TABLE",
            "context_eligible": False,
            "review_required": True,
            "auto_emit_eligible": True,
            "reasons": ("rule_context_not_satisfied", "alias_maps_to_multiple_accounts"),
            "source_text": "부문 매출은 10억원이다.",
        },
    ]

    report = cluster_narrative_candidates(candidates, promotion_minimum=1)

    assert report["category_counts"]["period_ambiguity"] == 1
    assert report["category_counts"]["context_mismatch"] == 1
    assert report["precision_guard"]["false_semantic_emit_count"] == 1
    assert report["precision_guard"]["automatic_promotion_count"] == 0


def test_capex_drift_audit_recomputes_period_factors_after_inflow_exclusion() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        path = root / "kr_normalized_005930.csv"
        path.write_text(
            "canonical_account_id,statement_type,fiscal_year,fiscal_month,normalized_amount,cash_direction\n"
            "CFO,CF,2020,12,200,\n"
            "CAPEX_PPE,CF,2020,12,-20,outflow\n"
            "CFO,CF,2021,12,200,\n"
            "CAPEX_PPE,CF,2021,12,-30,outflow\n"
            "CFO,CF,2022,12,200,\n"
            "CAPEX_PPE,CF,2022,12,100,inflow\n"
            "CAPEX_PPE,CF,2022,12,-40,outflow\n",
            encoding="utf-8",
        )

        report = build_capex_factor_drift_report(root)

    assert report["corrected_annual_fact_count"] == 1
    assert report["structural_impact"]["affected_factor_count"] == 8
    capx = next(row for row in report["factor_summaries"] if row["factor"] == "capx")
    assert capx["changed_cell_count"] == 1
    assert capx["examples"][0]["old_value"] == "100"
    assert capx["examples"][0]["new_value"] == "40"
