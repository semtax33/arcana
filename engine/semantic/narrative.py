from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Mapping

from bs4 import BeautifulSoup, Tag
from spacy.lang.xx import MultiLanguage
from spacy.matcher import PhraseMatcher
from spacy.tokens import Doc

from .rules import SemanticRuleSet


NARRATIVE_UNIT_FACTORS: Mapping[str, Decimal] = {
    "조원": Decimal(1_000_000_000_000),
    "십억원": Decimal(1_000_000_000),
    "천만원": Decimal(10_000_000),
    "백만원": Decimal(1_000_000),
    "십만원": Decimal(100_000),
    "억원": Decimal(100_000_000),
    "만원": Decimal(10_000),
    "천원": Decimal(1_000),
    "백원": Decimal(100),
    "십원": Decimal(10),
    "원": Decimal(1),
}
_UNIT_PATTERN = "|".join(
    re.escape(value) for value in sorted(NARRATIVE_UNIT_FACTORS, key=len, reverse=True)
)
_MONEY_RE = re.compile(
    rf"(?P<prefix>\(\s*[△▲+\-－]\s*\)|[△▲+\-－]?)\s*"
    rf"(?P<open>\()?\s*"
    rf"(?P<number>(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?)\s*"
    rf"(?P<unit>{_UNIT_PATTERN})(?P<close>\))?"
)
_GENERIC_ALIASES = {"계", "합계", "총계", "증가", "감소", "손익", "비용", "수익"}


@dataclass(frozen=True)
class NarrativeFactCandidate:
    canonical_ids: tuple[str, ...]
    matched_alias: str
    raw_amount: str
    value_krw: Decimal
    unit: str
    paragraph_index: int
    source_text: str
    distance: int
    confidence: str
    review_required: bool
    reasons: tuple[str, ...]


