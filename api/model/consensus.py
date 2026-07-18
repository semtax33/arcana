from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class RealConsensusReport:
    report_date: date | str
    broker_name: str
    analyst_name: str
    report_title: str
    grade_value: str
    old_grade_value: str
    target_price: float | None = None
    old_target_price: float | None = None
    change_price: float | None = None
    currency: str = "KRW"


@dataclass(frozen=True)
class RealConsensusReportsResponse:
    stock_code: str
    as_of_date: date | str
    average_target_price: float | None = None
    target_price_analyst_count: int = 0
    currency: str = "KRW"
    reports: list[RealConsensusReport] = field(default_factory=list)
    source: str = "real_consensus_reports"
