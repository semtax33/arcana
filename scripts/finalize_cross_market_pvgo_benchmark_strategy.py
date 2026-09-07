from __future__ import annotations

"""Persist and verify the benchmark-beating cross-market PVGO strategy."""

from dataclasses import asdict
from datetime import date
import json
import math
from pathlib import Path
from typing import Any

from api.model.backtest import BacktestEquityCurvePoint, FactorBacktestResult
from api.service.backtest_service import _summary
from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService, _lab_factor_id
from scripts.discover_kr_pvgo_multifactor_strategy import (
    CandidateSpec,
    _reachable_factor_ids,
    _weighted_sleeve,
    build_graph,
)


OUTPUT = Path("deliverables/cross_market_pvgo_benchmark_strategy_20260907.json")
KR_MODEL_NAME = (
    "Arcana_KR_PVGO_AssetDiscipline_LowVol_NoBeta_Quarterly_2002_2026_20260906"
)
US_PRIMARY_MODEL_NAME = (
    "Arcana_US_PVGO_TrendFCFValue_Quarterly_2016_2026_20260906"
)
US_FALLBACK_MODEL_NAME = (
    "Arcana_US_PVGO_ShareholderLiquidityFallback_Quarterly_2016_2026_20260907"
)
US_COMPOSITE_MODEL_NAME = (
    "Arcana_US_PVGO_ConfirmationFallback_Quarterly_2016_2026_20260907"
)
KR_RUN_ID = "80ce5ff3-d3ea-4427-a4c3-d7e67064f630"
US_PRIMARY_RUN_ID = "66d316cd-401a-4d28-bdaf-511a166c0738"
KR_PERIOD = (date(2002, 4, 1), date(2026, 9, 4))
US_PERIOD = (date(2016, 1, 4), date(2026, 9, 4))
TRANSACTION_COST_BPS = 50.0
US_BLEND_POLICY = "when_primary_has_no_positions"


def us_fallback_spec() -> CandidateSpec:
    return CandidateSpec(
        core_weight=0.30,
        sleeves=(
            _weighted_sleeve("shareholder_yield", 0.35),
            _weighted_sleeve("cash_to_debt", 0.35),
        ),
        beta_gate=1.5,
        min_market_cap_mil=1_000.0,
        require_positive_normalized_nopat=True,
    )


def _factor_ids(service: FactorLabService, graph: Any) -> list[str]:
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            f"invalid saved graph: {[item.message for item in validation.errors]}"
        )
    return _reachable_factor_ids(graph, validation.execution_order)


def _save_and_run_us_fallback(service: FactorLabService) -> tuple[str, str, list[str]]:
    graph = build_graph(
        US_FALLBACK_MODEL_NAME,
        us_fallback_spec(),
        start_date=US_PERIOD[0],
        end_date=US_PERIOD[1],
    )
    graph.experiment.market = "US"
    factor_ids = _factor_ids(service, graph)
    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    run = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=saved.experiment_id,
            mode="history",
            history_start_date=US_PERIOD[0],
            history_end_date=US_PERIOD[1],
            history_rebalance_frequency="quarterly",
        )
    )
    if run.status != "completed":
        raise RuntimeError(f"fallback FactorLab run did not complete: {run.status}")
    return saved.experiment_id, run.run_id, factor_ids


def _us_composite_graph(
    *,
    primary_run_id: str,
    fallback_run_id: str,
) -> FactorLabGraphDto:
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": US_COMPOSITE_MODEL_NAME,
            "market": "US",
            "start_date": US_PERIOD[0],
            "end_date": US_PERIOD[1],
            "universe": {
                "type": "market",
                "sector_codes": [],
                "industry_group_codes": [],
            },
            "rebalance": {
                "frequency": "quarterly",
                "signal_lag_days": 1,
                "transaction_cost_bps": TRANSACTION_COST_BPS,
            },
        },
        nodes=[
            {
                "id": "primary_score",
                "type": "factor_input",
                "position": {"x": 80, "y": 120},
                "config": {
                    "factor_id": _lab_factor_id(primary_run_id),
                    "financial_basis": "lab",
                },
            },
            {
                "id": "fallback_score",
                "type": "factor_input",
                "position": {"x": 80, "y": 300},
                "config": {
                    "factor_id": _lab_factor_id(fallback_run_id),
                    "financial_basis": "lab",
                },
            },
            {
                "id": "primary_else_fallback",
                "type": "date_fallback",
                "position": {"x": 420, "y": 210},
                "config": {
                    "backtest_policy": US_BLEND_POLICY,
                    "primary_run_id": primary_run_id,
                    "fallback_run_id": fallback_run_id,
                },
            },
        ],
        edges=[
            {
                "id": "primary_to_policy",
                "source": "primary_score",
                "target": "primary_else_fallback",
                "target_handle": "primary",
            },
            {
                "id": "fallback_to_policy",
                "source": "fallback_score",
                "target": "primary_else_fallback",
                "target_handle": "fallback",
            },
        ],
        outputs={"final_node_id": "primary_else_fallback"},
    )


