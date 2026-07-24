from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import json
import math
import random
from typing import Any

import numpy as np

from api.config.clickhouse import get_clickhouse_client
from scripts import optimize_required_factor_lab_model as base


ALLOWED_FACTOR_TYPES = {
    "valuation",
    "quality",
    "growth",
    "risk",
    "technical",
    "shareholder",
}
ALLOWED_UNITS = {
    "ratio",
    "percent",
    "score",
    "times",
    "years",
    "days",
    "flag",
}
ALLOWED_DIRECTIONS = {"HIGHER_BETTER", "LOWER_BETTER"}

BASELINE_WEIGHTS = {
    "current_ratio": 0.08103163360536887,
    "equity_duration_20y": 0.2627097083933391,
    "k_ratio_3y": 0.09214258869443577,
    "rim_upside_potential": 0.08930218174726079,
    "rnd_to_market_cap": 0.1089378023514659,
    "tr_12_1": 0.3658760852081294,
}
MANDATORY = (
    "k_ratio_3y",
    "equity_duration_20y",
    "rim_upside_potential",
)


def _discover_factor_specs(
    client: Any,
    signal_dates: list,
) -> tuple[dict[str, tuple[str, str, str]], dict[str, dict[str, Any]]]:
    rows = client.query(
        """
WITH mandatory AS (
    SELECT trade_date, security_id
    FROM fact_daily_factors
    WHERE trade_date IN {signal_dates:Array(Date)}
        AND (
            (factor_id = 'k_ratio_3y' AND financial_basis = 'annual')
            OR (
                factor_id = 'equity_duration_20y'
                AND financial_basis = 'annual'
            )
            OR (
                factor_id = 'rim_upside_potential'
                AND financial_basis = 'annual'
            )
        )
        AND factor_value IS NOT NULL
        AND isFinite(toFloat64(factor_value))
    GROUP BY trade_date, security_id
    HAVING uniqExact(concat(factor_id, '|', financial_basis)) = 3
),
coverage AS (
    SELECT
        f.factor_id AS factor_id,
        f.financial_basis AS financial_basis,
        f.trade_date AS trade_date,
        uniqExact(f.security_id) AS security_count
    FROM fact_daily_factors AS f
    INNER JOIN mandatory AS m
        ON m.trade_date = f.trade_date
        AND m.security_id = f.security_id
    WHERE f.trade_date IN {signal_dates:Array(Date)}
        AND f.factor_value IS NOT NULL
        AND isFinite(toFloat64(f.factor_value))
    GROUP BY f.factor_id, f.financial_basis, f.trade_date
),
aggregated AS (
    SELECT
        factor_id,
        financial_basis,
        count() AS date_count,
        min(security_count) AS min_security_count,
        avg(security_count) AS average_security_count,
        max(security_count) AS max_security_count
    FROM coverage
    GROUP BY factor_id, financial_basis
)
SELECT
    a.factor_id,
    a.financial_basis,
    a.date_count,
    a.min_security_count,
    a.average_security_count,
    a.max_security_count,
    c.factor_name,
    c.factor_type,
    c.factor_group,
    c.unit,
    c.value_direction
FROM aggregated AS a
INNER JOIN (SELECT * FROM factor_catalog FINAL) AS c
    ON c.factor_id = a.factor_id
WHERE a.date_count = {required_dates:UInt32}
    AND a.min_security_count >= 100
    AND c.is_active
ORDER BY a.factor_id, a.financial_basis
""".strip(),
        parameters={
            "signal_dates": [value.isoformat() for value in signal_dates],
            "required_dates": len(signal_dates),
        },
    ).result_rows

    by_factor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = {
            "factor_id": str(row[0]),
            "financial_basis": str(row[1]),
            "date_count": int(row[2]),
            "min_security_count": int(row[3]),
            "average_security_count": float(row[4]),
            "max_security_count": int(row[5]),
            "factor_name": str(row[6]),
            "factor_type": str(row[7]),
            "factor_group": str(row[8]),
            "unit": str(row[9]),
            "value_direction": str(row[10]),
        }
        if item["factor_type"] not in ALLOWED_FACTOR_TYPES:
            continue
        if item["unit"] not in ALLOWED_UNITS:
            continue
        if item["value_direction"] not in ALLOWED_DIRECTIONS:
            continue
        by_factor[item["factor_id"]].append(item)

    metadata: dict[str, dict[str, Any]] = {}
    specs: dict[str, tuple[str, str, str]] = {}
    for factor_id, options in by_factor.items():
        def priority(item: dict[str, Any]) -> tuple[float, float, int]:
            if item["factor_type"] == "shareholder":
                preferred_basis = item["financial_basis"] == "annual"
            elif item["factor_type"] == "technical":
                preferred_basis = item["financial_basis"] == "ttm"
            else:
                preferred_basis = item["financial_basis"] == "ttm"
            return (
                item["min_security_count"],
                item["average_security_count"],
                int(preferred_basis),
            )

        selected = max(options, key=priority)
        direction = (
            "lower"
            if selected["value_direction"] == "LOWER_BETTER"
            else "higher"
        )
        specs[factor_id] = (
            factor_id,
            selected["financial_basis"],
            direction,
        )
        metadata[factor_id] = selected

    specs["k_ratio_3y"] = ("k_ratio_3y", "annual", "higher")
    specs["equity_duration_20y"] = (
        "equity_duration_20y",
        "annual",
        "lower",
    )
    specs["rim_upside_potential"] = (
        "rim_upside_potential",
        "annual",
        "higher",
    )
    specs["current_ratio"] = ("current_ratio", "ttm", "higher")
    specs["rnd_to_market_cap"] = (
        "rnd_to_market_cap",
        "ttm",
        "higher",
    )
    specs["tr_12_1"] = ("tr_12_1", "ttm", "higher")
    for factor_id, basis in {
        "current_ratio": "ttm",
        "rnd_to_market_cap": "ttm",
        "tr_12_1": "ttm",
    }.items():
        if factor_id in metadata:
            metadata[factor_id]["financial_basis"] = basis
    return specs, metadata


