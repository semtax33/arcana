"""Shared, currency-local investment universe contract."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


EXCHANGES = {
    "KR": {"KOSPI", "KOSDAQ"},
    "US": {"NASDAQ", "NYSE", "NYSE_AMERICAN", "OTHER"},
}


class SizePercentileDto(BaseModel):
    side: Literal["top", "bottom"]
    percent: float = Field(gt=0, le=100, allow_inf_nan=False)


class UniverseFiltersDto(BaseModel):
    exchange_codes: list[Literal["KOSPI", "KOSDAQ", "NASDAQ", "NYSE", "NYSE_AMERICAN", "OTHER"]] = Field(default_factory=list)
    market_cap_min_mil: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    market_cap_max_mil: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    size_percentile: SizePercentileDto | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.market_cap_min_mil is not None and self.market_cap_max_mil is not None:
            if self.market_cap_min_mil > self.market_cap_max_mil:
                raise ValueError("시가총액 최소값은 최대값보다 클 수 없습니다.")
        self.exchange_codes = sorted(set(self.exchange_codes))
        return self


def normalize_universe(value=None, market: str | None = None) -> dict:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    filters = UniverseFiltersDto(**(value or {})).model_dump()
    active = any(v is not None and v != [] for v in filters.values())
    country = str(market or "").upper()
    if active and country not in EXCHANGES:
        raise ValueError("투자 대상 필터에는 KR 또는 US 국가 선택이 필요합니다.")
    if active and not set(filters["exchange_codes"]).issubset(EXCHANGES[country]):
        raise ValueError("선택한 국가와 거래소가 일치하지 않습니다.")
    return filters


def has_universe_filters(value=None) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    value = value or {}
    return bool(value.get("exchange_codes") or value.get("size_percentile") or
                value.get("market_cap_min_mil") is not None or value.get("market_cap_max_mil") is not None)


def has_size_filters(value: dict) -> bool:
    return bool(value.get("size_percentile") or value.get("market_cap_min_mil") is not None or
                value.get("market_cap_max_mil") is not None)