def _save_and_run_us_composite(
    service: FactorLabService,
    *,
    primary_run_id: str,
    fallback_run_id: str,
) -> tuple[str, str]:
    graph = _us_composite_graph(
        primary_run_id=primary_run_id,
        fallback_run_id=fallback_run_id,
    )
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            f"invalid composite graph: {[item.message for item in validation.errors]}"
        )
    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    run = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=saved.experiment_id,
            mode="history",
            history_start_date=US_PERIOD[0],
            history_end_date=US_PERIOD[1],
            history_rebalance_frequency="quarterly",
        )
    )
    if run.status != "completed":
        raise RuntimeError(f"composite FactorLab run did not complete: {run.status}")
    return saved.experiment_id, run.run_id


def _assert_same_backtest(
    expected: FactorBacktestResult,
    actual: FactorBacktestResult,
) -> None:
    metric_names = (
        "cumulative_return",
        "cagr",
        "max_drawdown",
        "volatility",
        "sharpe",
    )
    for metric_name in metric_names:
        expected_value = getattr(expected.summary, metric_name)
        actual_value = getattr(actual.summary, metric_name)
        if expected_value is None or actual_value is None or not math.isclose(
            expected_value,
            actual_value,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f"composite backtest mismatch for {metric_name}: "
                f"expected={expected_value}, actual={actual_value}"
            )
    expected_dates = [point.trade_date for point in expected.equity_curve]
    actual_dates = [point.trade_date for point in actual.equity_curve]
    if expected_dates != actual_dates:
        raise RuntimeError("composite backtest trading-day calendar mismatch")


def _benchmark_metrics(result: FactorBacktestResult) -> dict[str, dict[str, Any]]:
    benchmark_ids = sorted(
        {
            benchmark_id
            for point in result.equity_curve
            for benchmark_id in point.benchmark_navs
        }
    )
    metrics: dict[str, dict[str, Any]] = {}
    for benchmark_id in benchmark_ids:
        points = [
            BacktestEquityCurvePoint(
                trade_date=point.trade_date,
                strategy_nav=float(point.benchmark_navs[benchmark_id]),
            )
            for point in result.equity_curve
            if point.benchmark_navs.get(benchmark_id) is not None
        ]
        if not points:
            raise RuntimeError(f"benchmark has no observations: {benchmark_id}")
        metrics[benchmark_id] = asdict(
            _summary(
                points,
                start_date=points[0].trade_date,
                end_date=points[-1].trade_date,
                rebalance_frequency=result.summary.rebalance_frequency,
                rebalance_count=0,
            )
        )
    return metrics


def _cash_day_count(result: FactorBacktestResult) -> int:
    positions_by_date = {
        rebalance.rebalance_date: bool(rebalance.positions)
        for rebalance in result.rebalance_history
    }
    invested = False
    count = 0
    for point in result.equity_curve:
        if point.trade_date in positions_by_date:
            invested = positions_by_date[point.trade_date]
        if not invested:
            count += 1
    return count


def _market_result(
    *,
    result: FactorBacktestResult,
    experiment_id: str,
    history_run_id: str,
    factor_ids: list[str],
    benchmark_ids: set[str],
) -> dict[str, Any]:
    benchmarks = _benchmark_metrics(result)
    if set(benchmarks) != benchmark_ids:
        raise RuntimeError(
            f"benchmark mismatch: expected={sorted(benchmark_ids)}, "
            f"actual={sorted(benchmarks)}"
        )
    cumulative_return = result.summary.cumulative_return
    sharpe = result.summary.sharpe
    goal_passed = (
        cumulative_return is not None
        and sharpe is not None
        and sharpe > 1.0
        and all(
            cumulative_return > benchmark["cumulative_return"]
            for benchmark in benchmarks.values()
        )
    )
    dates = [point.trade_date for point in result.equity_curve]
    full_coverage = (
        bool(dates)
        and dates == sorted(set(dates))
        and result.summary.return_observations == len(dates) - 1
    )
    return {
        "experiment_id": experiment_id,
        "history_run_id": history_run_id,
        "factor_ids": factor_ids,
        "observed_period": [dates[0].isoformat(), dates[-1].isoformat()],
        "equity_curve_points": len(dates),
        "full_trading_day_coverage": full_coverage,
        "cash_day_count": _cash_day_count(result),
        "strategy_metrics": asdict(result.summary),
        "benchmark_metrics": benchmarks,
        "goal_passed": goal_passed,
        "warnings": result.warnings,
    }


