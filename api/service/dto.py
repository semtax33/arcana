from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.service.style_score_catalog import DEFAULT_FACTOR_SCREEN_STYLE_PROFILE


ConditionMode = Literal["top_percent", "threshold"]
MatchMode = Literal["all", "any"]
RankDirection = Literal["catalog", "higher", "lower"]
RebalanceFrequency = Literal["quarterly", "semiannual", "annual"]
SectorLeaderSortBy = Literal[
    "strong_stock_ratio",
    "eps_expected_growth",
    "return_1d",
    "return_1w",
    "roe",
    "per",
    "pbr",
]
SectorLeaderLevel = Literal["sector", "industry_group"]
SectorLeaderMarket = Literal["KR", "US"]
SortDirection = Literal["asc", "desc"]
StyleProfile = Literal["DEFAULT", "MINERVINI_ZWEIG", "DIVIDEND_QUALITY"]
StyleScoreComponentKey = Literal[
    "COMPOSITE",
    "VALUE",
    "QUALITY",
    "GROWTH",
    "MOMENTUM",
    "RISK",
    "DIVIDEND",
]


class SectorDto(BaseModel):
    sector_code: str
    sector_name: str
    stock_count: int = 0


class IndustryGroupDto(BaseModel):
    industry_group_code: str
    industry_group_name: str
    sector_code: str
    sector_name: str
    stock_count: int = 0


class SectorLeaderMetricDto(BaseModel):
    value: float | None = None
    display_value: str = "N/A"


class SectorLeaderRowDto(BaseModel):
    rank: int
    sector_code: str
    sector_name: str
    stock_count: int = 0
    strong_stock_count: int = 0
    strong_stock_ratio: SectorLeaderMetricDto
    eps_expected_growth: SectorLeaderMetricDto
    return_1d: SectorLeaderMetricDto
    return_1w: SectorLeaderMetricDto
    roe: SectorLeaderMetricDto
    per: SectorLeaderMetricDto
    pbr: SectorLeaderMetricDto


class SectorLeaderResponseDto(BaseModel):
    as_of_date: date
    market: SectorLeaderMarket = "KR"
    level: SectorLeaderLevel = "industry_group"
    sort_by: SectorLeaderSortBy
    direction: SortDirection
    near_high_pct: float
    financial_basis: str
    factor_source: str
    eps_growth_factor_id: str
    rows: list[SectorLeaderRowDto] = Field(default_factory=list)


class StyleScoreRowDto(BaseModel):
    trade_date: date
    rank: int
    security_id: str
    issuer_id: str = ""
    stock_code: str = ""
    company_name: str = ""
    industry_schema: str = ""
    sector_code: str = ""
    industry_group_code: str = ""
    industry_group_name: str = ""
    style_profile: str = "DEFAULT"
    value_score: float | None = None
    quality_score: float | None = None
    growth_score: float | None = None
    momentum_score: float | None = None
    risk_score: float | None = None
    dividend_score: float | None = None
    total_score: float | None = None
    score_confidence: float = 0.0
    available_factor_count: int = 0
    required_factor_count: int = 0
    missing_factor_ids: list[str] = Field(default_factory=list)
    invalid_factor_ids: list[str] = Field(default_factory=list)


class StyleScoreResponseDto(BaseModel):
    trade_date: date
    style_profile: str
    total_count: int
    rows: list[StyleScoreRowDto] = Field(default_factory=list)


class FactorScoreBreakdownDto(BaseModel):
    factor_id: str
    style_group: str
    factor_direction: int
    raw_factor_value: float | None = None
    winsorized_value: float | None = None
    percentile_score: float | None = None
    robust_z_score: float | None = None
    n_peers: int = 0
    industry_level: str = ""
    industry_code: str = ""
    industry_name: str = ""
    is_valid: bool = True
    invalid_reason: str = ""
    is_winsorized: bool = False
    score_confidence: float = 0.0


class StyleScoreDetailResponseDto(BaseModel):
    row: StyleScoreRowDto | None = None
    factors: list[FactorScoreBreakdownDto] = Field(default_factory=list)


class StyleScoreComponentDto(BaseModel):
    component_key: str
    label: str
    score: float | None = None
    score_confidence: float = 0.0
    available_factor_count: int = 0
    required_factor_count: int = 0
    available_weight: float = 0.0
    required_weight: float = 0.0


class StyleScoreComponentFactorDto(BaseModel):
    factor_id: str
    label: str
    style_group: str
    raw_factor_value: float | None = None
    winsorized_value: float | None = None
    percentile_score: float | None = None
    robust_z_score: float | None = None
    factor_weight: float = 0.0
    weighted_score: float | None = None
    n_peers: int = 0
    industry_level: str = ""
    industry_code: str = ""
    industry_name: str = ""
    is_valid: bool = False
    invalid_reason: str = ""
    is_winsorized: bool = False
    score_confidence: float = 0.0


class StyleScoreComponentsResponseDto(BaseModel):
    trade_date: date
    security_id: str
    stock_code: str = ""
    company_name: str = ""
    style_profile: str
    components: list[StyleScoreComponentDto] = Field(default_factory=list)


class StyleScoreComponentDetailResponseDto(BaseModel):
    trade_date: date
    security_id: str
    stock_code: str = ""
    company_name: str = ""
    style_profile: str
    component: StyleScoreComponentDto
    factors: list[StyleScoreComponentFactorDto] = Field(default_factory=list)


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
    market: str | None = None
    financial_basis: str | None = "annual"
    style_profile: StyleProfile = DEFAULT_FACTOR_SCREEN_STYLE_PROFILE
    sector_codes: list[str] | None = None
    industry_group_codes: list[str] | None = None
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
    industry_group_code: str | None = None
    industry_group_name: str | None = None
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


