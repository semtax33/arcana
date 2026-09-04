from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import re
from typing import Iterable, Mapping

from bs4 import BeautifulSoup, Tag
from spacy.lang.xx import MultiLanguage
from spacy.matcher import PhraseMatcher
from spacy.tokens import Doc

from .detection import detect_scope
from .document import DartHtmlDocumentAdapter, reconstruct_html_table_grid
from .models import (
    DisclosureSourceType,
    FinancialDocumentIR,
    NarrativeRelation,
    Qualifier,
    Scope,
)
from .narrative import NarrativeAccountScanner, NarrativeFactCandidate


_RELATION_CUES: Mapping[NarrativeRelation, tuple[str, ...]] = {
    NarrativeRelation.PAID: ("지급", "지출", "납부", "상환"),
    NarrativeRelation.INCURRED: ("발생", "인식", "계상"),
    NarrativeRelation.ACQUIRED: ("취득", "매입", "투자"),
    NarrativeRelation.DISPOSED: ("처분", "매각", "회수"),
    NarrativeRelation.INCREASED_TO: ("증가", "늘어", "상승"),
    NarrativeRelation.DECREASED_TO: ("감소", "줄어", "하락"),
    NarrativeRelation.ORDER_BACKLOG: ("수주잔고", "계약잔고"),
    NarrativeRelation.REVENUE: ("매출", "수익"),
    NarrativeRelation.CAPACITY: ("생산능력", "생산능력은"),
    NarrativeRelation.PRODUCTION: ("생산실적", "생산량"),
    NarrativeRelation.UTILIZATION: ("가동률",),
    NarrativeRelation.PLANNED: ("계획", "예정", "전망"),
    NarrativeRelation.BALANCE: ("잔액", "잔고", "보유"),
}
_PERIOD_CUES = {
    "CURRENT": ("당기", "당분기", "당반기", "금기", "현재", "기말"),
    "COMPARATIVE": ("전기", "전분기", "전반기", "전년", "전기말"),
    "OPENING": ("기초", "전기초"),
}
_QUALIFIER_CUES = {
    Qualifier.PLAN: ("계획", "예정"),
    Qualifier.FORECAST: ("전망", "예상", "추정"),
    Qualifier.MAXIMUM: ("최대", "상한"),
    Qualifier.MINIMUM: ("최소", "하한"),
    Qualifier.APPROXIMATE: ("약", "대략", "내외"),
}
_HEADING_RE = re.compile(r"^\s*(?:[IVXⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.|\d+[.)]|[가-힣][.)])\s*")


@dataclass(frozen=True)
class DisclosureFactCandidate:
    source_type: DisclosureSourceType
    document_id: str
    section_path: tuple[str, ...]
    canonical_ids: tuple[str, ...]
    matched_alias: str
    relation: NarrativeRelation
    value: Decimal
    currency: str
    unit: str
    period_role: str
    scope: Scope
    qualifier: Qualifier
    cash_direction: str
    source_text: str
    source_uri: str
    source_hash: str
    sector_code: str = ""
    industry_group_code: str = ""
    table_kind: str = "NARRATIVE"
    context_eligible: bool = True
    table_index: int | None = None
    row_index: int | None = None
    confidence: str = "review"
    review_required: bool = True
    auto_emit_eligible: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DisclosureSection:
    section_index: int
    title: str
    normalized_title: str
    text: str


@dataclass(frozen=True)
class DisclosureTable:
    table_index: int
    section_path: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    unit_text: str = ""


