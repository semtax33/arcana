from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

import pandas as pd

from .factor_graph import FactorDependencyGraph


class MissingFactCause(str, Enum):
    CONCEPT_NOT_MAPPED = "concept_not_mapped"
    CONCEPT_NOT_REPORTED = "concept_not_reported"
    SCOPE_MISSING = "scope_missing"
    PERIOD_MISSING = "period_missing"
    STATEMENT_INCOMPLETE = "statement_incomplete"
    SOURCE_NOT_INGESTED = "source_not_ingested"
    HISTORICAL_DIALECT_GAP = "historical_dialect_gap"
    TRANSFORMATION_GAP = "transformation_gap"


def classify_missing_fact(
    *,
    source_ingested: bool = True,
    scope_known: bool = True,
    period_known: bool = True,
    dialect_supported: bool = True,
    raw_candidate_present: bool = False,
    statement_complete: bool = True,
    canonical_present: bool = False,
    transformation_available: bool = True,
) -> MissingFactCause:
    """Assign exactly one loss reason using a precision-first precedence."""

    if not source_ingested:
        return MissingFactCause.SOURCE_NOT_INGESTED
    if not scope_known:
        return MissingFactCause.SCOPE_MISSING
    if not period_known:
        return MissingFactCause.PERIOD_MISSING
    if not dialect_supported:
        return MissingFactCause.HISTORICAL_DIALECT_GAP
    if canonical_present and not transformation_available:
        return MissingFactCause.TRANSFORMATION_GAP
    if raw_candidate_present and not canonical_present:
        return MissingFactCause.CONCEPT_NOT_MAPPED
    if not statement_complete:
        return MissingFactCause.STATEMENT_INCOMPLETE
    return MissingFactCause.CONCEPT_NOT_REPORTED


STATEMENT_CORE_FACTS: Mapping[str, frozenset[str]] = {
    "BS": frozenset({"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"}),
    "IS": frozenset({"REVENUE", "OPERATING_INCOME", "NET_INCOME"}),
    "CF": frozenset({"CFO", "CFI", "CFF"}),
}

INVARIANT_REQUIRED_FACTS: Mapping[str, tuple[frozenset[str], ...]] = {
    "BS_ASSETS_EQUALS_LIABILITIES_PLUS_EQUITY": (
        frozenset({"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"}),
    ),
    "IS_REVENUE_MINUS_COGS_EQUALS_GROSS_PROFIT": (
        frozenset({"REVENUE", "COGS", "GROSS_PROFIT"}),
    ),
    "IS_PBT_MINUS_TAX_EQUALS_NET_INCOME": (
        frozenset({"PBT", "TAX_EXPENSE", "NET_INCOME"}),
    ),
    "CF_OPERATING_PLUS_INVESTING_PLUS_FINANCING_EQUALS_CHANGE_BEFORE_FX": (
        frozenset({"CFO", "CFI", "CFF", "CF_CASH_CHANGE_BEFORE_FX"}),
    ),
    "CF_END_MINUS_BEGIN_EQUALS_CASH_CHANGE": (
        frozenset({"CF_CASH_BEGIN", "CF_CASH_END", "CF_CASH_CHANGE"}),
        frozenset({"CF_CASH_BEGIN", "CASH_AND_EQUIVALENTS", "CF_CASH_CHANGE"}),
    ),
    "CF_BEGIN_PLUS_FLOWS_EQUALS_END": (
        frozenset({"CF_CASH_BEGIN", "CFO", "CFI", "CFF", "FX_EFFECT_CASH", "CF_CASH_END"}),
        frozenset(
            {"CF_CASH_BEGIN", "CFO", "CFI", "CFF", "FX_EFFECT_CASH", "CASH_AND_EQUIVALENTS"}
        ),
    ),
}


@dataclass(frozen=True)
class CompanyYearCompletenessAuditor:
    factor_graph: FactorDependencyGraph | None = None

    def assess(
        self,
        company_id: str,
        fiscal_year: int,
        rows: pd.DataFrame | Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        statement_values = (
            frame.get("statement_type", pd.Series(dtype=str))
            .fillna("")
            .astype(str)
            .str.upper()
        )
        statement_values = statement_values.replace({"CIS": "IS"})
        concepts = {
            str(value).strip()
            for value in frame.get("canonical_account_id", pd.Series(dtype=str)).dropna()
            if str(value).strip() and str(value).strip() != "UNMAPPED"
        }
        presence = {
            statement: bool(statement_values.eq(statement).any())
            for statement in STATEMENT_CORE_FACTS
        }
        statement_core_complete = {
            statement: required.issubset(concepts)
            for statement, required in STATEMENT_CORE_FACTS.items()
        }
        core_union = frozenset().union(*STATEMENT_CORE_FACTS.values())
        invariant_testable = [
            invariant_id
            for invariant_id, alternatives in INVARIANT_REQUIRED_FACTS.items()
            if any(required.issubset(concepts) for required in alternatives)
        ]
        dependency = (
            self.factor_graph.dependency_coverage(concepts)
            if self.factor_graph is not None
            else {
                "covered_factor_count": 0,
                "financial_dependency_factor_count": 0,
                "factor_input_coverage_pct": 0.0,
            }
        )
        missing_core = sorted(core_union - concepts)
        return {
            "company_id": str(company_id),
            "fiscal_year": int(fiscal_year),
            "statement_presence": presence,
            "statement_core_complete": statement_core_complete,
            "core_fact_present_count": len(core_union & concepts),
            "core_fact_expected_count": len(core_union),
            "core_fact_coverage_pct": 100.0 * len(core_union & concepts) / len(core_union),
            "missing_core_facts": missing_core,
            "factor_ready_count": int(dependency["covered_factor_count"]),
            "factor_count": int(dependency["financial_dependency_factor_count"]),
            "factor_ready_pct": float(dependency["factor_input_coverage_pct"]),
            "invariant_testable_count": len(invariant_testable),
            "invariant_count": len(INVARIANT_REQUIRED_FACTS),
            "invariant_testable_ids": invariant_testable,
            "company_year_complete": all(presence.values())
            and all(statement_core_complete.values()),
        }


def summarize_company_year_completeness(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    records = [dict(row) for row in rows]
    presence = Counter()
    core_complete = Counter()
    for row in records:
        for statement, value in row.get("statement_presence", {}).items():
            presence[statement] += int(bool(value))
        for statement, value in row.get("statement_core_complete", {}).items():
            core_complete[statement] += int(bool(value))
    count = len(records)
    return {
        "company_year_count": count,
        "complete_company_year_count": sum(
            bool(row.get("company_year_complete")) for row in records
        ),
        "statement_presence_counts": dict(sorted(presence.items())),
        "statement_core_complete_counts": dict(sorted(core_complete.items())),
        "average_core_fact_coverage_pct": (
            sum(float(row.get("core_fact_coverage_pct", 0.0)) for row in records) / count
            if count
            else 0.0
        ),
        "average_factor_ready_pct": (
            sum(float(row.get("factor_ready_pct", 0.0)) for row in records) / count
            if count
            else 0.0
        ),
        "invariant_testable_company_year_count": sum(
            int(row.get("invariant_testable_count", 0)) > 0 for row in records
        ),
    }
