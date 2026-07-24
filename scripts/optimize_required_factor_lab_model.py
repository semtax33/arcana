from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
import random
from typing import Any

import numpy as np
import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.service.backtest_service import (
    BacktestService,
    _previous_trading_day,
    _rebalance_dates,
    _segment_end_date,
)


START_DATE = date(2022, 1, 1)
END_DATE = date(2026, 7, 22)
TRANSACTION_COST_BPS = 20.0
TOP_PERCENT = 20.0

# key: (factor_id, financial_basis, direction)
FACTOR_SPECS: dict[str, tuple[str, str, str]] = {
    "k_ratio_3y": ("k_ratio_3y", "annual", "higher"),
    "equity_duration_20y": ("equity_duration_20y", "annual", "lower"),
    "rim_upside_potential": ("rim_upside_potential", "annual", "higher"),
    "psr": ("psr", "ttm", "lower"),
    "pbr": ("pbr", "ttm", "lower"),
    "tr_12_1": ("tr_12_1", "ttm", "higher"),
    "tr_6_1": ("tr_6_1", "ttm", "higher"),
    "high52w_gap_pct": ("high52w_gap_pct", "ttm", "higher"),
    "mdd1yr_12_1_pct": ("mdd1yr_12_1_pct", "ttm", "lower"),
    "vol_12_1_ann": ("vol_12_1_ann", "ttm", "lower"),
    "rnd_to_sales": ("rnd_to_sales", "ttm", "higher"),
    "rnd_to_market_cap": ("rnd_to_market_cap", "ttm", "higher"),
    "net_margin": ("net_margin", "ttm", "higher"),
    "sales_growth_1y": ("sales_growth_1y", "ttm", "higher"),
    "sales_growth_3y": ("sales_growth_3y", "ttm", "higher"),
    "cash_to_debt": ("cash_to_debt", "ttm", "higher"),
    "iroe": ("iroe", "ttm", "higher"),
    "operating_margin_growth_1y": (
        "operating_margin_growth_1y",
        "ttm",
        "higher",
    ),
    "f_score": ("f_score", "ttm", "higher"),
    "fcf_yield": ("fcf_yield", "ttm", "higher"),
    "eps_expected_growth": ("eps_expected_growth", "ttm", "higher"),
    "eps_surprise_pct": ("eps_surprise_pct", "ttm", "higher"),
    "current_ratio": ("current_ratio", "ttm", "higher"),
    "debt_ratio": ("debt_ratio", "ttm", "lower"),
    "asset_turnover": ("asset_turnover", "ttm", "higher"),
    "accrual_ratio": ("accrual_ratio", "ttm", "lower"),
    "revenue_surprise_pct": ("revenue_surprise_pct", "ttm", "higher"),
    "dividend_yield": ("dividend_yield", "annual", "higher"),
}

MANDATORY_FACTORS = (
    "k_ratio_3y",
    "equity_duration_20y",
    "rim_upside_potential",
)


@dataclass(frozen=True)
class Metrics:
    cumulative_return: float
    cagr: float
    max_drawdown: float
    volatility: float
    sharpe: float
    t_stat: float
    win_rate: float
    observations: int


@dataclass
class CandidateResult:
    weights: dict[str, float]
    max_positions: int
    metrics: Metrics
    train_metrics: Metrics | None
    test_metrics: Metrics | None
    annual_returns: dict[int, float]
    position_counts: dict[str, int]


def _load_schedule(client: Any) -> tuple[list[date], list[date], dict[date, date]]:
    service = BacktestService()
    trading_days = service._load_trading_days(
        client,
        START_DATE,
        END_DATE,
        market="KR",
    )
    rebalance_dates = _rebalance_dates(
        trading_days,
        start_date=START_DATE,
        end_date=END_DATE,
        frequency="annual",
    )
    signal_dates = {
        rebalance_date: _previous_trading_day(trading_days, rebalance_date)
        for rebalance_date in rebalance_dates
    }
    if any(value is None for value in signal_dates.values()):
        raise RuntimeError("annual schedule has a rebalance date without a signal date")
    return (
        trading_days,
        rebalance_dates,
        {key: value for key, value in signal_dates.items() if value is not None},
    )


