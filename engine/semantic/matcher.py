from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
import re
from typing import Any, Callable, Iterable, Mapping

from spacy.lang.xx import MultiLanguage
from spacy.matcher import Matcher, PhraseMatcher
from spacy.tokens import Doc

from .models import (
    AccountingRegimeFamily,
    Comparability,
    DisclosureSourceType,
    DocumentDialect,
    MatchProvenance,
    RelationType,
)
from .rules import (
    RuleApplicability,
    RulePhase,
    SemanticRule,
    SemanticRuleSet,
    TextPredicate,
    phrases_from_rules,
)


@dataclass(frozen=True)
class TextAnalysis:
    text: str
    phrases: frozenset[str]
    regexes: frozenset[str]


@dataclass(frozen=True)
class SemanticMatch:
    canonical_id: str
    canonical_name: str
    rule_id: str
    reason: str
    amount_policy: str
    cash_direction: str
    comparability: Comparability
    relations: tuple[RelationType, ...]
    provenance: MatchProvenance


class SpacyPatternIndex:
    """spaCy-backed phrase/regex index for deterministic financial matching.

    Korean account labels are commonly emitted without spaces.  A character Doc
    gives PhraseMatcher stable substring semantics without requiring Mecab or a
    statistical model, while still using spaCy's compiled matching machinery.
    """

    def __init__(
        self,
        phrases: Iterable[str] = (),
        regexes: Iterable[str] = (),
    ) -> None:
        self.nlp = MultiLanguage()
        self.phrase_matcher = PhraseMatcher(self.nlp.vocab, attr="ORTH")
        self.regex_matcher = Matcher(self.nlp.vocab)
        self._phrase_by_id: dict[int, str] = {}
        self._regex_by_id: dict[int, str] = {}

        for index, phrase in enumerate(sorted(set(filter(None, phrases)))):
            match_name = f"ARCANA_PHRASE_{index}"
            self.phrase_matcher.add(match_name, [self._char_doc(phrase)])
            self._phrase_by_id[self.nlp.vocab.strings[match_name]] = phrase

        for index, expression in enumerate(sorted(set(filter(None, regexes)))):
            match_name = f"ARCANA_REGEX_{index}"
            self.regex_matcher.add(
                match_name,
                [[{"TEXT": {"REGEX": expression}}]],
            )
            self._regex_by_id[self.nlp.vocab.strings[match_name]] = expression

    def _char_doc(self, text: str) -> Doc:
        value = str(text or "")
        return Doc(
            self.nlp.vocab,
            words=list(value) if value else [""],
            spaces=[False] * max(len(value), 1),
        )

    def _single_token_doc(self, text: str) -> Doc:
        return Doc(self.nlp.vocab, words=[str(text or "")], spaces=[False])

    @lru_cache(maxsize=200_000)
    def analyze(self, text: str) -> TextAnalysis:
        value = str(text or "")
        if not value:
            return TextAnalysis(text="", phrases=frozenset(), regexes=frozenset())
        char_doc = self._char_doc(value)
        phrase_hits = frozenset(
            self._phrase_by_id[match_id]
            for match_id, _, _ in self.phrase_matcher(char_doc)
        )
        token_doc = self._single_token_doc(value)
        regex_hits = (
            frozenset(
                self._regex_by_id[match_id]
                for match_id, _, _ in self.regex_matcher(token_doc)
            )
            if self._regex_by_id
            else frozenset()
        )
        return TextAnalysis(text=value, phrases=phrase_hits, regexes=regex_hits)


