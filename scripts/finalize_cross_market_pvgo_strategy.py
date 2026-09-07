from __future__ import annotations

"""Save and re-query the two-market PVGO strategy family."""

from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Any

from api.service.dto import FactorLabExperimentSaveRequestDto, FactorLabRunRequestDto
from api.service.factor_lab_service import FactorLabService
from scripts.discover_kr_pvgo_multifactor_strategy import (
    CandidateSpec,
    REFINED_SOURCE_MODEL_NAME,
    _experiment_identity,
    _reachable_factor_ids,
    _weighted_sleeve,
    build_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import GateSpec, PortfolioSpec, _graph_hash, _jsonable, _write_checkpoint
from scripts.search_cross_market_pvgo_strategy import (
    KR_FULL,
    KR_HOLDOUT,
    KR_TRAIN,
    KR_VALIDATION,
    US_FULL,
    US_HOLDOUT,
    US_TRAIN,
    US_VALIDATION,
    _backtest,
    _run_history,
)


OUTPUT = Path("deliverables/cross_market_pvgo_strategy_20260906.json")
US_RESEARCH = Path("deliverables/cross_market_pvgo_us_trend_confirmations_20260906.json")
KR_RESEARCH = Path("deliverables/kr_pvgo_multifactor_combinations_20260906.json")
KR_MODEL_NAME = "Arcana_KR_PVGO_AssetDiscipline_LowVol_NoBeta_Quarterly_2002_2026_20260906"
US_MODEL_NAME = "Arcana_US_PVGO_TrendFCFValue_Quarterly_2016_2026_20260906"
PRIMARY_COST_BPS = 50.0
COSTS_BPS = (20.0, 50.0, 100.0)
KR_PORTFOLIO = PortfolioSpec(top_percent=30.0, max_positions=50)
US_PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)


def kr_spec() -> CandidateSpec:
    return CandidateSpec(
        core_weight=0.40,
        sleeves=(
            _weighted_sleeve("asset_growth_discipline", 0.30),
            _weighted_sleeve("low_volatility", 0.30),
        ),
        beta_gate=None,
    )


def us_spec() -> CandidateSpec:
    return CandidateSpec(
        core_weight=0.20,
        sleeves=(
            _weighted_sleeve("low_volatility", 0.20),
            _weighted_sleeve("shareholder_yield", 0.25),
            _weighted_sleeve("cash_to_debt", 0.25),
            _weighted_sleeve("fcf_to_ev_yield", 0.10),
        ),
        beta_gate=1.2,
        min_market_cap_mil=1_000.0,
        require_positive_normalized_nopat=True,
        extra_gates=(GateSpec("tr_12_1", "greater_than", 0.0, "annual"),),
    )


def _run_saved(
    service: FactorLabService,
    experiment_id: str,
    period: tuple[Any, Any],
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment_id,
            mode="history",
            history_start_date=period[0],
            history_end_date=period[1],
            history_rebalance_frequency="quarterly",
        )
    )


def _metric_triplet(result: dict[str, Any]) -> tuple[float, float, float]:
    metrics = result["metrics"]
    return (
        float(metrics["sharpe"]),
        float(metrics["cagr"]),
        float(metrics["max_drawdown"]),
    )


def _equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
        for a, b in zip(_metric_triplet(left), _metric_triplet(right), strict=True)
    )


def _save_market(
    service: FactorLabService,
    *,
    market: str,
    model_name: str,
    spec: CandidateSpec,
    portfolio: PortfolioSpec,
    full_period: tuple[Any, Any],
    periods: dict[str, tuple[Any, Any]],
) -> dict[str, Any]:
    graph = build_graph(
        model_name,
        spec,
        start_date=full_period[0],
        end_date=full_period[1],
    )
    graph.experiment.market = market
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])

    inline = _run_history(service, graph, full_period)
    inline_primary = _backtest(
        service,
        inline.run_id,
        market=market,
        period=full_period,
        portfolio=portfolio,
        cost_bps=PRIMARY_COST_BPS,
    )
    if float(inline_primary["metrics"]["sharpe"]) <= 1.0:
        raise RuntimeError(
            f"{market} full-period Sharpe gate failed before save: "
            f"{inline_primary['metrics']['sharpe']}"
        )

    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    history = _run_saved(service, saved.experiment_id, full_period)
    saved_primary = _backtest(
        service,
        history.run_id,
        market=market,
        period=full_period,
        portfolio=portfolio,
        cost_bps=PRIMARY_COST_BPS,
    )
    if inline.graph_hash != history.graph_hash or not _equivalent(
        inline_primary, saved_primary
    ):
        raise RuntimeError(f"{market} inline/saved re-query mismatch")

    evidence = {
        label: _backtest(
            service,
            history.run_id,
            market=market,
            period=period,
            portfolio=portfolio,
            cost_bps=PRIMARY_COST_BPS,
        )
        for label, period in {**periods, "full_period": full_period}.items()
    }
    if float(evidence["full_period"]["metrics"]["sharpe"]) <= 1.0:
        raise RuntimeError(f"{market} saved experiment failed the Sharpe gate")
    costs = {
        f"cost_{int(cost)}bps": _backtest(
            service,
            history.run_id,
            market=market,
            period=full_period,
            portfolio=portfolio,
            cost_bps=cost,
        )
        for cost in COSTS_BPS
    }
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )
    return {
        "model_name": model_name,
        "experiment_id": saved.experiment_id,
        "market": market,
        "graph_hash_sha256": _graph_hash(graph),
        "spec": asdict(spec),
        "portfolio": asdict(portfolio),
        "factor_ids": _reachable_factor_ids(graph, validation.execution_order),
        "goal_evidence_50bps": evidence,
        "cost_scenarios": costs,
        "history_run_id": history.run_id,
        "history_quality": _jsonable(history.quality),
        "screen_run_id": screen.run_id,
        "screen_quality": _jsonable(screen.quality),
        "screen_top20": [_jsonable(row) for row in screen.rows[:20]],
        "inline_saved_requery_match": True,
    }


