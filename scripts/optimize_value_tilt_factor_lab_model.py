from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import random
from typing import Any

import numpy as np

from api.config.clickhouse import get_clickhouse_client
from scripts import optimize_expanded_factor_lab_model as expanded
from scripts import optimize_required_factor_lab_model as base


BALANCED_WEIGHTS = {
    "current_ratio": 0.04061512190589691,
    "equity_duration_20y": 0.17600956122698674,
    "k_ratio_3y": 0.0478624442319099,
    "pcr": 0.20436763280265652,
    "real_operating_income_expected_growth": 0.1494483435210717,
    "rim_upside_potential": 0.07186482397475506,
    "rnd_to_market_cap": 0.04371196582518845,
    "tr_12_1": 0.2661201065115347,
}

MANDATORY = (
    "k_ratio_3y",
    "equity_duration_20y",
    "rim_upside_potential",
)

# Only factors whose economic interpretation directly connects price or
# enterprise value to a fundamental variable are treated as "cheapness".
# mcap_mil is catalogued as valuation, but it is a size measure rather than a
# cheapness measure and is intentionally excluded.
VALUE_ALLOWLIST = {
    "bpr",
    "cpr",
    "ebitda_to_ev",
    "economic_profit_yield",
    "epr",
    "ev_to_ebitda",
    "ev_to_nopat",
    "fcf_to_ev_yield",
    "fcf_yield",
    "fcf_yield_dividend_yield_spread",
    "fcfpr",
    "pbr",
    "pcr",
    "per",
    "psr",
    "rim_upside_potential",
    "rnd_to_market_cap",
    "spr",
    "tpr",
}

CORE_VALUE_FACTORS = (
    "pcr",
    "rim_upside_potential",
)

SUPPORT_FACTORS = (
    "current_ratio",
    "real_operating_income_expected_growth",
)


def _allocate_mass(
    rng: random.Random,
    names: list[str],
    mass: float,
    *,
    floor: float,
    priors: dict[str, float] | None = None,
) -> dict[str, float]:
    if not names:
        return {}
    floor_total = floor * len(names)
    if floor_total > mass:
        raise ValueError("allocation floors exceed the requested mass")
    priors = priors or {}
    draws = np.array(
        [
            rng.gammavariate(priors.get(name, 1.2), 1.0)
            for name in names
        ],
        dtype=float,
    )
    draws = draws / draws.sum()
    remaining = mass - floor_total
    return {
        name: floor + remaining * float(draw)
        for name, draw in zip(names, draws, strict=True)
    }


def _draw_weights(
    rng: random.Random,
    value_pool: list[str],
    *,
    momentum_free: bool,
) -> dict[str, float]:
    for _attempt in range(100):
        tr_weight = (
            0.0
            if momentum_free
            else rng.uniform(0.01, 0.08)
        )
        k_weight = rng.uniform(0.025, 0.06)
        duration_weight = rng.uniform(0.055, 0.14)
        support_mass = rng.uniform(0.08, 0.17)
        value_mass = (
            1.0
            - tr_weight
            - k_weight
            - duration_weight
            - support_mass
        )
        if 0.60 <= value_mass <= 0.80:
            break
    else:
        raise RuntimeError("could not draw a feasible value allocation")

    optional_count = rng.randint(1, min(5, len(value_pool)))
    optional = rng.sample(value_pool, k=optional_count)
    value_names = list(CORE_VALUE_FACTORS)
    if (
        "rnd_to_market_cap" in value_pool
        and "rnd_to_market_cap" not in optional
        and rng.random() < 0.60
    ):
        optional.append("rnd_to_market_cap")
    value_names.extend(
        name
        for name in optional
        if name not in value_names
    )
    value_weights = _allocate_mass(
        rng,
        value_names,
        value_mass,
        floor=0.015,
        priors={
            "pcr": 2.0,
            "rim_upside_potential": 1.6,
            "fcf_yield": 1.5,
            "fcf_to_ev_yield": 1.5,
            "economic_profit_yield": 1.4,
            "ebitda_to_ev": 1.4,
        },
    )
    support_weights = _allocate_mass(
        rng,
        list(SUPPORT_FACTORS),
        support_mass,
        floor=0.02,
        priors={
            "real_operating_income_expected_growth": 1.8,
            "current_ratio": 1.0,
        },
    )

    weights = {
        "k_ratio_3y": k_weight,
        "equity_duration_20y": duration_weight,
        **support_weights,
        **value_weights,
    }
    if tr_weight > 0:
        weights["tr_12_1"] = tr_weight
    total = sum(weights.values())
    return {
        name: value / total
        for name, value in weights.items()
    }


def _value_mass(weights: dict[str, float]) -> float:
    return sum(
        weight
        for factor, weight in weights.items()
        if factor in VALUE_ALLOWLIST
    )


def _technical_mass(weights: dict[str, float]) -> float:
    return (
        weights.get("k_ratio_3y", 0.0)
        + weights.get("tr_12_1", 0.0)
    )


