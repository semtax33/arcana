"""Arcana Financial Semantic Rule Engine v3.

The package exposes a source-faithful Document IR, typed deterministic rules,
spaCy-backed matching, evidence-based accounting regime detection, and the
reported/canonical/harmonized fact layers.
"""

from .detection import (
    AccountingRegimeDetector,
    detect_document_dialect,
    detect_scope,
)
from .document import (
    DartHtmlDocumentAdapter,
    build_document_ir_from_rows,
    reconstruct_html_table_grid,
)
from .matcher import SemanticMatch, SemanticRuleExecutor, SpacyPatternIndex
from .models import *
from .corrections import SemanticCorrection, capex_direction_correction
from .disclosures import (
    DisclosureDocument,
    DisclosureFactCandidate,
    DisclosureHtmlParser,
    NarrativeRelationExtractor,
    write_disclosure_csvs,
)
from .factor_graph import FactorDependencyGraph, FactorImpact, core_concept_coverage
from .invariants import AccountingInvariantAuditor, InvariantContext, InvariantEvidence
from .narrative import MoneyMention, NarrativeAccountScanner, NarrativeFactCandidate
from .normalizer import FinancialSemanticNormalizer, SemanticFieldNormalizer
from .unmapped import (
    CanonicalSuggestion,
    HistoricalLexiconCandidate,
    HistoricalLexiconMiner,
    UnmappedAssessment,
    UnmappedClassifier,
)
from .rules import (
    EmitAction,
    RuleApplicability,
    RulePhase,
    SemanticRule,
    SemanticRuleSet,
    StructuralConstraint,
    TextPredicate,
    compile_legacy_mapping_rule,
    compile_v2_mapping_rule,
    load_semantic_mapping_rules,
)

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
    "MoneyMention",
    "NarrativeAccountScanner",
    "NarrativeFactCandidate",
    "NarrativeRelationExtractor",
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
    "compile_legacy_mapping_rule",
    "compile_v2_mapping_rule",
    "capex_direction_correction",
    "core_concept_coverage",
    "detect_document_dialect",
    "detect_scope",
    "load_semantic_mapping_rules",
    "reconstruct_html_table_grid",
    "write_disclosure_csvs",
]