class NarrativeAccountScanner:
    """Find account/value candidates hidden in prose without auto-emitting facts.

    This is deliberately a discovery/capture layer.  Only an unambiguous alias,
    one nearby unit-bearing amount and one account mention produce a confirmed
    candidate.  Everything else remains review-required and cannot silently
    enter the production fact stream.
    """

    def __init__(self, aliases: Mapping[str, Iterable[str]], *, max_distance: int = 120):
        self.max_distance = max_distance
        self.nlp = MultiLanguage()
        self.matcher = PhraseMatcher(self.nlp.vocab, attr="ORTH")
        self._alias_by_id: dict[int, str] = {}
        self._canonical_by_alias: dict[str, tuple[str, ...]] = {}
        normalized_aliases: dict[str, set[str]] = {}
        for raw_alias, canonical_ids in aliases.items():
            alias = self.normalize_text(raw_alias)
            if len(alias) < 2 or alias in _GENERIC_ALIASES:
                continue
            normalized_aliases.setdefault(alias, set()).update(
                str(value) for value in canonical_ids if str(value) != "UNMAPPED"
            )
        for index, (alias, canonical_ids) in enumerate(sorted(normalized_aliases.items())):
            if not canonical_ids:
                continue
            name = f"NARRATIVE_ACCOUNT_{index}"
            self.matcher.add(name, [self._char_doc(alias)])
            match_id = self.nlp.vocab.strings[name]
            self._alias_by_id[match_id] = alias
            self._canonical_by_alias[alias] = tuple(sorted(canonical_ids))

    @classmethod
    def from_ruleset(
        cls, ruleset: SemanticRuleSet, *, max_distance: int = 120
    ) -> "NarrativeAccountScanner":
        aliases: dict[str, set[str]] = {}
        for rule in ruleset.normalization_rules:
            canonical_id = rule.emit.canonical_id
            if canonical_id == "UNMAPPED":
                continue
            for alias in rule.label.exact_any:
                aliases.setdefault(alias, set()).add(canonical_id)
        return cls(aliases, max_distance=max_distance)

    @staticmethod
    def normalize_text(value: str) -> str:
        return re.sub(r"[\s\u3000ㆍ·]", "", str(value or "")).lower()

    def _char_doc(self, value: str) -> Doc:
        text = str(value or "")
        return Doc(
            self.nlp.vocab,
            words=list(text) if text else [""],
            spaces=[False] * max(1, len(text)),
        )

    @staticmethod
    def _money_value(match: re.Match[str]) -> Decimal | None:
        if bool(match.group("open")) != bool(match.group("close")):
            return None
        try:
            value = Decimal(match.group("number").replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        prefix = re.sub(r"\s", "", match.group("prefix") or "")
        negative = (
            (match.group("open") == "(" and match.group("close") == ")")
            or any(sign in prefix for sign in ("△", "▲", "-", "－"))
        )
        value *= NARRATIVE_UNIT_FACTORS[match.group("unit")]
        return -value if negative else value

    def scan_text(self, text: str, *, paragraph_index: int = 0) -> tuple[NarrativeFactCandidate, ...]:
        normalized = self.normalize_text(text)
        if not normalized:
            return ()
        account_hits = [
            (match_id, start, end)
            for match_id, start, end in self.matcher(self._char_doc(normalized))
        ]
        money_hits = list(_MONEY_RE.finditer(normalized))
        if not account_hits or not money_hits:
            return ()

        candidates: list[NarrativeFactCandidate] = []
        seen: set[tuple[str, int, int]] = set()
        for match_id, start, end in account_hits:
            alias = self._alias_by_id[match_id]
            nearby = [
                money
                for money in money_hits
                if min(abs(money.start() - end), abs(start - money.end()))
                <= self.max_distance
            ]
            for money in nearby:
                key = (alias, money.start(), money.end())
                if key in seen:
                    continue
                seen.add(key)
                value = self._money_value(money)
                if value is None:
                    continue
                canonical_ids = self._canonical_by_alias[alias]
                distance = min(abs(money.start() - end), abs(start - money.end()))
                reasons: list[str] = ["unit_bearing_amount", "spacy_alias_match"]
                if len(canonical_ids) != 1:
                    reasons.append("alias_maps_to_multiple_accounts")
                if len(nearby) != 1:
                    reasons.append("multiple_nearby_amounts")
                if len(account_hits) != 1:
                    reasons.append("multiple_account_mentions")
                review_required = any(
                    reason.startswith(("alias_maps", "multiple_")) for reason in reasons
                )
                candidates.append(
                    NarrativeFactCandidate(
                        canonical_ids=canonical_ids,
                        matched_alias=alias,
                        raw_amount=money.group(0),
                        value_krw=value,
                        unit=money.group("unit"),
                        paragraph_index=paragraph_index,
                        source_text=" ".join(str(text).split())[:1000],
                        distance=distance,
                        confidence="high" if not review_required else "review",
                        review_required=review_required,
                        reasons=tuple(reasons),
                    )
                )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.paragraph_index,
                    item.distance,
                    item.matched_alias,
                    item.raw_amount,
                ),
            )
        )

    def scan_soup(self, soup: BeautifulSoup) -> tuple[NarrativeFactCandidate, ...]:
        candidates: list[NarrativeFactCandidate] = []
        paragraph_index = 0
        for node in soup.find_all(["p", "div", "li"]):
            if not isinstance(node, Tag) or node.find_parent("table") is not None:
                continue
            if node.find_parent(["script", "style"]) is not None:
                continue
            if node.find(["p", "div", "li"], recursive=False) is not None:
                continue
            text = node.get_text(" ", strip=True)
            if len(text) < 5 or len(text) > 5_000:
                continue
            candidates.extend(self.scan_text(text, paragraph_index=paragraph_index))
            paragraph_index += 1
        return tuple(candidates)

    @staticmethod
    def confirmed(
        candidates: Iterable[NarrativeFactCandidate],
    ) -> tuple[NarrativeFactCandidate, ...]:
        return tuple(candidate for candidate in candidates if not candidate.review_required)