def _result_payload(result: base.CandidateResult) -> dict[str, Any]:
    return {
        "weights": dict(sorted(result.weights.items())),
        "value_mass": _value_mass(result.weights),
        "technical_mass": _technical_mass(result.weights),
        "momentum_mass": result.weights.get("tr_12_1", 0.0),
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


def _strictly_preserves(
    result: base.CandidateResult,
    baseline: base.CandidateResult,
) -> bool:
    return (
        result.metrics.cumulative_return
        >= baseline.metrics.cumulative_return
        and result.metrics.max_drawdown
        >= baseline.metrics.max_drawdown
        and result.metrics.sharpe >= baseline.metrics.sharpe
        and result.metrics.t_stat >= baseline.metrics.t_stat
    )


def _practically_preserves(
    result: base.CandidateResult,
    baseline: base.CandidateResult,
) -> bool:
    return (
        result.metrics.cumulative_return
        >= baseline.metrics.cumulative_return * 0.95
        and result.metrics.max_drawdown
        >= baseline.metrics.max_drawdown - 0.02
        and result.metrics.sharpe
        >= max(1.0, baseline.metrics.sharpe * 0.97)
        and result.metrics.t_stat >= 2.0
    )


def _rank_key(
    result: base.CandidateResult,
    baseline: base.CandidateResult,
) -> tuple[float, ...]:
    return (
        float(_strictly_preserves(result, baseline)),
        float(_practically_preserves(result, baseline)),
        result.metrics.cumulative_return,
        result.metrics.max_drawdown,
        result.metrics.sharpe,
        _value_mass(result.weights),
    )


def _evaluate_trials(
    *,
    rng: random.Random,
    trials: int,
    momentum_free: bool,
    value_pool: list[str],
    normalized,
    price_returns,
    trading_days,
    rebalance_dates,
    signal_dates_by_rebalance,
) -> list[base.CandidateResult]:
    results: list[base.CandidateResult] = []
    for _trial in range(trials):
        weights = _draw_weights(
            rng,
            value_pool,
            momentum_free=momentum_free,
        )
        result = base._evaluate(
            normalized,
            price_returns,
            trading_days,
            rebalance_dates,
            signal_dates_by_rebalance,
            weights,
            max_positions=10,
        )
        if result is not None:
            results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    client = get_clickhouse_client()
    try:
        (
            trading_days,
            rebalance_dates,
            signal_dates_by_rebalance,
        ) = base._load_schedule(client)
        specs, metadata = expanded._discover_factor_specs(
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

    available = set(normalized.columns)
    missing_support = set(SUPPORT_FACTORS) - available
    if missing_support:
        raise RuntimeError(
            f"support factors unavailable: {sorted(missing_support)}"
        )
    value_pool = sorted(
        (
            VALUE_ALLOWLIST
            - set(CORE_VALUE_FACTORS)
        )
        & available
    )
    baseline = base._evaluate(
        normalized,
        price_returns,
        trading_days,
        rebalance_dates,
        signal_dates_by_rebalance,
        BALANCED_WEIGHTS,
        max_positions=10,
    )
    if baseline is None:
        raise RuntimeError("balanced baseline could not be evaluated")

    rng = random.Random(args.seed)
    low_momentum = _evaluate_trials(
        rng=rng,
        trials=args.trials,
        momentum_free=False,
        value_pool=value_pool,
        normalized=normalized,
        price_returns=price_returns,
        trading_days=trading_days,
        rebalance_dates=rebalance_dates,
        signal_dates_by_rebalance=signal_dates_by_rebalance,
    )
    momentum_free = _evaluate_trials(
        rng=rng,
        trials=max(1, args.trials // 2),
        momentum_free=True,
        value_pool=value_pool,
        normalized=normalized,
        price_returns=price_returns,
        trading_days=trading_days,
        rebalance_dates=rebalance_dates,
        signal_dates_by_rebalance=signal_dates_by_rebalance,
    )
    all_results = low_momentum + momentum_free
    all_results.sort(
        key=lambda result: _rank_key(result, baseline),
        reverse=True,
    )
    strict = [
        result
        for result in all_results
        if _strictly_preserves(result, baseline)
    ]
    practical = [
        result
        for result in all_results
        if _practically_preserves(result, baseline)
    ]
    zero_momentum = [
        result
        for result in momentum_free
        if _practically_preserves(result, baseline)
    ]
    zero_momentum.sort(
        key=lambda result: _rank_key(result, baseline),
        reverse=True,
    )

    reported = (
        strict[: args.top]
        + practical[: args.top]
        + zero_momentum[: args.top]
    )
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
            "low_momentum_trials": args.trials,
            "momentum_free_trials": max(1, args.trials // 2),
            "value_mass_minimum": 0.60,
            "tr_12_1_maximum": 0.08,
            "technical_mass_maximum": 0.14,
            "max_positions": 10,
            "strict_preservation_count": len(strict),
            "practical_preservation_count": len(practical),
            "momentum_free_practical_count": len(zero_momentum),
        },
        "baseline": _result_payload(baseline),
        "value_pool": value_pool,
        "used_factor_specs": {
            factor: {
                "financial_basis": specs[factor][1],
                "direction": specs[factor][2],
                "factor_type": metadata.get(factor, {}).get("factor_type"),
            }
            for factor in used_factors
        },
        "strictly_preserved": [
            _result_payload(result)
            for result in strict[: args.top]
        ],
        "practically_preserved": [
            _result_payload(result)
            for result in practical[: args.top]
        ],
        "momentum_free_practical": [
            _result_payload(result)
            for result in zero_momentum[: args.top]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