def _result_payload(result: base.CandidateResult) -> dict[str, Any]:
    return {
        "weights": dict(sorted(result.weights.items())),
        "max_positions": result.max_positions,
        "metrics": asdict(result.metrics),
        "train_metrics": (
            asdict(result.train_metrics)
            if result.train_metrics is not None
            else None
        ),
        "test_metrics": (
            asdict(result.test_metrics)
            if result.test_metrics is not None
            else None
        ),
        "annual_returns": result.annual_returns,
        "position_counts": result.position_counts,
    }


def _passes_constraints(result: base.CandidateResult) -> bool:
    return (
        result.metrics.t_stat >= 2.0
        and result.metrics.sharpe >= 1.0
        and result.metrics.max_drawdown >= -0.35
    )


def _blend(
    baseline: dict[str, float],
    factor: str,
    factor_weight: float,
) -> dict[str, float]:
    weights = {
        name: value * (1.0 - factor_weight)
        for name, value in baseline.items()
    }
    weights[factor] = weights.get(factor, 0.0) + factor_weight
    total = sum(weights.values())
    return {name: value / total for name, value in weights.items()}


def _screen_incremental_factors(
    *,
    normalized,
    price_returns,
    trading_days,
    rebalance_dates,
    signal_dates_by_rebalance,
    candidate_factors: list[str],
) -> list[dict[str, Any]]:
    screened: list[dict[str, Any]] = []
    for factor in candidate_factors:
        best: base.CandidateResult | None = None
        for factor_weight in [0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50]:
            weights = _blend(BASELINE_WEIGHTS, factor, factor_weight)
            for max_positions in [10, 15, 20, 30]:
                result = base._evaluate(
                    normalized,
                    price_returns,
                    trading_days,
                    rebalance_dates,
                    signal_dates_by_rebalance,
                    weights,
                    max_positions,
                )
                if result is None:
                    continue
                if best is None or (
                    result.metrics.cumulative_return,
                    result.metrics.max_drawdown,
                ) > (
                    best.metrics.cumulative_return,
                    best.metrics.max_drawdown,
                ):
                    best = result
        if best is not None:
            screened.append(
                {
                    "factor": factor,
                    "passes_constraints": _passes_constraints(best),
                    "best": best,
                }
            )
    screened.sort(
        key=lambda item: (
            item["passes_constraints"],
            item["best"].metrics.cumulative_return,
            item["best"].metrics.max_drawdown,
        ),
        reverse=True,
    )
    return screened


def _perturbed_baseline_weights(
    rng: random.Random,
    new_factors: list[str],
) -> dict[str, float]:
    baseline_mass = rng.uniform(0.45, 0.88)
    baseline_names = list(BASELINE_WEIGHTS)
    baseline_draws = np.array(
        [
            BASELINE_WEIGHTS[name]
            * rng.lognormvariate(0.0, 0.55)
            for name in baseline_names
        ],
        dtype=float,
    )
    floors = np.array(
        [
            0.025 if name in MANDATORY else 0.01
            for name in baseline_names
        ],
        dtype=float,
    )
    floor_total = float(floors.sum())
    if baseline_mass <= floor_total:
        baseline_mass = floor_total + 0.05
    baseline_draws = baseline_draws / baseline_draws.sum()
    baseline_weights = floors + (
        baseline_mass - floor_total
    ) * baseline_draws

    weights = dict(
        zip(baseline_names, baseline_weights.tolist(), strict=True)
    )
    new_mass = 1.0 - baseline_mass
    new_draws = np.array(
        [rng.gammavariate(1.2, 1.0) for _ in new_factors],
        dtype=float,
    )
    new_draws = new_draws / new_draws.sum()
    for factor, value in zip(new_factors, new_draws, strict=True):
        weights[factor] = weights.get(factor, 0.0) + new_mass * float(value)
    total = sum(weights.values())
    return {name: value / total for name, value in weights.items()}