@dataclass
class DisclosureDocument:
    document_id: str
    source_type: DisclosureSourceType
    source_uri: str
    source_hash: str
    ir: FinancialDocumentIR
    sector_code: str = ""
    industry_group_code: str = ""
    sections: list[DisclosureSection] = field(default_factory=list)
    tables: list[DisclosureTable] = field(default_factory=list)
    candidates: list[DisclosureFactCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class NarrativeRelationExtractor:
    """spaCy-backed deterministic ACCOUNT–RELATION–AMOUNT relation capture."""

    def __init__(self, account_scanner: NarrativeAccountScanner) -> None:
        self.account_scanner = account_scanner
        self.nlp = MultiLanguage()
        self.nlp.add_pipe("sentencizer")
        self.matcher = PhraseMatcher(self.nlp.vocab, attr="ORTH")
        self._relations: dict[int, NarrativeRelation] = {}
        for index, (relation, cues) in enumerate(_RELATION_CUES.items()):
            name = f"DISCLOSURE_RELATION_{index}"
            self.matcher.add(name, [self._char_doc(self._normalize(cue)) for cue in cues])
            self._relations[self.nlp.vocab.strings[name]] = relation

    def _char_doc(self, value: str) -> Doc:
        return Doc(self.nlp.vocab, words=list(value) if value else [""], spaces=[False] * max(1, len(value)))

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s\u3000ㆍ·]", "", str(value or "")).lower()

    def relation(self, text: str) -> NarrativeRelation:
        normalized = self._normalize(text)
        matches = self.matcher(self._char_doc(normalized))
        if not matches:
            return NarrativeRelation.REPORTED
        # Prefer the most specific (longest) cue, then stable rule order.
        match_id, start, end = max(matches, key=lambda item: (item[2] - item[1], -item[1]))
        return self._relations[match_id]

    @staticmethod
    def period_role(text: str, raw_amount: str, amount_count: int) -> str:
        compact = NarrativeRelationExtractor._normalize(text)
        amount = NarrativeRelationExtractor._normalize(raw_amount)
        position = compact.find(amount)
        before = compact[max(0, position - 60) : position] if position >= 0 else compact
        hits = [role for role, cues in _PERIOD_CUES.items() if any(cue in before for cue in cues)]
        if len(hits) == 1:
            return hits[0]
        whole_hits = [role for role, cues in _PERIOD_CUES.items() if any(cue in compact for cue in cues)]
        if amount_count == 1 and len(whole_hits) == 1:
            return whole_hits[0]
        return "AMBIGUOUS" if len(set(whole_hits)) > 1 or amount_count > 1 else "UNKNOWN"

    @staticmethod
    def qualifier(text: str) -> Qualifier:
        compact = NarrativeRelationExtractor._normalize(text)
        for qualifier, cues in _QUALIFIER_CUES.items():
            if any(cue in compact for cue in cues):
                return qualifier
        return Qualifier.ACTUAL

    @staticmethod
    def cash_direction(relation: NarrativeRelation) -> str:
        if relation in {NarrativeRelation.PAID, NarrativeRelation.ACQUIRED}:
            return "outflow"
        if relation == NarrativeRelation.DISPOSED:
            return "inflow"
        return ""

    def extract(
        self,
        text: str,
        *,
        source_type: DisclosureSourceType,
        document_id: str,
        section_path: tuple[str, ...],
        source_uri: str,
        source_hash: str,
        sector_code: str = "",
        industry_group_code: str = "",
        table_kind: str = "NARRATIVE",
        table_index: int | None = None,
        row_index: int | None = None,
    ) -> tuple[DisclosureFactCandidate, ...]:
        base = self.account_scanner.scan_text(text)
        amount_count = len(self.account_scanner.extract_money_mentions(text))
        relation = self.relation(text)
        scope = detect_scope(text)
        qualifier = self.qualifier(text)
        output: list[DisclosureFactCandidate] = []
        for candidate in base:
            contextual_ids, context_eligible = (
                self.account_scanner.contextual_canonical_ids(
                    candidate.matched_alias,
                    candidate.canonical_ids,
                    source_type=source_type,
                    sector_code=sector_code,
                    industry_group_code=industry_group_code,
                    table_kind=table_kind,
                )
            )
            period_role = self.period_role(text, candidate.raw_amount, amount_count)
            reasons = list(candidate.reasons)
            if period_role in {"AMBIGUOUS", "UNKNOWN"}:
                reasons.append(f"period_{period_role.lower()}")
            if scope == Scope.UNKNOWN:
                reasons.append("scope_unknown")
            if not context_eligible:
                reasons.append("rule_context_not_satisfied")
            review_required = (
                candidate.review_required
                or period_role == "AMBIGUOUS"
                or not context_eligible
            )
            # Narrative output is a reviewable semantic layer. Production emit is opt-in
            # only after a golden rule is approved, so ambiguous automatic emits stay zero.
            output.append(
                DisclosureFactCandidate(
                    source_type=source_type,
                    document_id=document_id,
                    section_path=section_path,
                    canonical_ids=contextual_ids,
                    matched_alias=candidate.matched_alias,
                    relation=relation,
                    value=candidate.value_krw,
                    currency="KRW",
                    unit=candidate.unit,
                    period_role=period_role,
                    scope=scope,
                    qualifier=qualifier,
                    cash_direction=self.cash_direction(relation),
                    source_text=candidate.source_text,
                    source_uri=source_uri,
                    source_hash=source_hash,
                    sector_code=sector_code,
                    industry_group_code=industry_group_code,
                    table_kind=table_kind,
                    context_eligible=context_eligible,
                    table_index=table_index,
                    row_index=row_index,
                    confidence="high" if not review_required else "review",
                    review_required=review_required,
                    auto_emit_eligible=False,
                    reasons=tuple(reasons),
                )
            )
        return tuple(output)


