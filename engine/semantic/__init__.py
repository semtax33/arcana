"""Arcana Financial Semantic Rule Engine v6.

Public exports are loaded on first use. Lightweight consumers such as the US
SEC rule DSL therefore do not import spaCy, Thinc, or PyTorch just to parse a
deterministic rule file.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AccountingRegimeDetector": ".detection",
    "detect_document_dialect": ".detection",
    "detect_scope": ".detection",
    "DartHtmlDocumentAdapter": ".document",
    "build_document_ir_from_rows": ".document",
    "reconstruct_html_table_grid": ".document",
    "SemanticMatch": ".matcher",
    "SemanticRuleExecutor": ".matcher",
    "SpacyPatternIndex": ".matcher",
    "SemanticCorrection": ".corrections",
    "capex_direction_correction": ".corrections",
    "DisclosureDocument": ".disclosures",
    "DisclosureFactCandidate": ".disclosures",
    "DisclosureHtmlParser": ".disclosures",
    "NarrativeRelationExtractor": ".disclosures",
    "write_disclosure_csvs": ".disclosures",
    "FactorDependencyGraph": ".factor_graph",
    "FactorImpact": ".factor_graph",
    "core_concept_coverage": ".factor_graph",
    "AccountingInvariantAuditor": ".invariants",
    "InvariantContext": ".invariants",
    "InvariantEvidence": ".invariants",
    "NotTestableReason": ".invariants",
    "summarize_invariant_evidence": ".invariants",
    "RuleBundleIntegrityError": ".manifest",
    "resolve_rule_bundle": ".manifest",
    "validate_rule_manifest": ".manifest",
    "CoverageStratifier": ".quality",
    "CoverageWaterfall": ".quality",
    "build_kr_financial_availability_dataframe": ".point_in_time",
    "CompanyYearCompletenessAuditor": ".completeness",
    "MissingFactCause": ".completeness",
    "classify_missing_fact": ".completeness",
    "summarize_company_year_completeness": ".completeness",
    "GoldenCorpusEvaluator": ".golden",
    "audit_portfolio_factor_drift": ".portfolio_drift",
    "MoneyMention": ".narrative",
    "NarrativeAccountScanner": ".narrative",
    "NarrativeFactCandidate": ".narrative",
    "cluster_narrative_candidates": ".narrative_clusters",
    "FinancialSemanticNormalizer": ".normalizer",
    "SemanticFieldNormalizer": ".normalizer",
    "CanonicalSuggestion": ".unmapped",
    "HistoricalLexiconCandidate": ".unmapped",
    "HistoricalLexiconMiner": ".unmapped",
    "UnmappedAssessment": ".unmapped",
    "UnmappedClassifier": ".unmapped",
    "EmitAction": ".rules",
    "RuleApplicability": ".rules",
    "RulePhase": ".rules",
    "SemanticRule": ".rules",
    "SemanticRuleSet": ".rules",
    "StructuralConstraint": ".rules",
    "TextPredicate": ".rules",
    "compile_legacy_mapping_rule": ".rules",
    "compile_v2_mapping_rule": ".rules",
    "load_semantic_mapping_rules": ".rules",
}

# ``models`` used to be imported with ``*``. Preserve all of those direct
# attributes, while keeping the original curated ``__all__`` below.
for _model_name in (
    "StrEnum",
    "AccountingRegimeFamily",
    "DocumentDialect",
    "StatementType",
    "Scope",
    "PeriodKind",
    "PeriodView",
    "DurationView",
    "Comparability",
    "LossState",
    "UnmappedCategory",
    "DisclosureSourceType",
    "NarrativeRelation",
    "Qualifier",
    "SemanticContext",
    "NodeKind",
    "RelationType",
    "AccountingStandard",
    "AccountingRegime",
    "RegimeEvidence",
    "RegimeDetection",
    "SourceLocation",
    "SemanticAddress",
    "DocumentNode",
    "DocumentRelation",
    "FinancialDocumentIR",
    "FactIdentity",
    "MatchProvenance",
    "SemanticLoss",
    "ReportedFact",
    "CanonicalFact",
    "HarmonizedFact",
):
    _EXPORTS[_model_name] = ".models"


__all__ = [
    "AccountingRegimeDetector",
    "AccountingInvariantAuditor",
    "CanonicalSuggestion",
    "DartHtmlDocumentAdapter",
    "EmitAction",
    "FinancialSemanticNormalizer",
    "DisclosureDocument",
    "DisclosureFactCandidate",
    "DisclosureHtmlParser",
    "FactorDependencyGraph",
    "FactorImpact",
    "HistoricalLexiconCandidate",
    "HistoricalLexiconMiner",
    "InvariantEvidence",
    "InvariantContext",
    "NotTestableReason",
    "MoneyMention",
    "NarrativeAccountScanner",
    "NarrativeFactCandidate",
    "NarrativeRelationExtractor",
    "cluster_narrative_candidates",
    "RuleApplicability",
    "RulePhase",
    "SemanticFieldNormalizer",
    "SemanticMatch",
    "SemanticRule",
    "SemanticRuleExecutor",
    "SemanticRuleSet",
    "SpacyPatternIndex",
    "SemanticCorrection",
    "SemanticContext",
    "StructuralConstraint",
    "TextPredicate",
    "UnmappedAssessment",
    "UnmappedClassifier",
    "build_document_ir_from_rows",
    "build_kr_financial_availability_dataframe",
    "CompanyYearCompletenessAuditor",
    "MissingFactCause",
    "classify_missing_fact",
    "summarize_company_year_completeness",
    "GoldenCorpusEvaluator",
    "audit_portfolio_factor_drift",
    "compile_legacy_mapping_rule",
    "compile_v2_mapping_rule",
    "capex_direction_correction",
    "core_concept_coverage",
    "detect_document_dialect",
    "detect_scope",
    "load_semantic_mapping_rules",
    "reconstruct_html_table_grid",
    "resolve_rule_bundle",
    "RuleBundleIntegrityError",
    "validate_rule_manifest",
    "CoverageStratifier",
    "CoverageWaterfall",
    "summarize_invariant_evidence",
    "write_disclosure_csvs",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