def _optimize_combinations(
    *,
    rng: random.Random,
    trials: int,
    factor_pool: list[str],
    normalized,
    price_returns,
    trading_days,
    rebalance_dates,
    signal_dates_by_rebalance,
) -> list[base.CandidateResult]:
    results: list[base.CandidateResult] = []
    for _trial in range(trials):
        new_count = rng.randint(1, min(6, len(factor_pool)))
        new_factors = rng.sample(factor_pool, k=new_count)
        weights = _perturbed_baseline_weights(rng, new_factors)
        max_positions = rng.choice([10, 12, 15, 20, 25, 30])
        result = base._evaluate(
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
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--pool-size", type=int, default=35)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    client = get_clickhouse_client()
    try:
        (
            trading_days,
            rebalance_dates,
            signal_dates_by_rebalance,
        ) = base._load_schedule(client)
        specs, metadata = _discover_factor_specs(
            client,
            sorted(signal_dates_by_rebalance.values()),
        )
        base.FACTOR_SPECS = specs
        base.MANDATORY_FACTORS = MANDATORY
        raw = base._load_factor_frame(
            client,
            sorted(signal_dates_by_rebalance.values()),
        )
        normalized = base._normalize_factor_frame(raw)
        required_rows = normalized.dropna(subset=list(MANDATORY))
        security_ids = sorted(
            required_rows.index.get_level_values("security_id")
            .unique()
            .astype(str)
        )
        price_returns = base._load_close_prices(
            client,
            security_ids,
            base.START_DATE,
            base.END_DATE,
        )
    finally:
        client.close()

    available_factors = sorted(set(normalized.columns))
    candidate_factors = [
        factor
        for factor in available_factors
        if factor not in BASELINE_WEIGHTS
    ]
    baseline = base._evaluate(
        normalized,
        price_returns,
        trading_days,
        rebalance_dates,
        signal_dates_by_rebalance,
        BASELINE_WEIGHTS,
        10,
    )
    if baseline is None:
        raise RuntimeError("baseline model could not be evaluated")

    screened = _screen_incremental_factors(
        normalized=normalized,
        price_returns=price_returns,
        trading_days=trading_days,
        rebalance_dates=rebalance_dates,
        signal_dates_by_rebalance=signal_dates_by_rebalance,
        candidate_factors=candidate_factors,
    )
    factor_pool = [
        item["factor"]
        for item in screened[: args.pool_size]
    ]

    rng = random.Random(args.seed)
    optimized = _optimize_combinations(
        rng=rng,
        trials=args.trials,
        factor_pool=factor_pool,
        normalized=normalized,
        price_returns=price_returns,
        trading_days=trading_days,
        rebalance_dates=rebalance_dates,
        signal_dates_by_rebalance=signal_dates_by_rebalance,
    )
    eligible = [result for result in optimized if _passes_constraints(result)]
    eligible.sort(
        key=lambda result: (
            result.metrics.cumulative_return,
            result.metrics.max_drawdown,
            result.test_metrics.sharpe
            if result.test_metrics is not None
            else -math.inf,
        ),
        reverse=True,
    )
    highest_unconstrained = sorted(
        optimized,
        key=lambda result: (
            result.metrics.cumulative_return,
            result.metrics.max_drawdown,
        ),
        reverse=True,
    )

    reported = eligible[: args.top]
    used_factors = sorted(
        {
            factor
            for result in reported
            for factor in result.weights
        }
    )
    payload = {
        "settings": {
            "start_date": base.START_DATE.isoformat(),
            "end_date": base.END_DATE.isoformat(),
            "seed": args.seed,
            "combination_trials": args.trials,
            "catalog_candidates": len(specs),
            "incremental_candidates": len(candidate_factors),
            "factor_pool_size": len(factor_pool),
            "eligible_count": len(eligible),
        },
        "used_factor_specs": {
            factor: {
                "financial_basis": specs[factor][1],
                "direction": specs[factor][2],
            }
            for factor in used_factors
        },
        "eligible": [
            _result_payload(result)
            for result in reported
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
