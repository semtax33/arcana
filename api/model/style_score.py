from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class StyleScoreRow:
    trade_date: date
    rank: int
    security_id: str
    issuer_id: str = ""
    stock_code: str = ""
    company_name: str = ""
    industry_schema: str = ""
    sector_code: str = ""
    industry_group_code: str = ""
    industry_group_name: str = ""
    style_profile: str = "DEFAULT"
    value_score: float | None = None
    quality_score: float | None = None
    growth_score: float | None = None
    momentum_score: float | None = None
    risk_score: float | None = None
    dividend_score: float | None = None
    total_score: float | None = None
    score_confidence: float = 0.0
    available_factor_count: int = 0
    required_factor_count: int = 0
    missing_factor_ids: list[str] = field(default_factory=list)
    invalid_factor_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StyleScoreResponse:
    trade_date: date
    style_profile: str
    total_count: int
    rows: list[StyleScoreRow] = field(default_factory=list)


@dataclass(frozen=True)
class FactorScoreBreakdown:
    factor_id: str
    style_group: str
    factor_direction: int
    raw_factor_value: float | None = None
    winsorized_value: float | None = None
    percentile_score: float | None = None
    robust_z_score: float | None = None
    n_peers: int = 0
    industry_level: str = ""
    industry_code: str = ""
    industry_name: str = ""
    is_valid: bool = True
    invalid_reason: str = ""
    is_winsorized: bool = False
    score_confidence: float = 0.0


@dataclass(frozen=True)
class StyleScoreDetailResponse:
    row: StyleScoreRow | None
    factors: list[FactorScoreBreakdown] = field(default_factory=list)


@dataclass(frozen=True)
class StyleScoreComponent:
    component_key: str
    label: str
    score: float | None = None
    score_confidence: float = 0.0
    available_factor_count: int = 0
    required_factor_count: int = 0
    available_weight: float = 0.0
    required_weight: float = 0.0


@dataclass(frozen=True)
class StyleScoreComponentFactor:
    factor_id: str
    label: str
    style_group: str
    raw_factor_value: float | None = None
    winsorized_value: float | None = None
    percentile_score: float | None = None
    robust_z_score: float | None = None
    factor_weight: float = 0.0
    weighted_score: float | None = None
    n_peers: int = 0
    industry_level: str = ""
    industry_code: str = ""
    industry_name: str = ""
    is_valid: bool = False
    invalid_reason: str = ""
    is_winsorized: bool = False
    score_confidence: float = 0.0


@dataclass(frozen=True)
class StyleScoreComponentsResponse:
    trade_date: date
    security_id: str
    style_profile: str
    stock_code: str = ""
    company_name: str = ""
    components: list[StyleScoreComponent] = field(default_factory=list)


@dataclass(frozen=True)
class StyleScoreComponentDetailResponse:
    trade_date: date
    security_id: str
    style_profile: str
    component: StyleScoreComponent
    stock_code: str = ""
    company_name: str = ""
    factors: list[StyleScoreComponentFactor] = field(default_factory=list)