class DisclosureHtmlParser:
    """Parse every visible note/business paragraph and table into IR and semantic candidates."""

    def __init__(self, relation_extractor: NarrativeRelationExtractor) -> None:
        self.relation_extractor = relation_extractor

    @staticmethod
    def _is_heading(node: Tag, text: str) -> bool:
        classes = " ".join(map(str, node.get("class") or []))
        return bool("section-" in classes or "table-group" in classes or (len(text) <= 180 and _HEADING_RE.match(text)))

    def parse(
        self,
        path: str | Path,
        *,
        source_type: DisclosureSourceType,
        sector_code: str = "",
        industry_group_code: str = "",
    ) -> DisclosureDocument:
        path = Path(path)
        raw = path.read_bytes()
        source_hash = sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(text, "lxml")
        ir = DartHtmlDocumentAdapter(text_normalizer=self.relation_extractor._normalize).parse(path)
        result = DisclosureDocument(
            source_hash,
            source_type,
            str(path),
            source_hash,
            ir,
            sector_code=sector_code,
            industry_group_code=industry_group_code,
        )
        section_path: tuple[str, ...] = ()
        paragraph_index = 0
        for node in soup.find_all(["p", "div", "li"]):
            if not isinstance(node, Tag) or node.find_parent("table") is not None:
                continue
            if node.find(["p", "div", "li"], recursive=False) is not None:
                continue
            node_text = " ".join(node.get_text(" ", strip=True).split())
            if not node_text:
                continue
            if self._is_heading(node, node_text):
                section_path = (node_text[:300],)
            result.sections.append(
                DisclosureSection(paragraph_index, section_path[-1] if section_path else "", self.relation_extractor._normalize(section_path[-1] if section_path else ""), node_text)
            )
            result.candidates.extend(
                self.relation_extractor.extract(
                    node_text,
                    source_type=source_type,
                    document_id=source_hash,
                    section_path=section_path,
                    source_uri=str(path),
                    source_hash=source_hash,
                    sector_code=sector_code,
                    industry_group_code=industry_group_code,
                    table_kind="NARRATIVE",
                )
            )
            paragraph_index += 1

        for table_index, table in enumerate(soup.find_all("table")):
            grid = reconstruct_html_table_grid(table)
            rows = tuple(
                tuple(" ".join((cell.get_text(" ", strip=True) if cell else "").split()) for cell in row)
                for row in grid
            )
            nearby = table.find_previous(["p", "div"])
            title = " ".join(nearby.get_text(" ", strip=True).split())[:300] if isinstance(nearby, Tag) else ""
            table_section = (title,) if title else section_path
            unit_match = re.search(r"단위\s*[:：]?\s*([^,\)\]]+)", table.get_text(" ", strip=True))
            result.tables.append(DisclosureTable(table_index, table_section, rows, unit_match.group(1).strip() if unit_match else ""))
            for row_index, row in enumerate(rows):
                row_text = " | ".join(value for value in row if value)
                if not row_text:
                    continue
                result.candidates.extend(
                    self.relation_extractor.extract(
                        row_text,
                        source_type=source_type,
                        document_id=source_hash,
                        section_path=table_section,
                        source_uri=str(path),
                        source_hash=source_hash,
                        sector_code=sector_code,
                        industry_group_code=industry_group_code,
                        table_kind="TABLE",
                        table_index=table_index,
                        row_index=row_index,
                    )
                )
        return result


def disclosure_records(document: DisclosureDocument) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    sections = [
        {**asdict(item), "source_type": document.source_type.value, "document_id": document.document_id, "source_uri": document.source_uri, "source_hash": document.source_hash}
        for item in document.sections
    ]
    tables = [
        {**asdict(item), "section_path": " > ".join(item.section_path), "rows": repr(item.rows), "source_type": document.source_type.value, "document_id": document.document_id, "source_uri": document.source_uri, "source_hash": document.source_hash}
        for item in document.tables
    ]
    facts: list[dict[str, object]] = []
    for item in document.candidates:
        row = asdict(item)
        for key in ("source_type", "relation", "scope", "qualifier"):
            value = row[key]
            row[key] = value.value if hasattr(value, "value") else str(value)
        row["canonical_ids"] = "|".join(item.canonical_ids)
        row["section_path"] = " > ".join(item.section_path)
        row["reasons"] = "|".join(item.reasons)
        row["value"] = str(item.value)
        facts.append(row)
    return sections, tables, facts


def write_disclosure_csvs(documents: Iterable[DisclosureDocument], output_dir: str | Path) -> tuple[Path, Path, Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    section_rows: list[dict[str, object]] = []
    table_rows: list[dict[str, object]] = []
    fact_rows: list[dict[str, object]] = []
    for document in documents:
        sections, tables, facts = disclosure_records(document)
        section_rows.extend(sections)
        table_rows.extend(tables)
        fact_rows.extend(facts)
    review_rows = [row for row in fact_rows if row.get("review_required")]
    paths = tuple(output_dir / name for name in ("kr_disclosure_sections.csv", "kr_disclosure_tables.csv", "kr_disclosure_facts.csv", "kr_disclosure_review.csv"))
    for path, rows in zip(paths, (section_rows, table_rows, fact_rows, review_rows)):
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames or ["document_id"])
            writer.writeheader()
            writer.writerows(rows)
    return paths  # type: ignore[return-value]
