from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Mapping, Sequence

from .models import Scope, StatementType, UnmappedCategory


_LOW_INFORMATION = re.compile(r"^(?:[-―—·ㆍ]|없음|해당없음|해당사항없음|미기재|n/?a)?$", re.I)
_SUBTOTAL = re.compile(r"(?:^|[^가-힣])(계|합계|총계)$|소계|총계|합\s*계|^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩivx]+\.?$", re.I)
_DIMENSION = re.compile(r"(?:부문|지역|제품군|사업부|연결조정|내부거래|미배분|기타부문)$")
_NON_FINANCIAL = re.compile(
    r"(?:임직원|종업원|특허|상표|점유율|생산능력|가동률|수주량|수주잔고|매장수|고객수|연구인력|면적|수량|톤|대수)"
)
_DISCLOSURE = re.compile(
    r"(?:담보|보증|약정|소송|우발|특수관계|퇴직급여|리스|법인세|공정가치|위험관리|만기분석|주석)"
)
_EXTENSION = re.compile(r"(?:당사|회사고유|프로젝트|사업부|브랜드|모델|광구|현장|호선|단지|펀드)")
_PERIOD_WORDS = re.compile(r"당기|전기|전전기|기초|기말|증가|감소|변동")
_SCOPE_WORDS = re.compile(r"연결|별도|개별|지배기업|종속기업")


@dataclass(frozen=True)
class CanonicalSuggestion:
    canonical_id: str
    score: float
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnmappedAssessment:
    category: UnmappedCategory
    normalized_label: str
    reasons: tuple[str, ...]
    suggestions: tuple[CanonicalSuggestion, ...] = ()
    auto_emit_eligible: bool = False


@dataclass(frozen=True)
class HistoricalLexiconCandidate:
    normalized_label: str
    statement_type: str
    occurrence_count: int
    entity_count: int
    year_count: int
    parent_contexts: tuple[str, ...]
    suggestions: tuple[CanonicalSuggestion, ...]
    approval_state: str = "REVIEW_REQUIRED"


def normalize_unmapped_label(value: object) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"\([^)]*(?:주|note|참조)[^)]*\)", "", text, flags=re.I)
    text = re.sub(r"[\s\u3000ㆍ·,.:;\[\]{}()]", "", text)
    return text


class UnmappedClassifier:
    """Closed-set taxonomy for failed mappings; it never emits a fact."""

    def classify(
        self,
        label: object,
        *,
        statement_type: StatementType | str = StatementType.UNKNOWN,
        raw_value: object = "",
        period_label: str = "",
        scope: Scope | str = Scope.UNKNOWN,
        parent_context: str = "",
        suggestions: Sequence[CanonicalSuggestion] = (),
    ) -> UnmappedAssessment:
        normalized = normalize_unmapped_label(label)
        reasons: list[str] = []
        if not normalized or _LOW_INFORMATION.fullmatch(normalized):
            return UnmappedAssessment(UnmappedCategory.LOW_INFORMATION, normalized, ("empty_or_placeholder",))

        numbers = re.findall(r"(?<![가-힣A-Za-z])[-+]?\(?\d[\d,.]*\)?", str(raw_value or ""))
        if len(str(label or "")) > 300 or len(numbers) > 1:
            reasons.append("multiple_values_or_corrupted_row")
            return UnmappedAssessment(UnmappedCategory.STRUCTURAL_PARSE_FAILURE, normalized, tuple(reasons))
        if raw_value not in (None, ""):
            try:
                if abs(Decimal(str(raw_value).replace(",", "").replace("(", "-").replace(")", ""))) > Decimal("1e18"):
                    return UnmappedAssessment(
                        UnmappedCategory.STRUCTURAL_PARSE_FAILURE,
                        normalized,
                        ("implausible_numeric_magnitude",),
                    )
            except (InvalidOperation, ValueError):
                pass

        scope_value = str(scope)
        if _SCOPE_WORDS.search(normalized) and scope_value.endswith("UNKNOWN"):
            return UnmappedAssessment(UnmappedCategory.SCOPE_AMBIGUITY, normalized, ("scope_cue_without_resolved_scope",))
        if _PERIOD_WORDS.search(normalized) and not str(period_label or "").strip():
            return UnmappedAssessment(UnmappedCategory.PERIOD_AMBIGUITY, normalized, ("period_cue_without_resolved_period",))
        if _SUBTOTAL.search(normalized):
            return UnmappedAssessment(UnmappedCategory.SUBTOTAL_OR_PRESENTATION_ONLY, normalized, ("subtotal_or_heading",))
        if _DIMENSION.search(normalized):
            return UnmappedAssessment(UnmappedCategory.DIMENSIONAL_MEMBER, normalized, ("dimension_member_label",))
        if _NON_FINANCIAL.search(normalized):
            return UnmappedAssessment(UnmappedCategory.NON_FINANCIAL, normalized, ("non_financial_measure",))
        if _DISCLOSURE.search(f"{parent_context}{normalized}"):
            return UnmappedAssessment(UnmappedCategory.DISCLOSURE_SPECIFIC, normalized, ("note_specific_concept",))
        if suggestions:
            return UnmappedAssessment(
                UnmappedCategory.KNOWN_CONCEPT_UNKNOWN_EXPRESSION,
                normalized,
                ("candidate_alias_similarity",),
                tuple(suggestions),
            )
        if _EXTENSION.search(normalized) or len(normalized) >= 35:
            return UnmappedAssessment(UnmappedCategory.ENTITY_SPECIFIC_EXTENSION, normalized, ("entity_specific_or_high_granularity",))
        return UnmappedAssessment(UnmappedCategory.UNKNOWN_LABEL, normalized, ("no_deterministic_evidence",))


