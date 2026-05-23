from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sector:
    sector_code: str
    sector_name: str
    stock_count: int = 0


@dataclass(frozen=True)
class IndustryGroup:
    industry_group_code: str
    industry_group_name: str
    sector_code: str
    sector_name: str
    stock_count: int = 0
