from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from api.model.financials import (
    FinancialAccountStatistics,
    FinancialChartPoint,
    FinancialPeriodColumn,
    FinancialStatementCell,
    FinancialStatementMetadata,
)


@dataclass(frozen=True)
class FinancialRatioRow:
    factor_id: str
    factor_name: str
    statement_type: str
    group_key: str
    group_name: str
    unit: str | None = None
    value_direction: str | None = None
    description: str | None = None
    values: list[FinancialStatementCell] = field(default_factory=list)
    trend: list[FinancialChartPoint] = field(default_factory=list)
    growth_chart: list[FinancialChartPoint] = field(default_factory=list)
    statistics: FinancialAccountStatistics = field(default_factory=FinancialAccountStatistics)


@dataclass(frozen=True)
class FinancialRatioGroup:
    group_key: str
    title: str
    title_en: str
    ratios: list[FinancialRatioRow] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialRatioSection:
    statement_type: str
    title: str
    title_en: str
    groups: list[FinancialRatioGroup] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialRatiosResponse:
    stock: FinancialStatementMetadata
    period: str
    financial_basis: str
    columns: list[FinancialPeriodColumn]
    sections: list[FinancialRatioSection]
    source: str = "fact_daily_factor"
    auxiliary_sources: list[str] = field(default_factory=list)