def main() -> None:
    service = FactorLabService()

    kr_experiment = service.get_experiment_by_name(KR_MODEL_NAME)
    kr_factor_ids = _factor_ids(service, kr_experiment.graph)
    kr_request = FactorLabBacktestRequestDto(
        top_percent=30,
        start_date=KR_PERIOD[0],
        end_date=KR_PERIOD[1],
        rebalance_frequency="quarterly",
        market="KR",
        benchmarks=["KOSPI200", "KOSDAQ"],
        max_positions=50,
        transaction_cost_bps=TRANSACTION_COST_BPS,
    )
    kr_backtest = service.run_backtest(KR_RUN_ID, kr_request)
    kr = _market_result(
        result=kr_backtest,
        experiment_id=kr_experiment.experiment_id,
        history_run_id=KR_RUN_ID,
        factor_ids=kr_factor_ids,
        benchmark_ids={"KOSPI200", "KOSDAQ"},
    )

    us_primary_experiment = service.get_experiment_by_name(US_PRIMARY_MODEL_NAME)
    us_primary_factor_ids = _factor_ids(service, us_primary_experiment.graph)
    fallback_experiment_id, fallback_run_id, fallback_factor_ids = (
        _save_and_run_us_fallback(service)
    )
    us_request = FactorLabBacktestRequestDto(
        top_percent=5,
        start_date=US_PERIOD[0],
        end_date=US_PERIOD[1],
        rebalance_frequency="quarterly",
        market="US",
        benchmarks=["US_QQQ", "US_SP500"],
        max_positions=20,
        transaction_cost_bps=TRANSACTION_COST_BPS,
    )
    blended_reference = service.run_blended_backtest(
        US_PRIMARY_RUN_ID,
        fallback_run_id,
        policy=US_BLEND_POLICY,
        request=us_request,
    )
    composite_experiment_id, composite_run_id = _save_and_run_us_composite(
        service,
        primary_run_id=US_PRIMARY_RUN_ID,
        fallback_run_id=fallback_run_id,
    )
    us_backtest = service.run_backtest(composite_run_id, us_request)
    _assert_same_backtest(blended_reference, us_backtest)
    us = _market_result(
        result=us_backtest,
        experiment_id=composite_experiment_id,
        history_run_id=composite_run_id,
        factor_ids=us_primary_factor_ids,
        benchmark_ids={"US_QQQ", "US_SP500"},
    )
    us.update(
        {
            "architecture": "primary_with_empty-signal_fallback",
            "model_name": US_COMPOSITE_MODEL_NAME,
            "primary_experiment_id": us_primary_experiment.experiment_id,
            "primary_history_run_id": US_PRIMARY_RUN_ID,
            "fallback_experiment_id": fallback_experiment_id,
            "fallback_history_run_id": fallback_run_id,
            "fallback_factor_ids": fallback_factor_ids,
            "composite_matches_service_blend": True,
        }
    )

    payload = {
        "strategy_name": (
            "Arcana_CrossMarket_PVGO_ConfirmationFallback_Quarterly_2002_2026_20260907"
        ),
        "objective": (
            "beat each local benchmark by cumulative return with Sharpe above 1; "
            "then maximize Sharpe and CAGR while minimizing absolute MDD"
        ),
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "us_blend_policy": US_BLEND_POLICY,
        "economic_thesis": {
            "shared_core": (
                "PVGO expectation gaps are accepted only when incremental returns exceed the "
                "cost of capital and embedded growth expectations are compressing."
            ),
            "KR_confirmation": (
                "Asset-growth discipline penalizes overinvestment and low volatility reduces "
                "the crash-prone speculative tail."
            ),
            "US_primary_confirmation": (
                "Positive trend, free-cash-flow yield, shareholder distributions, liquidity, "
                "positive normalized NOPAT, and scale confirm that PVGO is financed by durable "
                "cash economics rather than narrative growth."
            ),
            "US_fallback_confirmation": (
                "When the stricter primary screen has no holdings, shareholder yield and "
                "cash-to-debt retain a conservative, cash-backed expression of the same PVGO "
                "mispricing thesis; when both screens are empty the portfolio remains in cash."
            ),
        },
        "markets": {"KR": kr, "US": us},
        "goal_passed": kr["goal_passed"] and us["goal_passed"],
        "limitations": [
            "Returns use the current close-price backtester; cash dividends, taxes, spread, "
            "market impact, and capacity are not fully modeled.",
            "Delisted-security history is incomplete, so survivor bias is not fully eliminated.",
            "The U.S. full-period Sharpe is only modestly above 1 and its 2024-2026 holdout "
            "Sharpe is below 1; the full-period goal passes but robustness is not conclusive.",
            "The U.S. result fails the same goal under the previously measured 100 bps cost "
            "stress, so 50 bps is a binding implementation assumption.",
        ],
    }
    if not payload["goal_passed"]:
        raise RuntimeError("cross-market benchmark objective was not met")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                market: {
                    "strategy": row["strategy_metrics"],
                    "benchmarks": row["benchmark_metrics"],
                    "goal_passed": row["goal_passed"],
                }
                for market, row in payload["markets"].items()
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
