from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class FactorScreenColumn:
    key: str
    label: str
    column_type: str
    order: int
    factor_id: str | None = None
    factor_name: str | None = None
    unit: str | None = None
    value_direction: str | None = None


@dataclass(frozen=True)
class FactorScreenValue:
    factor_id: str
    factor_name: str
    condition_id: str
    value: float | None
    trade_date: date | str | None
    unit: str | None = None
    value_direction: str | None = None


@dataclass(frozen=True)
class ScreenedStockRow:
    rank: int
    security_id: str
    ticker: str | None = None
    stock_name: str | None = None
    country: str | None = None
    market_cap: float | None = None
    sector_code: str | None = None
    percentile: float | None = None
    matched_condition_count: int = 0
    matched_conditions: list[str] = field(default_factory=list)
    latest_trade_date: date | str | None = None
    factor_values: dict[str, FactorScreenValue] = field(default_factory=dict)
    raw_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorScreenResult:
    total_count: int
    fixed_columns: list[FactorScreenColumn]
    factor_columns: list[FactorScreenColumn]
    rows: list[ScreenedStockRow]
