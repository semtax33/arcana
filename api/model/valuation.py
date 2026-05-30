from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class ValuationStockMetadata:
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


@dataclass(frozen=True)
class ValuationMetric:
    value: float | None = None
    display_value: str = "N/A"


@dataclass(frozen=True)
class ValuationBenchmarkComparison:
    benchmark_key: str
    benchmark_name: str
    value: ValuationMetric
    difference_pct: float | None = None
    signal: str = "neutral"
    signal_label: str = "Neutral"


@dataclass(frozen=True)
class ValuationFactorComparison:
    factor_id: str
    factor_name: str
    unit: str
    direction: str
    current: ValuationMetric
    comparisons: list[ValuationBenchmarkComparison] = field(default_factory=list)


@dataclass(frozen=True)
class ValuationHistoryPoint:
    factor_id: str
    period: date
    value: float | None = None
    display_value: str = "N/A"


@dataclass(frozen=True)
class ValuationBand:
    factor_id: str
    factor_name: str
    current_multiple: ValuationMetric
    target_multiple: ValuationMetric
    target_source: str
    fair_price: ValuationMetric
    buy_below_price: ValuationMetric
    sell_above_price: ValuationMetric
    upside_pct: float | None = None
    signal: str = "neutral"
    signal_label: str = "Neutral"
    warning: str | None = None


@dataclass(frozen=True)
class ValuationBandSummary:
    fair_price: ValuationMetric
    buy_below_price: ValuationMetric
    sell_above_price: ValuationMetric
    valid_factor_count: int = 0
    excluded_factor_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MultipleValuationResponse:
    stock: ValuationStockMetadata
    as_of_date: date
    price_date: date | None
    current_price: ValuationMetric
    financial_basis: str
    lookback_years: int
    buy_margin_pct: float
    sell_margin_pct: float
    band_basis: str
    factor_source: str
    factor_ids: list[str] = field(default_factory=list)
    comparisons: list[ValuationFactorComparison] = field(default_factory=list)
    bands: list[ValuationBand] = field(default_factory=list)
    central_band: ValuationBandSummary | None = None
    history: list[ValuationHistoryPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
