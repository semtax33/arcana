from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Iterable, Mapping

from spacy.lang.xx import MultiLanguage
from spacy.matcher import Matcher
from spacy.tokens import Doc

from .detection import detect_scope
from .matcher import SemanticRuleExecutor
from .models import (
    AccountingRegimeFamily,
    CanonicalFact,
    DocumentDialect,
    DurationView,
    FactIdentity,
    HarmonizedFact,
    PeriodKind,
    PeriodView,
    ReportedFact,
    Scope,
    SemanticAddress,
    SourceLocation,
    StatementType,
)


UNIT_MULTIPLIERS: Mapping[str, Decimal] = {
    "원": Decimal(1),
    "KRW": Decimal(1),
    "십원": Decimal(10),
    "백원": Decimal(100),
    "천원": Decimal(1_000),
    "만원": Decimal(10_000),
    "십만원": Decimal(100_000),
    "백만원": Decimal(1_000_000),
    "천만원": Decimal(10_000_000),
    "억원": Decimal(100_000_000),
    "십억원": Decimal(1_000_000_000),
    "조원": Decimal(1_000_000_000_000),
    "USD": Decimal(1),
    "천달러": Decimal(1_000),
    "백만달러": Decimal(1_000_000),
}


class SemanticFieldNormalizer:
    """Normalize non-account semantics with a closed spaCy matcher registry."""

    _STATEMENT_PATTERNS = {
        StatementType.CIS: ("포괄손익계산서", "statementofcomprehensiveincome"),
        StatementType.BS: ("재무상태표", "대차대조표", "balancesheet"),
        StatementType.CF: ("현금흐름표", "cashflowstatement"),
        StatementType.CE: ("자본변동표", "statementofchangesinequity"),
        StatementType.IS: ("손익계산서", "income statement", "statementofincome"),
        StatementType.NOTES: ("주석", "notestothefinancialstatements"),
    }

    def __init__(self) -> None:
        self.nlp = MultiLanguage()
        self.matcher = Matcher(self.nlp.vocab)
        self._statement_by_id: dict[int, StatementType] = {}
        index = 0
        for statement_type, patterns in self._STATEMENT_PATTERNS.items():
            for expression in patterns:
                name = f"STATEMENT_{index}"
                regex = f".*{re.escape(self.normalize_text(expression))}.*"
                self.matcher.add(name, [[{"TEXT": {"REGEX": regex}}]])
                self._statement_by_id[self.nlp.vocab.strings[name]] = statement_type
                index += 1

    @staticmethod
    def normalize_text(value: Any) -> str:
        text = str(value or "").strip().strip('"\'“”‘’')
        text = re.sub(r"\(주(?:석)?\s*[^)]*\)", "", text)
        text = re.sub(r"^\s*(?:\(?\d+\)?[.)．、]?|[Ⅰ-ⅫIVXLCDM]+[.)．、])\s*", "", text)
        text = text.replace("（", "(").replace("）", ")")
        text = re.sub(r"[\s\u3000ㆍ·/\-,，]+", "", text)
        return text.rstrip(".．。").lower()

    def _doc(self, value: Any) -> Doc:
        return Doc(self.nlp.vocab, words=[self.normalize_text(value)], spaces=[False])

    def statement_type(self, value: Any) -> StatementType:
        raw = str(value or "").strip().upper()
        try:
            return StatementType(raw)
        except ValueError:
            pass
        # CIS is registered first and therefore wins over its IS suffix.
        matches = self.matcher(self._doc(value))
        if not matches:
            return StatementType.UNKNOWN
        return self._statement_by_id[matches[0][0]]

    @staticmethod
    def amount(value: Any) -> Decimal | None:
        if value is None:
            return None
        text = str(value).replace("\u3000", "").replace(",", "").strip()
        if text in {"", "-", "－", "—", "–"}:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        if text.startswith(("△", "▲", "-", "－")):
            negative = True
            text = text[1:]
        scalar = re.fullmatch(
            r"(?:₩|￦)?\s*(\d+(?:\.\d+)?)\s*(?:원|KRW)?",
            text.strip(),
            flags=re.IGNORECASE,
        )
        if scalar is None:
            return None
        try:
            result = Decimal(scalar.group(1))
        except (InvalidOperation, ValueError):
            return None
        return -result if negative else result

    @classmethod
    def is_per_share(cls, label: Any) -> bool:
        normalized = cls.normalize_text(label)
        if any(token in normalized for token in ("eps", "earningspershare")):
            return True
        return "주당" in normalized and any(
            token in normalized for token in ("이익", "손익", "손실")
        )

    @staticmethod
    def unit(value: Any) -> tuple[str, Decimal, str]:
        text = re.sub(r"\s+", "", str(value or ""))
        for name in sorted(UNIT_MULTIPLIERS, key=len, reverse=True):
            if name.lower() in text.lower():
                currency = "USD" if "달러" in name or name == "USD" else "KRW"
                return name, UNIT_MULTIPLIERS[name], currency
        currency = "USD" if "usd" in text.lower() or "$" in text else "KRW" if "원" in text else ""
        return "", Decimal(1), currency

    @staticmethod
    def period_semantics(value: Any) -> tuple[PeriodView, DurationView]:
        text = re.sub(r"\s+", "", str(value or "")).lower()
        period_view = PeriodView.UNKNOWN
        if any(token in text for token in ("당기", "당분기", "current")):
            period_view = PeriodView.CURRENT
        elif any(token in text for token in ("전기", "전분기", "comparative", "prior")):
            period_view = PeriodView.COMPARATIVE
        duration = DurationView.UNKNOWN
        if any(token in text for token in ("누적", "ytd")):
            duration = DurationView.YTD
        elif any(token in text for token in ("3개월", "분기", "quarter")):
            duration = DurationView.QUARTER
        elif any(token in text for token in ("연간", "사업연도", "annual", "year")):
            duration = DurationView.ANNUAL
        return period_view, duration