class HistoricalLexiconMiner:
    """Aggregate historical expressions into review candidates, never rules."""

    def __init__(self, alias_to_canonical: Mapping[str, Iterable[str]]) -> None:
        self.aliases = {
            normalize_unmapped_label(alias): tuple(sorted(set(map(str, canonical_ids))))
            for alias, canonical_ids in alias_to_canonical.items()
            if normalize_unmapped_label(alias)
        }

    @staticmethod
    def _ngrams(text: str, size: int = 2) -> set[str]:
        if len(text) < size:
            return {text} if text else set()
        return {text[index : index + size] for index in range(len(text) - size + 1)}

    def suggest(self, label: object, *, limit: int = 3) -> tuple[CanonicalSuggestion, ...]:
        normalized = normalize_unmapped_label(label)
        source = self._ngrams(normalized)
        ranked: list[tuple[float, str, tuple[str, ...]]] = []
        for alias, canonical_ids in self.aliases.items():
            target = self._ngrams(alias)
            union = source | target
            score = len(source & target) / len(union) if union else 0.0
            if score >= 0.45:
                for canonical_id in canonical_ids:
                    ranked.append((score, canonical_id, (f"char_bigram_jaccard:{score:.4f}", f"nearest_alias:{alias}")))
        best: dict[str, tuple[float, tuple[str, ...]]] = {}
        for score, canonical_id, evidence in ranked:
            if canonical_id not in best or score > best[canonical_id][0]:
                best[canonical_id] = (score, evidence)
        return tuple(
            CanonicalSuggestion(canonical_id, score, evidence)
            for canonical_id, (score, evidence) in sorted(best.items(), key=lambda item: (-item[1][0], item[0]))[:limit]
        )

    def aggregate(self, rows: Iterable[Mapping[str, object]]) -> tuple[HistoricalLexiconCandidate, ...]:
        groups: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
        for row in rows:
            label = normalize_unmapped_label(row.get("label") or row.get("original_account_name"))
            statement_type = str(row.get("statement_type") or "UNKNOWN")
            if label:
                groups[(label, statement_type)].append(row)
        candidates: list[HistoricalLexiconCandidate] = []
        for (label, statement_type), items in groups.items():
            suggestions = self.suggest(label)
            candidates.append(
                HistoricalLexiconCandidate(
                    normalized_label=label,
                    statement_type=statement_type,
                    occurrence_count=len(items),
                    entity_count=len({str(item.get("entity_id") or item.get("company_name") or "") for item in items}),
                    year_count=len({str(item.get("year") or str(item.get("period") or "")[:4]) for item in items}),
                    parent_contexts=tuple(sorted({str(item.get("parent_context") or "") for item in items if item.get("parent_context")}))[:10],
                    suggestions=suggestions,
                )
            )
        return tuple(sorted(candidates, key=lambda item: (-item.occurrence_count, item.normalized_label)))