def _us_pareto_research(service: FactorLabService) -> list[dict[str, Any]]:
    research = json.loads(US_RESEARCH.read_text(encoding="utf-8"))
    rows = []
    for key in ("fcf_to_ev_yield_no_delta", "asset_growth_discipline_10"):
        candidate = research["candidates"][key]
        result = _backtest(
            service,
            candidate["history_run_id"],
            market="US",
            period=US_FULL,
            portfolio=US_PORTFOLIO,
            cost_bps=PRIMARY_COST_BPS,
        )
        rows.append({"key": key, "full_period_50bps": result})
    return rows


def main() -> None:
    service = FactorLabService()
    source_before = _experiment_identity(
        service.get_experiment_by_name(REFINED_SOURCE_MODEL_NAME)
    )
    markets = {
        "KR": _save_market(
            service,
            market="KR",
            model_name=KR_MODEL_NAME,
            spec=kr_spec(),
            portfolio=KR_PORTFOLIO,
            full_period=KR_FULL,
            periods={
                "train": KR_TRAIN,
                "validation": KR_VALIDATION,
                "holdout": KR_HOLDOUT,
            },
        ),
        "US": _save_market(
            service,
            market="US",
            model_name=US_MODEL_NAME,
            spec=us_spec(),
            portfolio=US_PORTFOLIO,
            full_period=US_FULL,
            periods={
                "train": US_TRAIN,
                "validation": US_VALIDATION,
                "holdout": US_HOLDOUT,
            },
        ),
    }
    source_after = _experiment_identity(
        service.get_experiment_by_name(REFINED_SOURCE_MODEL_NAME)
    )
    if source_before != source_after:
        raise RuntimeError("refined PVGO source changed while saving final strategies")
    payload = {
        "design": {
            "primary_transaction_cost_bps": PRIMARY_COST_BPS,
            "requested_full_periods": {
                "KR": [str(value) for value in KR_FULL],
                "US": [str(value) for value in US_FULL],
            },
            "architecture": "shared refined PVGO core with market-specific confirmation sleeves",
            "identical_cross_market_candidate_result": "rejected by pre-holdout transfer tests",
            "research_artifacts": [str(KR_RESEARCH), str(US_RESEARCH)],
            "multiple_testing_warning": (
                "raw Sharpe ratios are not deflated for the number and dependence of trials"
            ),
        },
        "final": {
            "objective": "maximize cross-market robust Sharpe and CAGR while minimizing absolute MDD",
            "market_adaptive_strategy_audit": {
                "passed": True,
                "shared_pvgo_core": True,
                "shared_core_relative_weights": {
                    "pvgo_gap_pct": 0.45,
                    "roiic_wacc_spread": 0.30,
                    "pvgo_compression_pct": 0.25,
                },
                "identical_weights_across_markets": False,
                "reason": "identical confirmation sleeves failed transfer in both directions",
            },
            "selected_on_cross_market_pareto_front": True,
            "us_pareto_front_50bps": _us_pareto_research(service),
            "markets": markets,
            "source_preservation": {
                "before": source_before,
                "after": source_after,
                "unchanged": source_before == source_after,
            },
            "limitations": [
                "The U.S. Sharpe-above-1 claim holds at 50 bps, but not at the 100 bps stress cost.",
                "The U.S. design is a second research loop after the first frozen holdout candidate failed.",
                "Required U.S. PVGO compression data is not investable before 2017; the 2016 test year is an explicit cash/abstention period, not an invested return history.",
                "Delisted-security history is incomplete, so survivor bias is not fully eliminated.",
                "Flat bps costs do not model spread, market impact, ADV, taxes, or capacity.",
                "Korean PVGO input coverage is sparse in 2013-2015; missing-score weights renormalize over available declared sleeves.",
            ],
        },
    }
    _write_checkpoint(OUTPUT, payload)
    print(
        json.dumps(
            {
                market: {
                    "model_name": result["model_name"],
                    "experiment_id": result["experiment_id"],
                    "full_period_50bps": result["goal_evidence_50bps"]["full_period"]["metrics"],
                }
                for market, result in markets.items()
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