class FinancialSemanticNormalizer:
    def __init__(
        self,
        executor: SemanticRuleExecutor,
        *,
        field_normalizer: SemanticFieldNormalizer | None = None,
    ) -> None:
        self.executor = executor
        self.fields = field_normalizer or SemanticFieldNormalizer()

    def reported_fact_from_row(
        self,
        row: Mapping[str, Any],
        *,
        entity_id: str = "",
        filing_id: str = "",
        revision_id: str = "",
        published_at: datetime | None = None,
        regime: AccountingRegimeFamily = AccountingRegimeFamily.UNKNOWN,
        dialect: DocumentDialect = DocumentDialect.UNKNOWN,
        source_uri: str = "",
    ) -> ReportedFact:
        raw_label = str(row.get("original_account_name", row.get("label", "")) or "")
        has_source_amount = row.get("amount_raw") not in (None, "")
        raw_value = str(
            (
                row.get("amount_raw")
                if has_source_amount
                else row.get("raw_amount", row.get("value", ""))
            )
            or ""
        )
        numeric = self.fields.amount(raw_value)
        unit_text = str(row.get("unit", row.get("table_title", "")) or "")
        unit, multiplier, currency = self.fields.unit(unit_text)
        if row.get("unit_factor") not in (None, ""):
            try:
                multiplier = Decimal(str(row["unit_factor"]))
            except InvalidOperation:
                pass
        # Per-share values are already denominated in won per share even when
        # the surrounding statement table is displayed in thousands/millions.
        if self.fields.is_per_share(raw_label):
            multiplier = Decimal(1)
            currency = currency or "KRW"
        statement_type = self.fields.statement_type(row.get("statement_type", ""))
        period_text = str(row.get("period", "") or "")
        period_end = _period_end(period_text)
        period_view, duration_view = self.fields.period_semantics(
            " ".join(
                str(row.get(key, "") or "")
                for key in ("column_header", "table_title", "period")
            )
        )
        scope = detect_scope(
            " ".join(
                str(row.get(key, "") or "")
                for key in ("table_title", "section_context", "scope")
            )
        )
        identity = FactIdentity(
            entity_id=entity_id or str(row.get("company_name", "") or ""),
            metric=raw_label,
            period_end=period_end,
            scope=scope,
            accounting_regime=regime,
            published_at=published_at,
            filing_id=filing_id,
            revision_id=revision_id,
        )
        address = SemanticAddress(
            section_path=tuple(
                value
                for value in (
                    str(row.get("section_context", "") or ""),
                    str(row.get("table_title", "") or ""),
                )
                if value
            ),
            row_header_path=tuple(
                value
                for value in str(row.get("context_path", "") or "").split(" > ")
                if value
            )
            + (raw_label,),
            column_header_path=tuple(
                value
                for value in str(row.get("column_header", "") or "").split(" > ")
                if value
            ),
            unit=unit,
            currency=currency,
            period_end=period_end,
            period_kind=(
                PeriodKind.INSTANT
                if statement_type == StatementType.BS
                else PeriodKind.DURATION
            ),
            period_view=period_view,
            duration_view=duration_view,
            scope=scope,
        )
        return ReportedFact(
            identity=identity,
            statement_type=statement_type,
            raw_label=raw_label,
            raw_value=raw_value,
            # DART adapter의 raw_amount는 이미 unit_factor가 반영된 값이다.
            numeric_value=(
                numeric * multiplier
                if numeric is not None and has_source_amount
                else numeric
            ),
            normalized_label=self.fields.normalize_text(raw_label),
            unit=unit,
            unit_multiplier=multiplier,
            currency=currency,
            address=address,
            source=SourceLocation(
                source_uri=source_uri,
                document_id=filing_id,
                table_index=_optional_int(row.get("table_index")),
                row_index=_optional_int(row.get("row_index")),
            ),
            document_dialect=dialect,
            attributes=dict(row),
        )

    def normalize(self, reported: ReportedFact) -> CanonicalFact:
        context = " ".join(
            [
                *reported.address.section_path,
                *reported.address.row_header_path[:-1],
            ]
        )
        matched = self.executor.match(
            statement_type=reported.statement_type.value,
            label=reported.raw_label,
            context=context,
            has_children=bool(reported.attributes.get("has_children", False)),
            amount_is_zero_or_blank=reported.numeric_value in (None, Decimal(0)),
            regime=reported.identity.accounting_regime,
            dialect=reported.document_dialect,
            effective_at=reported.identity.period_end,
        )
        raw_value = reported.numeric_value
        value = raw_value
        if value is not None:
            if matched.amount_policy == "abs":
                value = abs(value)
            elif matched.amount_policy == "neg_abs":
                value = -abs(value)
        cash_value = value
        if cash_value is not None:
            if matched.cash_direction == "inflow":
                cash_value = abs(cash_value)
            elif matched.cash_direction == "outflow":
                cash_value = -abs(cash_value)
        identity = replace(reported.identity, metric=matched.canonical_id)
        return CanonicalFact(
            identity=identity,
            canonical_id=matched.canonical_id,
            canonical_name=matched.canonical_name,
            statement_type=reported.statement_type,
            value=value,
            raw_value=raw_value,
            amount_policy=matched.amount_policy,
            cash_direction=matched.cash_direction,
            cash_effect_value=cash_value,
            comparability=matched.comparability,
            reported_fact=reported,
            provenance=matched.provenance,
        )

    def normalize_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        **identity: Any,
    ) -> tuple[CanonicalFact, ...]:
        return tuple(
            self.normalize(self.reported_fact_from_row(row, **identity))
            for row in rows
        )

    @staticmethod
    def harmonize(
        facts: Iterable[CanonicalFact],
        *,
        analytical_metric: str,
        bridge_rule_id: str,
        reducer: str = "last",
    ) -> HarmonizedFact:
        selected = tuple(facts)
        if not selected:
            raise ValueError("at least one canonical fact is required")
        if reducer == "sum":
            values = [fact.value for fact in selected if fact.value is not None]
            value = sum(values, Decimal(0)) if values else None
        elif reducer == "last":
            value = selected[-1].value
        else:
            raise ValueError(f"unsupported deterministic reducer: {reducer}")
        comparability = max(
            (fact.comparability for fact in selected),
            key=lambda item: _comparability_rank(item.value),
        )
        return HarmonizedFact(
            identity=replace(selected[-1].identity, metric=analytical_metric),
            analytical_metric=analytical_metric,
            value=value,
            comparability=comparability,
            canonical_facts=selected,
            bridge_rule_id=bridge_rule_id,
        )


def _period_end(value: str) -> date | None:
    match = re.search(r"(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?", value)
    if not match:
        return None
    year, month, day = (int(part) if part else None for part in match.groups())
    if day is None:
        import calendar

        day = calendar.monthrange(year, month)[1]
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _comparability_rank(value: str) -> int:
    order = {
        "EXACT": 0,
        "PRESENTATION_ONLY_DIFFERENCE": 1,
        "AGGREGATION_DIFFERENCE": 2,
        "MEASUREMENT_DIFFERENCE": 3,
        "DERIVED_BRIDGE": 4,
        "ACCOUNTING_POLICY_BREAK": 5,
        "UNKNOWN": 6,
    }
    return order.get(value, 99)
