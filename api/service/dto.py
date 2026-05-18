from __future__ import annotations

from datetime import date
from typing import Literal

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
