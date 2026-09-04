from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping


class StrEnum(str, Enum):
    """A dependency-free string enum that serializes cleanly."""

    def __str__(self) -> str:
        return self.value


class AccountingRegimeFamily(StrEnum):
    K_GAAP = "K_GAAP"
    GENERAL_K_GAAP = "GENERAL_K_GAAP"
    K_IFRS = "K_IFRS"
    IFRS = "IFRS"
    US_GAAP = "US_GAAP"
    UNKNOWN = "UNKNOWN"


class DocumentDialect(StrEnum):
    DART_LEGACY_HTML = "DART_LEGACY_HTML"
    DART_XBRL_LEGACY = "DART_XBRL_LEGACY"
    DART_HTML = "DART_HTML"
    DART_IFRS_XBRL = "DART_IFRS_XBRL"
    SEC_HTML = "SEC_HTML"
    INLINE_XBRL = "INLINE_XBRL"
    UNKNOWN = "UNKNOWN"


class StatementType(StrEnum):
    BS = "BS"
    IS = "IS"
    CIS = "CIS"
    CF = "CF"
    CE = "CE"
    NOTES = "NOTES"
    UNKNOWN = "UNKNOWN"


class Scope(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    SEPARATE = "SEPARATE"
    INDIVIDUAL = "INDIVIDUAL"
    UNKNOWN = "UNKNOWN"


class PeriodKind(StrEnum):
    INSTANT = "INSTANT"
    DURATION = "DURATION"
    UNKNOWN = "UNKNOWN"


class PeriodView(StrEnum):
    CURRENT = "CURRENT"
    COMPARATIVE = "COMPARATIVE"
    UNKNOWN = "UNKNOWN"


class DurationView(StrEnum):
    QUARTER = "QUARTER"
    YTD = "YTD"
    ANNUAL = "ANNUAL"
    UNKNOWN = "UNKNOWN"


class Comparability(StrEnum):
    EXACT = "EXACT"
    PRESENTATION_ONLY_DIFFERENCE = "PRESENTATION_ONLY_DIFFERENCE"
    AGGREGATION_DIFFERENCE = "AGGREGATION_DIFFERENCE"
    MEASUREMENT_DIFFERENCE = "MEASUREMENT_DIFFERENCE"
    ACCOUNTING_POLICY_BREAK = "ACCOUNTING_POLICY_BREAK"
    DERIVED_BRIDGE = "DERIVED_BRIDGE"
    UNKNOWN = "UNKNOWN"


class NodeKind(StrEnum):
    DOCUMENT = "DOCUMENT"
    SECTION = "SECTION"
    PARAGRAPH = "PARAGRAPH"
    TABLE = "TABLE"
    ROW = "ROW"
    COLUMN = "COLUMN"
    CELL = "CELL"
    XBRL_FACT = "XBRL_FACT"


class RelationType(StrEnum):
    CHILD_OF = "CHILD_OF"
    PARENT_OF = "PARENT_OF"
    COMPONENT_OF = "COMPONENT_OF"
    CONTRA_OF = "CONTRA_OF"
    SUBTOTAL_OF = "SUBTOTAL_OF"
    NET_OF = "NET_OF"
    RECONCILES_TO = "RECONCILES_TO"
    DERIVED_FROM = "DERIVED_FROM"
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    LEFT_OF = "LEFT_OF"
    RIGHT_OF = "RIGHT_OF"
    NEAR = "NEAR"
    ANCHORED_BY = "ANCHORED_BY"


@dataclass(frozen=True)
class AccountingStandard:
    topic: str
    standard_id: str
    version: str = ""
    effective_from: date | None = None
    effective_to: date | None = None


@dataclass(frozen=True)
class AccountingRegime:
    family: AccountingRegimeFamily
    effective_at: date | None = None
    presentation_profile: str = ""
    standards: tuple[AccountingStandard, ...] = ()


@dataclass(frozen=True)
class RegimeEvidence:
    kind: str
    value: str
    candidate: AccountingRegimeFamily
    weight: int
    source: str = "document"


@dataclass(frozen=True)
class RegimeDetection:
    regime: AccountingRegime
    confidence: float
    scores: Mapping[AccountingRegimeFamily, int]
    evidence: tuple[RegimeEvidence, ...] = ()


@dataclass(frozen=True)
class SourceLocation:
    source_uri: str = ""
    document_id: str = ""
    section_path: tuple[str, ...] = ()
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    xbrl_concept: str = ""
    xbrl_context_ref: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class SemanticAddress:
    section_path: tuple[str, ...] = ()
    row_header_path: tuple[str, ...] = ()
    column_header_path: tuple[str, ...] = ()
    unit: str = ""
    currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    period_kind: PeriodKind = PeriodKind.UNKNOWN
    period_view: PeriodView = PeriodView.UNKNOWN
    duration_view: DurationView = DurationView.UNKNOWN
    scope: Scope = Scope.UNKNOWN


@dataclass(frozen=True)
class DocumentNode:
    node_id: str
    kind: NodeKind
    raw_text: str = ""
    normalized_text: str = ""
    semantic_address: SemanticAddress = field(default_factory=SemanticAddress)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    source: SourceLocation = field(default_factory=SourceLocation)


@dataclass(frozen=True)
class DocumentRelation:
    source_id: str
    relation: RelationType
    target_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FinancialDocumentIR:
    document_id: str
    dialect: DocumentDialect = DocumentDialect.UNKNOWN
    accounting_regime: AccountingRegime = field(
        default_factory=lambda: AccountingRegime(AccountingRegimeFamily.UNKNOWN)
    )
    nodes: dict[str, DocumentNode] = field(default_factory=dict)
    relations: list[DocumentRelation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: DocumentNode) -> None:
        if node.node_id in self.nodes:
            raise ValueError(f"duplicate Document IR node: {node.node_id}")
        self.nodes[node.node_id] = node

    def add_relation(self, relation: DocumentRelation) -> None:
        if relation.source_id not in self.nodes:
            raise KeyError(f"unknown relation source: {relation.source_id}")
        if relation.target_id not in self.nodes:
            raise KeyError(f"unknown relation target: {relation.target_id}")
        self.relations.append(relation)

    def related(
        self,
        node_id: str,
        relation: RelationType | None = None,
        *,
        outgoing: bool = True,
    ) -> tuple[DocumentNode, ...]:
        results: list[DocumentNode] = []
        for edge in self.relations:
            if relation is not None and edge.relation != relation:
                continue
            if outgoing and edge.source_id == node_id:
                results.append(self.nodes[edge.target_id])
            elif not outgoing and edge.target_id == node_id:
                results.append(self.nodes[edge.source_id])
        return tuple(results)

    def iter_nodes(self, *kinds: NodeKind) -> Iterable[DocumentNode]:
        accepted = set(kinds)
        return (
            node
            for node in self.nodes.values()
            if not accepted or node.kind in accepted
        )


@dataclass(frozen=True)
class FactIdentity:
    entity_id: str
    metric: str
    period_start: date | None = None
    period_end: date | None = None
    scope: Scope = Scope.UNKNOWN
    accounting_regime: AccountingRegimeFamily = AccountingRegimeFamily.UNKNOWN
    published_at: datetime | None = None
    filing_id: str = ""
    revision_id: str = ""


@dataclass(frozen=True)
class MatchProvenance:
    rule_id: str
    rule_version: int
    phase: str
    source_rule_file: str = ""
    source_rule_index: int | None = None
    normalized_inputs: Mapping[str, str] = field(default_factory=dict)
    captures: Mapping[str, Any] = field(default_factory=dict)
    assertions: Mapping[str, bool] = field(default_factory=dict)
    matched_predicates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportedFact:
    identity: FactIdentity
    statement_type: StatementType
    raw_label: str
    raw_value: str
    numeric_value: Decimal | None
    normalized_label: str = ""
    unit: str = ""
    unit_multiplier: Decimal = Decimal(1)
    currency: str = ""
    address: SemanticAddress = field(default_factory=SemanticAddress)
    source: SourceLocation = field(default_factory=SourceLocation)
    document_dialect: DocumentDialect = DocumentDialect.UNKNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalFact:
    identity: FactIdentity
    canonical_id: str
    canonical_name: str
    statement_type: StatementType
    value: Decimal | None
    raw_value: Decimal | None
    amount_policy: str = "as_reported"
    cash_direction: str = ""
    cash_effect_value: Decimal | None = None
    comparability: Comparability = Comparability.EXACT
    reported_fact: ReportedFact | None = None
    provenance: MatchProvenance | None = None
    relations: tuple[DocumentRelation, ...] = ()


@dataclass(frozen=True)
class HarmonizedFact:
    identity: FactIdentity
    analytical_metric: str
    value: Decimal | None
    comparability: Comparability
    canonical_facts: tuple[CanonicalFact, ...]
    bridge_rule_id: str
    provenance: MatchProvenance | None = None

