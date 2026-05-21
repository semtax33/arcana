from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class StockChartPoint:
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


@dataclass(frozen=True)
class RecentStockChartRow:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    monthly_return: float | None = None
    continuity: str | None = None
    volume_signal: str | None = None
    rsi: Any = None
    bollinger_band: Any = None
    trend: Any = None
    macd: Any = None


@dataclass(frozen=True)
class StockChartMetadata:
    stock_code: str
    security_id: str
    stock_name: str | None = None
    country: str | None = "KR"
    currency: str | None = "KRW"


@dataclass(frozen=True)
class StockChartResponse:
    stock: StockChartMetadata
    range: str
    from_date: date | None
    to_date: date
    chart: list[StockChartPoint]
    recent: list[RecentStockChartRow]
    factor_source: str = "fact_daily_factors"
    factor_ids: dict[str, list[str]] = field(default_factory=dict)