class SemanticRuleExecutor:
    """Typed match -> capture -> constraint -> emit rule executor.

    The executor deliberately has no callback escape hatch.  Its supported
    operations are closed, pure and deterministic, following the useful part of
    HMRB's matcher algebra without importing HMRB itself.
    """

    _KNOWN_STATEMENT_TYPES = ("BS", "CF", "IS", "CIS", "CE", "NOTES", "UNKNOWN")

    def __init__(
        self,
        ruleset: SemanticRuleSet,
        canonical_names: Mapping[str, str],
        *,
        text_normalizer: Callable[[Any], str],
        sign_resolver: Callable[[str, str, SemanticRule], tuple[str, str]] | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.canonical_names = dict(canonical_names)
        self.valid_ids = set(self.canonical_names)
        self.text_normalizer = text_normalizer
        self.sign_resolver = sign_resolver
        indexed_rules = list(enumerate(ruleset.normalization_rules))
        indexed_rules.sort(key=lambda item: (-item[1].priority, item[0]))
        self.rules = tuple(rule for _, rule in indexed_rules)
        regexes = {
            expression
            for rule in self.rules
            for predicate in (rule.label, rule.context)
            for expression in predicate.regex_any
        }
        self.patterns = SpacyPatternIndex(phrases_from_rules(self.rules), regexes)
        self._buckets = {
            fs_type: self._build_bucket(fs_type)
            for fs_type in self._KNOWN_STATEMENT_TYPES
        }
        self._candidate_cache: dict[tuple[str, str], tuple[SemanticRule, ...]] = {}

    @staticmethod
    def statement_type_compatible(rule_type: str, row_type: str) -> bool:
        if rule_type in {"", "ANY"} or rule_type == row_type:
            return True
        return row_type == "CIS" and rule_type == "IS"

    def _build_bucket(self, fs_type: str) -> dict[str, Any]:
        exact_by_name: dict[str, list[SemanticRule]] = {}
        non_exact: list[SemanticRule] = []
        order = {id(rule): index for index, rule in enumerate(self.rules)}
        for rule in self.rules:
            if not any(
                self.statement_type_compatible(rule_type, fs_type)
                for rule_type in rule.applies.statement_types
            ):
                continue
            if rule.label.exact_any:
                for name in rule.label.exact_any:
                    exact_by_name.setdefault(name, []).append(rule)
            else:
                non_exact.append(rule)
        return {
            "exact_by_name": exact_by_name,
            "non_exact": tuple(non_exact),
            "order": order,
        }

    def _candidates(self, fs_type: str, name: str) -> tuple[SemanticRule, ...]:
        key = (fs_type, name)
        cached = self._candidate_cache.get(key)
        if cached is not None:
            return cached
        bucket = self._buckets.setdefault(fs_type, self._build_bucket(fs_type))
        exact = bucket["exact_by_name"].get(name, ())
        if not exact:
            result = bucket["non_exact"]
        else:
            result = tuple(
                sorted(
                    (*bucket["non_exact"], *exact),
                    key=lambda rule: bucket["order"][id(rule)],
                )
            )
        if len(self._candidate_cache) >= 200_000:
            self._candidate_cache.clear()
        self._candidate_cache[key] = result
        return result

    def _predicate_matches(
        self,
        analysis: TextAnalysis,
        predicate: TextPredicate,
        matched: list[str],
        prefix: str,
    ) -> bool:
        if predicate.exact_any:
            if analysis.text not in predicate.exact_any:
                return False
            matched.append(f"{prefix}.exact_any")
        if predicate.contains_all:
            if not all(value in analysis.phrases for value in predicate.contains_all):
                return False
            matched.append(f"{prefix}.contains_all")
        for index, group in enumerate(predicate.contains_any_groups):
            if group and not any(value in analysis.phrases for value in group):
                return False
            if group:
                matched.append(f"{prefix}.contains_any_groups[{index}]")
        if predicate.excludes_any:
            if any(value in analysis.phrases for value in predicate.excludes_any):
                return False
            matched.append(f"{prefix}.excludes_any")
        if predicate.regex_any:
            if not any(value in analysis.regexes for value in predicate.regex_any):
                return False
            matched.append(f"{prefix}.regex_any")
        return True

    def _applies(
        self,
        rule: SemanticRule,
        *,
        statement_type: str,
        regime: AccountingRegimeFamily,
        dialect: DocumentDialect,
        effective_at: date | None,
        relations: frozenset[RelationType],
        source_type: DisclosureSourceType,
        sector_code: str,
        industry_group_code: str,
        table_kind: str,
    ) -> bool:
        if not any(
            self.statement_type_compatible(rule_type, statement_type)
            for rule_type in rule.applies.statement_types
        ):
            return False
        if rule.applies.accounting_regimes and regime not in rule.applies.accounting_regimes:
            return False
        if rule.applies.document_dialects and dialect not in rule.applies.document_dialects:
            return False
        if rule.applies.source_types and source_type not in rule.applies.source_types:
            return False
        if rule.applies.sector_codes and sector_code not in rule.applies.sector_codes:
            return False
        if rule.applies.industry_group_codes and industry_group_code not in rule.applies.industry_group_codes:
            return False
        if rule.applies.table_kinds and table_kind not in rule.applies.table_kinds:
            return False
        if rule.applies.effective_from and (
            effective_at is None or effective_at < rule.applies.effective_from
        ):
            return False
        if rule.applies.effective_to and (
            effective_at is None or effective_at > rule.applies.effective_to
        ):
            return False
        if rule.constraints.required_relations and not set(
            rule.constraints.required_relations
        ).issubset(relations):
            return False
        return True

    def match(
        self,
        *,
        statement_type: str,
        label: str,
        context: str = "",
        has_children: bool = False,
        amount_is_zero_or_blank: bool = False,
        regime: AccountingRegimeFamily = AccountingRegimeFamily.UNKNOWN,
        dialect: DocumentDialect = DocumentDialect.UNKNOWN,
        effective_at: date | None = None,
        relations: Iterable[RelationType] = (),
        source_type: DisclosureSourceType = DisclosureSourceType.FINANCIAL_STATEMENT,
        sector_code: str = "",
        industry_group_code: str = "",
        table_kind: str = "",
    ) -> SemanticMatch:
        fs_type = str(statement_type or "UNKNOWN")
        normalized_label = self.text_normalizer(label)
        normalized_context = self.text_normalizer(context)
        label_analysis = self.patterns.analyze(normalized_label)
        context_analysis = self.patterns.analyze(normalized_context)
        relation_set = frozenset(relations)

        for rule in self._candidates(fs_type, normalized_label):
            if not self._applies(
                rule,
                statement_type=fs_type,
                regime=regime,
                dialect=dialect,
                effective_at=effective_at,
                relations=relation_set,
                source_type=source_type,
                sector_code=str(sector_code or ""),
                industry_group_code=str(industry_group_code or ""),
                table_kind=str(table_kind or ""),
            ):
                continue
            matched: list[str] = []
            if not self._predicate_matches(label_analysis, rule.label, matched, "label"):
                continue
            if not self._predicate_matches(
                context_analysis, rule.context, matched, "context"
            ):
                continue
            if (
                rule.constraints.has_children is not None
                and rule.constraints.has_children != bool(has_children)
            ):
                continue
            if (
                rule.constraints.amount_is_zero_or_blank is not None
                and rule.constraints.amount_is_zero_or_blank
                != bool(amount_is_zero_or_blank)
            ):
                continue

            canonical_id = rule.emit.canonical_id
            if canonical_id != "UNMAPPED" and canonical_id not in self.valid_ids:
                canonical_id = rule.emit.fallback_if_missing
            if canonical_id != "UNMAPPED" and canonical_id not in self.valid_ids:
                canonical_id = "UNMAPPED"
            if self.sign_resolver is None:
                amount_policy = rule.emit.amount_policy or "as_reported"
                cash_direction = rule.emit.cash_direction
            else:
                amount_policy, cash_direction = self.sign_resolver(
                    fs_type, canonical_id, rule
                )

            provenance = MatchProvenance(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                phase=rule.phase.value,
                source_rule_file=rule.source_file,
                source_rule_index=rule.source_index,
                normalized_inputs={
                    "label": normalized_label,
                    "context": normalized_context,
                    "statement_type": fs_type,
                    "accounting_regime": regime.value,
                    "document_dialect": dialect.value,
                    "source_type": source_type.value,
                    "sector_code": str(sector_code or ""),
                    "industry_group_code": str(industry_group_code or ""),
                    "table_kind": str(table_kind or ""),
                },
                captures={
                    "label": label,
                    "context": context,
                    "canonical_id": canonical_id,
                },
                assertions={
                    "canonical_id_exists": canonical_id == "UNMAPPED"
                    or canonical_id in self.valid_ids,
                    "structural_constraints": True,
                },
                matched_predicates=tuple(matched),
            )
            return SemanticMatch(
                canonical_id=canonical_id,
                canonical_name=(
                    "미매핑"
                    if canonical_id == "UNMAPPED"
                    else self.canonical_names.get(canonical_id, "미매핑")
                ),
                rule_id=rule.rule_id,
                reason=rule.reason,
                amount_policy=amount_policy,
                cash_direction=cash_direction,
                comparability=rule.emit.comparability,
                relations=rule.emit.relations,
                provenance=provenance,
            )

        provenance = MatchProvenance(
            rule_id="default_unmapped",
            rule_version=2,
            phase="normalize",
            normalized_inputs={
                "label": normalized_label,
                "context": normalized_context,
                "statement_type": fs_type,
                "accounting_regime": regime.value,
                "document_dialect": dialect.value,
                "source_type": source_type.value,
                "sector_code": str(sector_code or ""),
                "industry_group_code": str(industry_group_code or ""),
                "table_kind": str(table_kind or ""),
            },
            captures={"label": label, "context": context},
            assertions={"canonical_id_exists": True},
        )
        amount_policy = "as_reported"
        cash_direction = ""
        if self.sign_resolver is not None:
            dummy = SemanticRule(
                rule_id="default_unmapped",
                version=2,
                phase=RulePhase.NORMALIZE,
                priority=-1,
                applies=RuleApplicability(),
            )
            amount_policy, cash_direction = self.sign_resolver(
                fs_type, "UNMAPPED", dummy
            )
        return SemanticMatch(
            canonical_id="UNMAPPED",
            canonical_name="미매핑",
            rule_id="default_unmapped",
            reason="매칭된 룰 없음",
            amount_policy=amount_policy,
            cash_direction=cash_direction,
            comparability=Comparability.UNKNOWN,
            relations=(),
            provenance=provenance,
        )


def decimal_amount(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(r"[^0-9.+-]", "", text)
    if not text:
        return None
    try:
        amount = Decimal(text)
    except Exception:
        return None
    return -amount if negative else amount
