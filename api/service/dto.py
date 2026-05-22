from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


ConditionMode = Literal["top_percent", "threshold"]
MatchMode = Literal["all", "any"]
RankDirection = Literal["catalog", "higher", "lower"]


class SectorDto(BaseModel):
    sector_code: str
    sector_name: str
    stock_count: int = 0


class FactorDto(BaseModel):
    factor_id: str
    factor_name: str
    factor_type: str
    factor_group: str
    unit: str | None = None
    value_direction: str
    description: str | None = None
    is_active: bool = True


class FactorConditionDto(BaseModel):
    factor_id: str
    mode: ConditionMode
    top_percent: float | None = Field(default=None, gt=0, le=100)
    rank_direction: RankDirection = "catalog"
    operator: str | None = None
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    alias: str | None = None


class FactorScreenRequestDto(BaseModel):
    conditions: list[FactorConditionDto] = Field(..., min_length=1)
    as_of_date: date | None = None
    financial_basis: str | None = "annual"
    sector_codes: list[str] | None = None
    match_mode: MatchMode = "all"
    limit: int | None = Field(default=5000, gt=0, le=5000)


ColumnType = Literal[
    "rank",
    "ticker",
    "name",
    "country",
    "market_cap",
    "factor",
    "percentile",
]


class FactorScreenSummaryDto(BaseModel):
    screening_result: Literal["OK", "EMPTY"]
    total_count: int
    displayed_count: int


class FactorScreenColumnDto(BaseModel):
    key: str
    label: str
    column_type: ColumnType
    order: int
    factor_id: str | None = None
    factor_name: str | None = None
    unit: str | None = None
    value_direction: str | None = None


class FactorScreenValueDto(BaseModel):
    factor_id: str
    factor_name: str
    condition_id: str
    value: float | None = None
    trade_date: date | str | None = None
    unit: str | None = None
    value_direction: str | None = None


class ScreenedStockRowDto(BaseModel):
    rank: int
    security_id: str
    ticker: str | None = None
    stock_name: str | None = None
    country: str | None = None
    market_cap: float | None = None
    sector_code: str | None = None
    percentile: float | None = None
    matched_condition_count: int
    matched_conditions: list[str]
    latest_trade_date: date | str | None = None
    factor_values: dict[str, FactorScreenValueDto] = Field(default_factory=dict)


class FactorScreenResponseDto(BaseModel):
    summary: FactorScreenSummaryDto
    total_count: int
    fixed_columns: list[FactorScreenColumnDto]
    factor_columns: list[FactorScreenColumnDto]
    rows: list[ScreenedStockRowDto]


ChartRange = Literal["1M", "3M", "6M", "1Y", "5Y", "MAX"]
FinancialStatementPeriod = Literal["annual", "quarter", "ttm"]
FinancialStatementFilter = Literal["all", "IS", "BS", "CF"]
FinancialRatioPeriod = Literal["annual", "quarter"]