def _load_factor_frame(
    client: Any,
    signal_dates: list[date],
) -> pd.DataFrame:
    pair_keys = [
        f"{factor_id}|{financial_basis}"
        for factor_id, financial_basis, _direction in FACTOR_SPECS.values()
    ]
    rows = client.query(
        """
SELECT
    f.trade_date AS trade_date,
    f.security_id AS security_id,
    concat(f.factor_id, '|', f.financial_basis) AS pair_key,
    argMax(toFloat64(f.factor_value), f.updated_at) AS factor_value
FROM fact_daily_factors AS f
INNER JOIN security_master AS sm
    ON sm.security_id = f.security_id
WHERE sm.country = 'KR'
    AND f.trade_date IN {signal_dates:Array(Date)}
    AND has({pair_keys:Array(String)}, concat(f.factor_id, '|', f.financial_basis))
    AND f.factor_value IS NOT NULL
    AND isFinite(toFloat64(f.factor_value))
GROUP BY f.trade_date, f.security_id, f.factor_id, f.financial_basis
ORDER BY f.trade_date, f.security_id, f.factor_id
""".strip(),
        parameters={
            "signal_dates": [value.isoformat() for value in signal_dates],
            "pair_keys": pair_keys,
        },
    ).result_rows
    pair_to_name = {
        f"{factor_id}|{financial_basis}": name
        for name, (factor_id, financial_basis, _direction) in FACTOR_SPECS.items()
    }
    frame = pd.DataFrame(
        rows,
        columns=["trade_date", "security_id", "pair_key", "factor_value"],
    )
    frame["factor"] = frame["pair_key"].map(pair_to_name)
    wide = frame.pivot_table(
        index=["trade_date", "security_id"],
        columns="factor",
        values="factor_value",
        aggfunc="last",
    ).sort_index()
    return wide


def _normalize_factor_frame(raw: pd.DataFrame) -> pd.DataFrame:
    normalized = pd.DataFrame(index=raw.index, columns=raw.columns, dtype=float)
    for factor in raw.columns:
        direction = FACTOR_SPECS[factor][2]
        for trade_date, values in raw[factor].groupby(level="trade_date"):
            values = values.dropna().astype(float)
            if len(values) < 20:
                continue
            lower = float(values.quantile(0.01))
            upper = float(values.quantile(0.99))
            clipped = values.clip(lower=lower, upper=upper)
            sigma = float(clipped.std(ddof=0))
            if not math.isfinite(sigma) or sigma <= 0:
                continue
            zscore = (clipped - float(clipped.mean())) / sigma
            if direction == "lower":
                zscore = -zscore
            zscore = zscore.clip(lower=-3.0, upper=3.0)
            normalized.loc[zscore.index, factor] = zscore.to_numpy()
    return normalized


