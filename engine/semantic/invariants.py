from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping


@dataclass(frozen=True)
class InvariantEvidence:
    invariant_id: str
    status: str
    left_value: Decimal | None
    right_value: Decimal | None
    residual: Decimal | None
    relative_residual: float | None
    involved_canonical_ids: tuple[str, ...]
    candidate_only: bool = True
    reason: str = ""
    eligibility_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvariantContext:
    scope_consistent: bool = True
    period_consistent: bool = True
    currency_consistent: bool = True
    unit_normalized: bool = True
    accounting_basis_consistent: bool = True
    has_dimensional_duplicates: bool = False
    aggregation_complete: bool = False
    qualifiers: tuple[str, ...] = ()


class AccountingInvariantAuditor:
    """Candidate evidence for semantic integrity; never changes a mapping."""

    def __init__(self, *, absolute_tolerance: Decimal = Decimal("1"), relative_tolerance: float = 0.005) -> None:
        self.absolute_tolerance = absolute_tolerance
        self.relative_tolerance = relative_tolerance

    @staticmethod
    def _value(facts: Mapping[str, Decimal | int | float | str | None], key: str) -> Decimal | None:
        value = facts.get(key)
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _eligibility(context: InvariantContext) -> tuple[bool, tuple[str, ...]]:
        checks = (
            f"scope_consistent:{context.scope_consistent}",
            f"period_consistent:{context.period_consistent}",
            f"currency_consistent:{context.currency_consistent}",
            f"unit_normalized:{context.unit_normalized}",
            f"accounting_basis_consistent:{context.accounting_basis_consistent}",
            f"no_dimensional_duplicates:{not context.has_dimensional_duplicates}",
        )
        return all(
            (
                context.scope_consistent,
                context.period_consistent,
                context.currency_consistent,
                context.unit_normalized,
                context.accounting_basis_consistent,
                not context.has_dimensional_duplicates,
            )
        ), checks

    def _compare(
        self,
        invariant_id: str,
        left: Decimal | None,
        right: Decimal | None,
        involved: tuple[str, ...],
        context: InvariantContext,
    ) -> InvariantEvidence:
        eligible, checks = self._eligibility(context)
        if not eligible:
            return InvariantEvidence(
                invariant_id, "NOT_TESTABLE", left, right, None, None, involved,
                reason="semantic_dimensions_are_not_comparable", eligibility_checks=checks,
            )
        if left is None or right is None:
            return InvariantEvidence(
                invariant_id, "NOT_TESTABLE", left, right, None, None, involved,
                reason="required_fact_missing", eligibility_checks=checks,
            )
        residual = left - right
        denominator = max(abs(left), abs(right), Decimal(1))
        relative = float(abs(residual) / denominator)
        status = "PASS" if abs(residual) <= self.absolute_tolerance or relative <= self.relative_tolerance else "REVIEW"
        return InvariantEvidence(
            invariant_id, status, left, right, residual, relative, involved,
            reason="mapping_candidate_evidence_only", eligibility_checks=checks,
        )

    @staticmethod
    def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
        sequence = tuple(values)
        return sum(sequence, Decimal(0)) if sequence and all(value is not None for value in sequence) else None

    def audit(
        self,
        facts: Mapping[str, Decimal | int | float | str | None],
        *,
        context: InvariantContext | None = None,
    ) -> tuple[InvariantEvidence, ...]:
        context = context or InvariantContext()
        assets = self._value(facts, "TOTAL_ASSETS")
        liabilities = self._value(facts, "TOTAL_LIABILITIES")
        equity = self._value(facts, "TOTAL_EQUITY")
        revenue = self._value(facts, "REVENUE")
        cogs = self._value(facts, "COGS")
        gross_profit = self._value(facts, "GROSS_PROFIT")
        begin_cash = self._value(facts, "CF_CASH_BEGIN")
        end_cash = self._value(facts, "CF_CASH_END")
        if end_cash is None:
            end_cash = self._value(facts, "CASH_AND_EQUIVALENTS")
        cfo = self._value(facts, "CFO")
        cfi = self._value(facts, "CFI")
        cff = self._value(facts, "CFF")
        # A missing FX fact is not evidence of a zero FX effect. Requiring the
        # reported fact avoids false REVIEW signals for presentations that omit
        # or aggregate foreign-exchange effects elsewhere.
        fx = self._value(facts, "FX_EFFECT_CASH")
        cash_change_before_fx = self._value(facts, "CF_CASH_CHANGE_BEFORE_FX")
        cash_change = self._value(facts, "CF_CASH_CHANGE")
        current_assets = self._value(facts, "CURRENT_ASSETS")
        noncurrent_assets = self._value(facts, "NONCURRENT_ASSETS")
        current_liabilities = self._value(facts, "CURRENT_LIABILITIES")
        noncurrent_liabilities = self._value(facts, "NONCURRENT_LIABILITIES")
        pbt = self._value(facts, "PBT")
        tax_expense = self._value(facts, "TAX_EXPENSE")
        net_income = self._value(facts, "NET_INCOME")
        evidence = [
            self._compare(
                "BS_ASSETS_EQUALS_LIABILITIES_PLUS_EQUITY",
                assets,
                liabilities + equity if liabilities is not None and equity is not None else None,
                ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"),
                context,
            ),
            self._compare(
                "IS_REVENUE_MINUS_COGS_EQUALS_GROSS_PROFIT",
                revenue - abs(cogs) if revenue is not None and cogs is not None else None,
                gross_profit,
                ("REVENUE", "COGS", "GROSS_PROFIT"),
                context,
            ),
            self._compare(
                "IS_PBT_MINUS_TAX_EQUALS_NET_INCOME",
                pbt - abs(tax_expense) if pbt is not None and tax_expense is not None else None,
                net_income,
                ("PBT", "TAX_EXPENSE", "NET_INCOME"),
                context,
            ),
            self._compare(
                "CF_OPERATING_PLUS_INVESTING_PLUS_FINANCING_EQUALS_CHANGE_BEFORE_FX",
                self._sum((cfo, cfi, cff)),
                cash_change_before_fx,
                ("CFO", "CFI", "CFF", "CF_CASH_CHANGE_BEFORE_FX"),
                context,
            ),
            self._compare(
                "CF_END_MINUS_BEGIN_EQUALS_CASH_CHANGE",
                end_cash - begin_cash if end_cash is not None and begin_cash is not None else None,
                cash_change,
                ("CF_CASH_BEGIN", "CF_CASH_END", "CF_CASH_CHANGE"),
                context,
            ),
            self._compare(
                "CF_BEGIN_PLUS_FLOWS_EQUALS_END",
                begin_cash + cfo + cfi + cff + fx
                if all(value is not None for value in (begin_cash, cfo, cfi, cff, fx))
                else None,
                end_cash,
                ("CF_CASH_BEGIN", "CFO", "CFI", "CFF", "FX_EFFECT_CASH", "CF_CASH_END"),
                context,
            ),
        ]
        # Component-sum equations are only valid when the extraction explicitly
        # proves that the presentation contains a complete, non-overlapping set.
        aggregate_context = InvariantContext(**{**context.__dict__})
        if context.aggregation_complete:
            evidence.extend(
                (
                    self._compare(
                        "BS_CURRENT_PLUS_NONCURRENT_ASSETS_EQUALS_TOTAL",
                        self._sum((current_assets, noncurrent_assets)),
                        assets,
                        ("CURRENT_ASSETS", "NONCURRENT_ASSETS", "TOTAL_ASSETS"),
                        aggregate_context,
                    ),
                    self._compare(
                        "BS_CURRENT_PLUS_NONCURRENT_LIABILITIES_EQUALS_TOTAL",
                        self._sum((current_liabilities, noncurrent_liabilities)),
                        liabilities,
                        ("CURRENT_LIABILITIES", "NONCURRENT_LIABILITIES", "TOTAL_LIABILITIES"),
                        aggregate_context,
                    ),
                )
            )
        return tuple(evidence)

    def counterfactual_residual_improvement(
        self,
        facts: Mapping[str, Decimal | int | float | str | None],
        *,
        replace: Mapping[str, Decimal | int | float | str | None],
        context: InvariantContext | None = None,
    ) -> dict[str, float | int]:
        """Score a proposed mapping; positive evidence still requires review."""

        before = self.audit(facts, context=context)
        after = self.audit({**facts, **replace}, context=context)
        before_residual = sum(item.relative_residual or 0.0 for item in before if item.status != "NOT_TESTABLE")
        after_residual = sum(item.relative_residual or 0.0 for item in after if item.status != "NOT_TESTABLE")
        return {
            "before_relative_residual": before_residual,
            "after_relative_residual": after_residual,
            "residual_improvement": before_residual - after_residual,
            "tested_invariant_count": sum(item.status != "NOT_TESTABLE" for item in after),
        }
