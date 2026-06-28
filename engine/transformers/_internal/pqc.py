from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
import math
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PqcForecast:
    target_period: str
    revenue_next: float | None
    q_next: float | None
    asp_next: float | None
    c_next: float | None
    gross_profit_next: float | None
    q_growth_pct: float | None
    asp_growth_pct: float | None
    c_growth_pct: float | None
    revenue_growth_pct: float | None
    confidence: float
    assumptions: dict[str, Any]


def period_end_date(fiscal_year: int, fiscal_month: int) -> date:
    month = int(fiscal_month or 12)
    if month == 12:
        next_month = date(int(fiscal_year) + 1, 1, 1)
    else:
        next_month = date(int(fiscal_year), month + 1, 1)
    return next_month - timedelta(days=1)


def pct_change(current: Any, previous: Any) -> float | None:
    current_float = _float_or_none(current)
    previous_float = _float_or_none(previous)
    if current_float is None or previous_float in (None, 0):
        return None
    return (current_float - previous_float) / abs(previous_float) * 100


def weighted_growth(parts: list[tuple[float, float | None]]) -> float | None:
    clean_parts = [(weight, value) for weight, value in parts if value is not None and math.isfinite(value)]
    if not clean_parts:
        return None
    weight_sum = sum(weight for weight, _ in clean_parts)
    if weight_sum <= 0:
        return None
    return sum(weight * value for weight, value in clean_parts) / weight_sum


def forecast_unit_economics(current: pd.Series, driver: pd.Series | None = None) -> PqcForecast:
    fiscal_year = int(current.get("fiscal_year") or 0)
    fiscal_month = int(current.get("fiscal_month") or 12)
    target_period = f"{fiscal_year + 1}.{fiscal_month:02d}"

    q_growth = _series_float(driver, "q_yoy_pct") if driver is not None else None
    asp_growth = _series_float(driver, "asp_yoy_pct") if driver is not None else None
    c_growth = _series_float(driver, "unit_cost_yoy_pct") if driver is not None else None
    revenue_growth = _series_float(driver, "revenue_yoy_pct") if driver is not None else None

    q_growth = weighted_growth([(0.50, q_growth), (0.50, revenue_growth)])
    asp_growth = weighted_growth([(0.70, asp_growth), (0.30, revenue_growth)])
    c_growth = weighted_growth([(0.60, c_growth), (0.40, asp_growth)])
    pqc_revenue_growth = weighted_growth([(0.50, q_growth), (0.50, asp_growth)])
    blended_revenue_growth = weighted_growth([(0.50, revenue_growth), (0.50, pqc_revenue_growth)])

    revenue = _series_float(current, "revenue")
    quantity = _series_float(current, "quantity")
    asp = _series_float(current, "asp") or _series_float(current, "p")
    unit_cost = _series_float(current, "c")

    q_next = _apply_growth(quantity, q_growth)
    asp_next = _apply_growth(asp, asp_growth)
    c_next = _apply_growth(unit_cost, c_growth)

    if q_next is not None and asp_next is not None:
        revenue_next = q_next * asp_next
    else:
        revenue_next = _apply_growth(revenue, blended_revenue_growth)

    cogs_next = q_next * c_next if q_next is not None and c_next is not None else None
    gross_profit_next = revenue_next - cogs_next if revenue_next is not None and cogs_next is not None else None

    coverage = _series_float(current, "revenue_coverage_ratio")
    source_confidence = _series_float(current, "confidence") or 0.0
    driver_count = sum(value is not None for value in (q_growth, asp_growth, c_growth, revenue_growth))
    confidence = min(0.95, max(0.05, source_confidence * 0.60 + (coverage or 0.0) * 0.25 + driver_count / 4 * 0.15))

    assumptions = {
        "q_growth_pct": q_growth,
        "asp_growth_pct": asp_growth,
        "unit_cost_growth_pct": c_growth,
        "revenue_growth_pct": revenue_growth,
        "pqc_revenue_growth_pct": pqc_revenue_growth,
        "formula": "Q_next*ASP_next when Q and ASP exist; otherwise revenue*(1+blended_growth)",
    }
    return PqcForecast(
        target_period=target_period,
        revenue_next=revenue_next,
        q_next=q_next,
        asp_next=asp_next,
        c_next=c_next,
        gross_profit_next=gross_profit_next,
        q_growth_pct=q_growth,
        asp_growth_pct=asp_growth,
        c_growth_pct=c_growth,
        revenue_growth_pct=blended_revenue_growth,
        confidence=confidence,
        assumptions=assumptions,
    )


def assumptions_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _apply_growth(value: float | None, growth_pct: float | None) -> float | None:
    if value is None:
        return None
    if growth_pct is None:
        return value
    return value * (1 + growth_pct / 100)


def _series_float(row: pd.Series, key: str) -> float | None:
    if key not in row:
        return None
    return _float_or_none(row.get(key))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result