def _load_close_prices(
    client: Any,
    security_ids: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    rows = client.query(
        """
SELECT
    p.security_id AS security_id,
    p.trade_date AS trade_date,
    argMax(toFloat64(p.close), p.updated_at) AS close_price
FROM price_daily AS p
WHERE has({security_ids:Array(String)}, p.security_id)
    AND p.trade_date >= {start_date:Date}
    AND p.trade_date <= {end_date:Date}
    AND p.close IS NOT NULL
GROUP BY p.security_id, p.trade_date
ORDER BY p.trade_date, p.security_id
""".strip(),
        parameters={
            "security_ids": security_ids,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
    ).result_rows
    frame = pd.DataFrame(rows, columns=["security_id", "trade_date", "close"])
    close = frame.pivot_table(
        index="trade_date",
        columns="security_id",
        values="close",
        aggfunc="last",
    ).sort_index()
    return close.pct_change(fill_method=None)


def _metrics(returns: pd.Series) -> Metrics:
    clean = (
        returns.astype(float)
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .sort_index()
    )
    if len(clean) < 2:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, len(clean))
    nav = (1.0 + clean).cumprod()
    cumulative_return = float(nav.iloc[-1] - 1.0)
    years = max((clean.index[-1] - clean.index[0]).days / 365.25, 1 / 365.25)
    cagr = float(nav.iloc[-1] ** (1.0 / years) - 1.0)
    drawdown = nav / nav.cummax() - 1.0
    stddev = float(clean.std(ddof=1))
    average = float(clean.mean())
    volatility = stddev * math.sqrt(252.0)
    sharpe = average / stddev * math.sqrt(252.0) if stddev > 0 else 0.0
    t_stat = average / (stddev / math.sqrt(len(clean))) if stddev > 0 else 0.0
    non_zero = clean[clean != 0]
    win_rate = float((non_zero > 0).mean()) if len(non_zero) else 0.0
    return Metrics(
        cumulative_return=cumulative_return,
        cagr=cagr,
        max_drawdown=float(drawdown.min()),
        volatility=volatility,
        sharpe=sharpe,
        t_stat=t_stat,
        win_rate=win_rate,
        observations=len(clean),
    )


def _evaluate(
    normalized: pd.DataFrame,
    price_returns: pd.DataFrame,
    trading_days: list[date],
    rebalance_dates: list[date],
    signal_dates_by_rebalance: dict[date, date],
    weights: dict[str, float],
    max_positions: int,
) -> CandidateResult | None:
    selected_returns: list[pd.Series] = []
    position_counts: dict[str, int] = {}
    factors = list(weights)
    weight_vector = pd.Series(weights, dtype=float)
    for index, rebalance_date in enumerate(rebalance_dates):
        signal_date = signal_dates_by_rebalance[rebalance_date]
        if signal_date not in normalized.index.get_level_values("trade_date"):
            return None
        scores = normalized.xs(signal_date, level="trade_date")[factors].dropna()
        if len(scores) < max_positions:
            return None
        composite = scores.mul(weight_vector, axis=1).sum(axis=1)
        top_n = max(1, math.ceil(len(composite) * TOP_PERCENT / 100.0))
        selected_ids = (
            composite.sort_values(ascending=False, kind="mergesort")
            .head(min(max_positions, top_n))
            .index.astype(str)
            .tolist()
        )
        if not selected_ids:
            return None
        position_counts[signal_date.isoformat()] = len(selected_ids)
        period_end = _segment_end_date(
            trading_days,
            rebalance_dates,
            current_index=index,
            final_end_date=END_DATE,
        )
        available_ids = [
            security_id
            for security_id in selected_ids
            if security_id in price_returns.columns
        ]
        if not available_ids:
            return None
        segment = price_returns.loc[
            (price_returns.index >= rebalance_date)
            & (price_returns.index <= period_end),
            available_ids,
        ]
        daily = segment.mean(axis=1, skipna=True).dropna()
        if daily.empty:
            return None
        daily.iloc[0] -= TRANSACTION_COST_BPS / 10_000.0
        selected_returns.append(daily)
    if not selected_returns:
        return None
    returns = pd.concat(selected_returns).sort_index()
    returns = returns[~returns.index.duplicated(keep="last")]
    train = returns[returns.index < date(2024, 1, 1)]
    test = returns[returns.index >= date(2024, 1, 1)]
    annual_returns: dict[int, float] = {}
    nav = (1.0 + returns).cumprod()
    previous_nav = 1.0
    years = pd.Index([value.year for value in nav.index])
    for year, year_nav in nav.groupby(years):
        annual_returns[int(year)] = float(year_nav.iloc[-1] / previous_nav - 1.0)
        previous_nav = float(year_nav.iloc[-1])
    return CandidateResult(
        weights={key: float(value) for key, value in weights.items()},
        max_positions=max_positions,
        metrics=_metrics(returns),
        train_metrics=_metrics(train) if len(train) >= 2 else None,
        test_metrics=_metrics(test) if len(test) >= 2 else None,
        annual_returns=annual_returns,
        position_counts=position_counts,
    )


def _random_weights(
    rng: random.Random,
    available_factors: set[str],
) -> dict[str, float]:
    optional_pool = [
        factor
        for factor in FACTOR_SPECS
        if factor not in MANDATORY_FACTORS and factor in available_factors
    ]
    optional_count = rng.randint(0, 5)
    optional = rng.sample(optional_pool, k=optional_count)
    if not optional:
        draws = np.array([rng.gammavariate(1.5, 1.0) for _ in MANDATORY_FACTORS])
        draws = draws / draws.sum()
        return dict(zip(MANDATORY_FACTORS, draws.tolist(), strict=True))

    mandatory_mass = rng.uniform(0.30, 0.70)
    mandatory_draws = np.array(
        [rng.gammavariate(1.5, 1.0) for _ in MANDATORY_FACTORS]
    )
    mandatory_draws = mandatory_draws / mandatory_draws.sum()
    mandatory_weights = 0.05 + (mandatory_mass - 0.15) * mandatory_draws

    optional_draws = np.array(
        [rng.gammavariate(1.2, 1.0) for _ in optional]
    )
    optional_draws = optional_draws / optional_draws.sum()
    optional_weights = (1.0 - mandatory_mass) * optional_draws
    weights = dict(
        zip(MANDATORY_FACTORS, mandatory_weights.tolist(), strict=True)
    )
    weights.update(dict(zip(optional, optional_weights.tolist(), strict=True)))
    total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


def _result_payload(result: CandidateResult) -> dict[str, Any]:
    return {
        "weights": dict(sorted(result.weights.items())),
        "max_positions": result.max_positions,
        "metrics": asdict(result.metrics),
        "train_metrics": (
            asdict(result.train_metrics) if result.train_metrics is not None else None
        ),
        "test_metrics": (
            asdict(result.test_metrics) if result.test_metrics is not None else None
        ),
        "annual_returns": result.annual_returns,
        "position_counts": result.position_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    client = get_clickhouse_client()
    try:
        trading_days, rebalance_dates, signal_dates_by_rebalance = _load_schedule(
            client
        )
        raw = _load_factor_frame(
            client,
            sorted(signal_dates_by_rebalance.values()),
        )
        normalized = _normalize_factor_frame(raw)
        required_rows = normalized.dropna(subset=list(MANDATORY_FACTORS))
        security_ids = sorted(
            required_rows.index.get_level_values("security_id").unique().astype(str)
        )
        price_returns = _load_close_prices(
            client,
            security_ids,
            START_DATE,
            END_DATE,
        )
    finally:
        client.close()

    rng = random.Random(args.seed)
    results: list[CandidateResult] = []
    baseline_weights = {factor: 1.0 / 3.0 for factor in MANDATORY_FACTORS}
    for max_positions in [10, 15, 20, 30, 40, 50]:
        baseline = _evaluate(
            normalized,
            price_returns,
            trading_days,
            rebalance_dates,
            signal_dates_by_rebalance,
            baseline_weights,
            max_positions,
        )
        if baseline is not None:
            results.append(baseline)

    for _trial in range(args.trials):
        weights = _random_weights(rng, set(normalized.columns))
        max_positions = rng.choice([10, 15, 20, 25, 30, 40, 50])
        result = _evaluate(
            normalized,
            price_returns,
            trading_days,
            rebalance_dates,
            signal_dates_by_rebalance,
            weights,
            max_positions,
        )
        if result is not None:
            results.append(result)

    eligible = [
        result
        for result in results
        if result.metrics.t_stat >= 2.0
        and result.metrics.sharpe >= 1.0
        and result.metrics.max_drawdown >= -0.35
    ]
    eligible.sort(
        key=lambda result: (
            result.metrics.cumulative_return,
            result.metrics.max_drawdown,
            result.test_metrics.sharpe if result.test_metrics else -math.inf,
        ),
        reverse=True,
    )
    all_ranked = sorted(
        results,
        key=lambda result: (
            result.metrics.cumulative_return,
            result.metrics.max_drawdown,
        ),
        reverse=True,
    )
    coverage = {
        signal_date.isoformat(): int(
            len(
                normalized.xs(signal_date, level="trade_date")
                .dropna(subset=list(MANDATORY_FACTORS))
            )
        )
        for signal_date in sorted(signal_dates_by_rebalance.values())
    }
    payload = {
        "settings": {
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "rebalance_frequency": "annual",
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "top_percent": TOP_PERCENT,
            "seed": args.seed,
            "trials": args.trials,
            "signal_dates": [
                value.isoformat()
                for value in sorted(signal_dates_by_rebalance.values())
            ],
        },
        "mandatory_coverage": coverage,
        "eligible_count": len(eligible),
        "eligible": [
            _result_payload(result) for result in eligible[: args.top]
        ],
        "highest_return_unconstrained": [
            _result_payload(result) for result in all_ranked[:5]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
