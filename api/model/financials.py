from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class FinancialStatementMetadata:
    stock_code: str
    security_id: str
    stock_name: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


@dataclass(frozen=True)
class FinancialPeriodColumn:
    key: str
    label: str
    fiscal_year: int
    fiscal_month: int
    period_end_date: date


@dataclass(frozen=True)
class FinancialStatementCell:
    period_key: str
    value: float | None
    display_value: str
    growth_rate: float | None = None
    display_growth_rate: str = "N/A"


@dataclass(frozen=True)
class FinancialChartPoint:
    period_key: str
    label: str
    value: float | None


@dataclass(frozen=True)
class FinancialAccountStatistics:
    latest: float | None = None
    maximum: float | None = None
    minimum: float | None = None
    average: float | None = None


@dataclass(frozen=True)
class FinancialAccountRow:
    canonical_id: str
    account_name: str
    statement_type: str
    is_derived: bool = False
    formula: str | None = None
    description: str | None = None
    unit: str | None = None
    currency: str | None = "KRW"
    values: list[FinancialStatementCell] = field(default_factory=list)
    trend: list[FinancialChartPoint] = field(default_factory=list)
    growth_chart: list[FinancialChartPoint] = field(default_factory=list)
    statistics: FinancialAccountStatistics = field(default_factory=FinancialAccountStatistics)


@dataclass(frozen=True)
class FinancialStatementSection:
    statement_type: str
    title: str
    title_en: str
    accounts: list[FinancialAccountRow] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialStatementsResponse:
    stock: FinancialStatementMetadata
    period: str
    statement: str
    columns: list[FinancialPeriodColumn]
    sections: list[FinancialStatementSection]
    source: str = "fact_canonical_statements"


@dataclass(frozen=True)
class FinancialAccountDetailResponse:
    stock: FinancialStatementMetadata
    period: str
    statement_type: str
    account: FinancialAccountRow
    columns: list[FinancialPeriodColumn]
    source: str = "fact_canonical_statements"
