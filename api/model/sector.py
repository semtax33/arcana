from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sector:
    sector_code: str
    sector_name: str
    stock_count: int = 0

