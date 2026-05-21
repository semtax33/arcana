from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class StockIntroductionMetadata:
    stock_code: str
    security_id: str
    stock_name: str | None = None
    stock_name_en: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


@dataclass(frozen=True)
class StockIntroductionMetrics:
    market_cap: float | None = None
    trailing_per: float | None = None
    dividend_yield: float | None = None
    fifty_two_week_range_pct: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    latest_close: float | None = None
    latest_trade_date: date | None = None


@dataclass(frozen=True)
class CompanyIntroduction:
    description: str = ""


@dataclass(frozen=True)
class BusinessAreaBadge:
    sector_code: str
    sector_name: str
    schema: str = "GICS"


@dataclass(frozen=True)
class StockIntroductionResponse:
    stock: StockIntroductionMetadata
    metrics: StockIntroductionMetrics
    company: CompanyIntroduction
    business_areas: list[BusinessAreaBadge] = field(default_factory=list)
    factor_source: str = "fact_daily_factor"
