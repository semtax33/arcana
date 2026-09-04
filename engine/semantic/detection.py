from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
from typing import Iterable

from spacy.lang.xx import MultiLanguage
from spacy.matcher import PhraseMatcher
from spacy.tokens import Doc

from .models import (
    AccountingRegime,
    AccountingRegimeFamily,
    DocumentDialect,
    RegimeDetection,
    RegimeEvidence,
    Scope,
)


class AccountingRegimeDetector:
    """Evidence-weighted PIT regime detection; filing year is only a hint."""

    POLICY_PATTERNS = {
        AccountingRegimeFamily.K_IFRS: (
            "한국채택국제회계기준",
            "K-IFRS",
            "KIFRS",
            "국제회계기준을채택하여제정한한국채택국제회계기준",
        ),
        AccountingRegimeFamily.GENERAL_K_GAAP: (
            "일반기업회계기준",
            "일반기업회계기준에따라",
        ),
        AccountingRegimeFamily.K_GAAP: (
            "대한민국에서일반적으로인정된회계처리기준",
            "연결재무제표기준및일반적으로인정된회계원칙",
            "기업회계기준에따라",
            "종전기업회계기준",
        ),
        AccountingRegimeFamily.IFRS: (
            "International Financial Reporting Standards",
            "IFRS Accounting Standards",
        ),
        AccountingRegimeFamily.US_GAAP: (
            "accounting principles generally accepted in the United States",
            "U.S. GAAP",
            "US GAAP",
        ),
    }
    AUDIT_PATTERNS = {
        AccountingRegimeFamily.K_IFRS: (
            "한국채택국제회계기준에따라중요성의관점에서공정하게표시",
            "한국채택국제회계기준에따라작성",
        ),
        AccountingRegimeFamily.K_GAAP: (
            "대한민국의기업회계기준에따라작성",
            "기업회계기준에따라중요성의관점에서적정하게표시",
        ),
        AccountingRegimeFamily.GENERAL_K_GAAP: (
            "일반기업회계기준에따라작성",
        ),
    }
    VOCABULARY_PATTERNS = {
        AccountingRegimeFamily.K_IFRS: (
            "재무상태표",
            "포괄손익계산서",
            "기타포괄손익",
            "비지배지분",
        ),
        AccountingRegimeFamily.K_GAAP: (
            "대차대조표",
            "이익잉여금처분계산서",
            "자본조정",
            "경상이익",
            "특별손익",
        ),
    }

    def __init__(self) -> None:
        self.nlp = MultiLanguage()
        self.matcher = PhraseMatcher(self.nlp.vocab, attr="ORTH")
        self._metadata: dict[int, tuple[str, AccountingRegimeFamily, str, int]] = {}
        index = 0
        for kind, weight, patterns in (
            ("explicit_accounting_policy", 100, self.POLICY_PATTERNS),
            ("audit_report_basis", 70, self.AUDIT_PATTERNS),
            ("statement_vocabulary", 30, self.VOCABULARY_PATTERNS),
        ):
            for family, values in patterns.items():
                for value in values:
                    match_name = f"REGIME_{index}"
                    normalized = self._normalize(value)
                    self.matcher.add(match_name, [self._char_doc(normalized)])
                    self._metadata[self.nlp.vocab.strings[match_name]] = (
                        kind,
                        family,
                        value,
                        weight,
                    )
                    index += 1

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s\-_/.,:;()（）]+", "", str(value or "")).lower()

    def _char_doc(self, text: str) -> Doc:
        return Doc(self.nlp.vocab, words=list(text) or [""], spaces=[False] * max(len(text), 1))

    def detect(
        self,
        text: str,
        *,
        taxonomy_namespaces: Iterable[str] = (),
        filing_date: date | None = None,
    ) -> RegimeDetection:
        # Regime markers live in titles/policies/audit wording.  Bounding the
        # input prevents a large filing from becoming a multi-million-token Doc.
        normalized = self._normalize(text)[:100_000]
        evidence: list[RegimeEvidence] = []
        seen: set[tuple[str, AccountingRegimeFamily, str]] = set()
        for match_id, _, _ in self.matcher(self._char_doc(normalized)):
            kind, family, value, weight = self._metadata[match_id]
            key = (kind, family, value)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(RegimeEvidence(kind, value, family, weight))

        for namespace in taxonomy_namespaces:
            lower = str(namespace).lower()
            if "ifrs" in lower or "k-ifrs" in lower or "kifrs" in lower:
                family = AccountingRegimeFamily.K_IFRS
            elif "us-gaap" in lower:
                family = AccountingRegimeFamily.US_GAAP
            elif "k-gaap" in lower or "kgaap" in lower:
                family = AccountingRegimeFamily.K_GAAP
            else:
                continue
            evidence.append(
                RegimeEvidence("taxonomy_namespace", str(namespace), family, 80, "xbrl")
            )

        if filing_date is not None:
            hinted = (
                AccountingRegimeFamily.K_GAAP
                if filing_date.year <= 2010
                else AccountingRegimeFamily.K_IFRS
            )
            evidence.append(
                RegimeEvidence(
                    "filing_year_hint",
                    str(filing_date.year),
                    hinted,
                    5,
                    "metadata",
                )
            )

        scores: dict[AccountingRegimeFamily, int] = defaultdict(int)
        for item in evidence:
            scores[item.candidate] += item.weight
        for family in AccountingRegimeFamily:
            scores.setdefault(family, 0)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
        top_family, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0
        if top_score <= 0:
            top_family = AccountingRegimeFamily.UNKNOWN
            confidence = 0.0
        else:
            confidence = min(1.0, max(0.0, (top_score - second_score) / top_score))
        return RegimeDetection(
            regime=AccountingRegime(
                family=top_family,
                effective_at=filing_date,
                presentation_profile=(
                    "legacy_k_gaap"
                    if top_family == AccountingRegimeFamily.K_GAAP
                    else top_family.value.lower()
                ),
            ),
            confidence=confidence,
            scores=dict(scores),
            evidence=tuple(sorted(evidence, key=lambda item: -item.weight)),
        )


def detect_document_dialect(
    text: str,
    *,
    source: str = "dart",
    filing_date: date | None = None,
) -> DocumentDialect:
    lower = str(text or "").lower()
    source = str(source or "").lower()
    has_inline_xbrl = any(token in lower for token in ("<ix:", "xmlns:ix=", "inline xbrl"))
    has_xbrl = any(token in lower for token in ("<xbrl", "xmlns:xbrli", "contextref="))
    has_ifrs = any(token in lower for token in ("ifrs-full", "k-ifrs", "kifrs"))
    if source == "sec":
        return DocumentDialect.INLINE_XBRL if has_inline_xbrl or has_xbrl else DocumentDialect.SEC_HTML
    if has_ifrs or has_inline_xbrl:
        return DocumentDialect.DART_IFRS_XBRL
    if has_xbrl:
        return DocumentDialect.DART_XBRL_LEGACY
    if filing_date is not None and filing_date.year <= 2010:
        return DocumentDialect.DART_LEGACY_HTML
    if source == "dart":
        return DocumentDialect.DART_HTML
    return DocumentDialect.UNKNOWN


def detect_scope(text: str) -> Scope:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    if any(value in normalized for value in ("연결재무제표", "연결대차대조표", "consolidated")):
        return Scope.CONSOLIDATED
    if any(value in normalized for value in ("별도재무제표", "별도대차대조표", "separate")):
        return Scope.SEPARATE
    if any(value in normalized for value in ("개별재무제표", "개별대차대조표", "individual")):
        return Scope.INDIVIDUAL
    return Scope.UNKNOWN