class StockChartPointDto(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ma5: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma150: float | None = None
    ma200: float | None = None


class RecentStockChartRowDto(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    monthly_return: float | None = None
    continuity: str | None = None
    volume_signal: str | None = None
    rsi: str | float | None = None
    bollinger_band: str | float | dict[str, Any] | None = None
    trend: str | float | None = None
    macd: str | float | dict[str, Any] | None = None


class StockChartMetadataDto(BaseModel):
    stock_code: str
    security_id: str
    stock_name: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


class StockChartResponseDto(BaseModel):
    stock: StockChartMetadataDto
    range: ChartRange
    from_date: date | None = None
    to_date: date
    chart: list[StockChartPointDto]
    recent: list[RecentStockChartRowDto]
    factor_source: str = "fact_daily_factors"
    factor_ids: dict[str, list[str]] = Field(default_factory=dict)


class StockIntroductionMetadataDto(BaseModel):
    stock_code: str
    security_id: str
    stock_name: str | None = None
    stock_name_en: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


class StockIntroductionMetricsDto(BaseModel):
    market_cap: float | None = None
    trailing_per: float | None = None
    dividend_yield: float | None = None
    fifty_two_week_range_pct: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    latest_close: float | None = None
    latest_trade_date: date | None = None


class CompanyIntroductionDto(BaseModel):
    description: str = ""


class BusinessAreaBadgeDto(BaseModel):
    sector_code: str
    sector_name: str
    schema: str = "GICS"


class StockIntroductionResponseDto(BaseModel):
    stock: StockIntroductionMetadataDto
    metrics: StockIntroductionMetricsDto
    company: CompanyIntroductionDto
    business_areas: list[BusinessAreaBadgeDto] = Field(default_factory=list)
    factor_source: str = "fact_daily_factor"


class FinancialStatementMetadataDto(BaseModel):
    stock_code: str
    security_id: str
    stock_name: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


class FinancialPeriodColumnDto(BaseModel):
    key: str
    label: str
    fiscal_year: int
    fiscal_month: int
    period_end_date: date


class FinancialStatementCellDto(BaseModel):
    period_key: str
    value: float | None = None
    display_value: str = "N/A"
    growth_rate: float | None = None
    display_growth_rate: str = "N/A"


class FinancialChartPointDto(BaseModel):
    period_key: str
    label: str
    value: float | None = None


class FinancialAccountStatisticsDto(BaseModel):
    latest: float | None = None
    maximum: float | None = None
    minimum: float | None = None
    average: float | None = None


class FinancialAccountRowDto(BaseModel):
    canonical_id: str
    account_name: str
    statement_type: str
    is_derived: bool = False
    formula: str | None = None
    description: str | None = None
    unit: str | None = None
    currency: str | None = "KRW"
    values: list[FinancialStatementCellDto] = Field(default_factory=list)
    trend: list[FinancialChartPointDto] = Field(default_factory=list)
    growth_chart: list[FinancialChartPointDto] = Field(default_factory=list)
    statistics: FinancialAccountStatisticsDto = Field(default_factory=FinancialAccountStatisticsDto)


class FinancialStatementSectionDto(BaseModel):
    statement_type: str
    title: str
    title_en: str
    accounts: list[FinancialAccountRowDto] = Field(default_factory=list)


class FinancialStatementsResponseDto(BaseModel):
    stock: FinancialStatementMetadataDto
    period: FinancialStatementPeriod
    statement: FinancialStatementFilter
    columns: list[FinancialPeriodColumnDto]
    sections: list[FinancialStatementSectionDto]
    source: str = "fact_canonical_statements"


class FinancialAccountDetailResponseDto(BaseModel):
    stock: FinancialStatementMetadataDto
    period: FinancialStatementPeriod
    statement_type: str
    account: FinancialAccountRowDto
    columns: list[FinancialPeriodColumnDto]
    source: str = "fact_canonical_statements"


class FinancialRatioRowDto(BaseModel):
    factor_id: str
    factor_name: str
    statement_type: str
    group_key: str
    group_name: str
    unit: str | None = None
    value_direction: str | None = None
    description: str | None = None
    values: list[FinancialStatementCellDto] = Field(default_factory=list)
    trend: list[FinancialChartPointDto] = Field(default_factory=list)
    growth_chart: list[FinancialChartPointDto] = Field(default_factory=list)
    statistics: FinancialAccountStatisticsDto = Field(default_factory=FinancialAccountStatisticsDto)


class FinancialRatioGroupDto(BaseModel):
    group_key: str
    title: str
    title_en: str
    ratios: list[FinancialRatioRowDto] = Field(default_factory=list)


class FinancialRatioSectionDto(BaseModel):
    statement_type: str
    title: str
    title_en: str
    groups: list[FinancialRatioGroupDto] = Field(default_factory=list)


class FinancialRatiosResponseDto(BaseModel):
    stock: FinancialStatementMetadataDto
    period: FinancialRatioPeriod
    financial_basis: str
    columns: list[FinancialPeriodColumnDto]
    sections: list[FinancialRatioSectionDto]
    source: str = "fact_daily_factor"
    auxiliary_sources: list[str] = Field(default_factory=list)
