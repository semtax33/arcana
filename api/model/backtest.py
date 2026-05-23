from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class BacktestSummary:
    start_date: date
    end_date: date
    rebalance_frequency: str
    cumulative_return: float | None = None
    cagr: float | None = None
    max_drawdown: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    rebalance_count: int = 0


@dataclass(frozen=True)
class BacktestEquityCurvePoint:
    trade_date: date
    strategy_nav: float
    benchmark_navs: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestPosition:
    security_id: str
    ticker: str | None
    stock_name: str | None
    weight: float
    score: float | None
    factor_values: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestRebalance:
    rebalance_date: date
    signal_date: date
    positions: list[BacktestPosition]


@dataclass(frozen=True)
class BacktestAnnualReturn:
    year: int
    strategy_return: float | None = None
    benchmark_returns: dict[str, float | None] = field(default_factory=dict)
    excess_returns: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorBacktestResult:
    summary: BacktestSummary
    equity_curve: list[BacktestEquityCurvePoint]
    rebalance_history: list[BacktestRebalance]
    annual_returns: list[BacktestAnnualReturn]
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