class ScreenerStrategySummaryDto(BaseModel):
    id: int
    name: str
    created_at: str
    updated_at: str


class ScreenerStrategyDetailDto(ScreenerStrategySummaryDto):
    strategy: FactorScreenRequestDto


class ScreenerStrategyListResponseDto(BaseModel):
    strategies: list[ScreenerStrategySummaryDto] = Field(default_factory=list)


class ScreenerStrategySaveRequestDto(BaseModel):
    name: str
    strategy: FactorScreenRequestDto


class ScreenerStrategyDeleteResponseDto(BaseModel):
    deleted: bool


class FactorBacktestRequestDto(BaseModel):
    conditions: list[FactorConditionDto] = Field(..., min_length=1)
    start_date: date
    end_date: date
    rebalance_frequency: RebalanceFrequency
    financial_basis: str | None = "annual"
    style_profile: StyleProfile = "DEFAULT"
    sector_codes: list[str] | None = None
    industry_group_codes: list[str] | None = None
    match_mode: MatchMode = "all"
    benchmarks: list[str] = Field(default_factory=lambda: ["KOSPI200", "KOSDAQ"])
    max_positions: int | None = Field(default=None, gt=0)
    transaction_cost_bps: float = Field(default=0, ge=0)


class BacktestSummaryDto(BaseModel):
    start_date: date
    end_date: date
    rebalance_frequency: RebalanceFrequency
    cumulative_return: float | None = None
    cagr: float | None = None
    max_drawdown: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    rebalance_count: int


class BacktestEquityCurvePointDto(BaseModel):
    trade_date: date
    strategy_nav: float
    benchmark_navs: dict[str, float | None] = Field(default_factory=dict)


class BacktestPositionDto(BaseModel):
    security_id: str
    ticker: str | None = None
    stock_name: str | None = None
    weight: float
    score: float | None = None
    factor_values: dict[str, float | None] = Field(default_factory=dict)


class BacktestRebalanceDto(BaseModel):
    rebalance_date: date
    signal_date: date
    positions: list[BacktestPositionDto] = Field(default_factory=list)
    entered_positions: list[BacktestPositionDto] = Field(default_factory=list)
    exited_positions: list[BacktestPositionDto] = Field(default_factory=list)


class BacktestAnnualReturnDto(BaseModel):
    year: int
    strategy_return: float | None = None
    benchmark_returns: dict[str, float | None] = Field(default_factory=dict)
    excess_returns: dict[str, float | None] = Field(default_factory=dict)


class FactorBacktestResponseDto(BaseModel):
    summary: BacktestSummaryDto
    equity_curve: list[BacktestEquityCurvePointDto] = Field(default_factory=list)
    rebalance_history: list[BacktestRebalanceDto] = Field(default_factory=list)
    annual_returns: list[BacktestAnnualReturnDto] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
    industry_group_code: str | None = None
    industry_group_name: str | None = None
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


MultipleValuationBandBasis = Literal["blend", "historical", "industry", "market", "listing_market"]


class ValuationStockMetadataDto(BaseModel):
    stock_code: str
    security_id: str
    stock_name: str | None = None
    stock_name_en: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"
    primary_market_mic: str = ""
    industry_schema: str = ""
    sector_code: str = ""
    industry_group_code: str = ""
    industry_group_name: str = ""


class ValuationMetricDto(BaseModel):
    value: float | None = None
    display_value: str = "N/A"


class ValuationBenchmarkComparisonDto(BaseModel):
    benchmark_key: str
    benchmark_name: str
    value: ValuationMetricDto
    difference_pct: float | None = None
    signal: str = "neutral"
    signal_label: str = "Neutral"


class ValuationFactorComparisonDto(BaseModel):
    factor_id: str
    factor_name: str
    unit: str
    direction: str
    current: ValuationMetricDto
    comparisons: list[ValuationBenchmarkComparisonDto] = Field(default_factory=list)


class ValuationHistoryPointDto(BaseModel):
    factor_id: str
    period: date
    value: float | None = None
    display_value: str = "N/A"


class ValuationBandDto(BaseModel):
    factor_id: str
    factor_name: str
    current_multiple: ValuationMetricDto
    target_multiple: ValuationMetricDto
    target_source: str
    fair_price: ValuationMetricDto
    buy_below_price: ValuationMetricDto
    sell_above_price: ValuationMetricDto
    upside_pct: float | None = None
    signal: str = "neutral"
    signal_label: str = "Neutral"
    warning: str | None = None


class ValuationBandSummaryDto(BaseModel):
    fair_price: ValuationMetricDto
    buy_below_price: ValuationMetricDto
    sell_above_price: ValuationMetricDto
    valid_factor_count: int = 0
    excluded_factor_ids: list[str] = Field(default_factory=list)


class MultipleValuationResponseDto(BaseModel):
    stock: ValuationStockMetadataDto
    as_of_date: date
    price_date: date | None = None
    current_price: ValuationMetricDto
    financial_basis: str
    lookback_years: int
    buy_margin_pct: float
    sell_margin_pct: float
    band_basis: str
    factor_source: str
    factor_ids: list[str] = Field(default_factory=list)
    comparisons: list[ValuationFactorComparisonDto] = Field(default_factory=list)
    bands: list[ValuationBandDto] = Field(default_factory=list)
    central_band: ValuationBandSummaryDto | None = None
    history: list[ValuationHistoryPointDto] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
