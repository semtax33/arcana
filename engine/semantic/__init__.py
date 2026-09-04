"""Arcana Financial Semantic Rule Engine v2.

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
from .narrative import NarrativeAccountScanner, NarrativeFactCandidate
from .normalizer import FinancialSemanticNormalizer, SemanticFieldNormalizer
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
    "DartHtmlDocumentAdapter",
    "EmitAction",
    "FinancialSemanticNormalizer",
    "NarrativeAccountScanner",
    "NarrativeFactCandidate",
    "RuleApplicability",
    "RulePhase",
    "SemanticFieldNormalizer",
    "SemanticMatch",
    "SemanticRule",
    "SemanticRuleExecutor",
    "SemanticRuleSet",
    "SpacyPatternIndex",
    "StructuralConstraint",
    "TextPredicate",
    "build_document_ir_from_rows",
    "compile_legacy_mapping_rule",
    "compile_v2_mapping_rule",
    "detect_document_dialect",
    "detect_scope",
    "load_semantic_mapping_rules",
    "reconstruct_html_table_grid",
]
