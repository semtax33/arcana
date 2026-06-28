from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from api.model.operating_metrics import OperatingMetricStock


@dataclass(frozen=True)
class EstimateComponentRow:
    target_period: str
    metric_id: str
    model_id: str
    scenario: str
    estimate_value: float | None = None
    currency: str = "KRW"
    source_actual_period: str = ""
    assumptions_json: str = ""
    confidence: float | None = None
    quality_flags: str = ""
    as_of_date: date | str = ""


@dataclass(frozen=True)
class EstimateConsensusRow:
    target_period: str
    metric_id: str
    scenario: str
    consensus_mean: float | None = None
    consensus_median: float | None = None
    consensus_low: float | None = None
    consensus_high: float | None = None
    model_count: int = 0
    confidence: float | None = None
    dispersion: float | None = None
    currency: str = "KRW"
    as_of_date: date | str = ""


@dataclass(frozen=True)
class EstimateResponse:
    stock: OperatingMetricStock
    as_of_date: date | str
    target_period: str = ""
    rows: list[Any] = field(default_factory=list)
    source: str = "gold_csv"
    warnings: list[str] = field(default_factory=list)
