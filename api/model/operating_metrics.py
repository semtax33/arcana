from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class OperatingMetricStock:
    stock_code: str
    security_id: str
    country: str = "KR"
    currency: str = "KRW"


@dataclass(frozen=True)
class OperatingMetricRow:
    fiscal_year: int
    fiscal_month: int
    period_end_date: date | str
    segment_id: str = ""
    segment_name: str = ""
    product_id: str = ""
    product_name: str = ""
    metric_id: str = ""
    metric_name: str = ""
    metric_value: float | None = None
    metric_unit: str = ""
    value_type: str = ""
    source_type: str = ""
    confidence: float | None = None
    quality_flags: str = ""


@dataclass(frozen=True)
class UnitEconomicsRow:
    fiscal_year: int
    fiscal_month: int
    period_end_date: date | str
    segment_id: str = ""
    segment_name: str = ""
    product_id: str = ""
    product_name: str = ""
    revenue: float | None = None
    quantity: float | None = None
    quantity_unit: str = ""
    p: float | None = None
    asp: float | None = None
    c: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None
    revenue_coverage_ratio: float | None = None
    confidence: float | None = None
    quality_flags: str = ""


@dataclass(frozen=True)
class OperatingMetricDriverRow:
    fiscal_year: int
    fiscal_month: int
    period_end_date: date | str
    segment_id: str = ""
    segment_name: str = ""
    product_id: str = ""
    product_name: str = ""
    q_yoy_pct: float | None = None
    asp_yoy_pct: float | None = None
    unit_cost_yoy_pct: float | None = None
    revenue_yoy_pct: float | None = None
    gross_margin_change_pctp: float | None = None


@dataclass(frozen=True)
class OperatingMetricResponse:
    stock: OperatingMetricStock
    as_of_date: date | str
    rows: list[Any] = field(default_factory=list)
    source: str = "gold_csv"
    warnings: list[str] = field(default_factory=list)
