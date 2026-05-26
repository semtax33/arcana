from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class SectorLeaderMetric:
    value: float | None = None
    display_value: str = "N/A"


@dataclass(frozen=True)
class SectorLeaderRow:
    rank: int
    sector_code: str
    sector_name: str
    stock_count: int = 0
    strong_stock_count: int = 0
    strong_stock_ratio: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    eps_expected_growth: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    return_1d: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    return_1w: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    roe: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    per: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)
    pbr: SectorLeaderMetric = field(default_factory=SectorLeaderMetric)


@dataclass(frozen=True)
class SectorLeaderResponse:
    as_of_date: date
    market: str
    level: str
    sort_by: str
    direction: str
    near_high_pct: float
    financial_basis: str
    factor_source: str
    eps_growth_factor_id: str
    rows: list[SectorLeaderRow] = field(default_factory=list)
