from __future__ import annotations

"""Research and persist a PIT quality/value confirmation for the latest US PVGO run."""

from dataclasses import asdict
from datetime import date
import argparse
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService, _lab_factor_id
from scripts.finalize_cross_market_pvgo_benchmark_strategy import _benchmark_metrics
from scripts.optimize_kr_pvgo_expectations_alpha import PortfolioSpec, _jsonable
from scripts.search_cross_market_pvgo_strategy import (
    US_FULL,
    US_HOLDOUT,
    US_TRAIN,
    US_VALIDATION,
    _backtest,
)


SOURCE_EXPERIMENT_ID = "3cf2cc3b-89bc-4e09-a209-5998f8f9978b"
SOURCE_RUN_ID = "47a4cb68-2538-424f-9e41-ba040cfbfd69"
SOURCE_GRAPH_HASH = "d869d218af05d901aa393cba9d75c0e7fe0b1e5719d17112af9a509fdeae29dc"
MODEL_NAME = (
    "Arcana_US_PVGO_InnovationEfficiencyValueConfirmation_"
    "Quarterly_2016_2026_20260907"
)
MODULE_MODEL_NAME = (
    "Arcana_US_PVGO_InnovationEfficiencyValueModule_"
    "Quarterly_2016_2026_20260907"
)
OUTPUT = Path(
    "deliverables/us_pvgo_innovation_efficiency_value_confirmation_20260907.json"
)
PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)
PRIMARY_COST_BPS = 50.0
COSTS_BPS = (20.0, 50.0, 100.0)
SCREEN_WEIGHTS = (0.025, 0.05, 0.10)
MODULE_WEIGHTS = {
    "innovative_roe": 0.40,
    "gross_profitability": 0.30,
    "rim_upside": 0.30,
}


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"{source}__to__{target}__{target_handle}",
        "source": source,
        "target": target,
        "target_handle": target_handle,
    }


def _factor_node(node_id: str, factor_id: str, basis: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "factor_input",
        "config": {
            "factor_id": factor_id,
            "financial_basis": basis,
            "missing_policy": "drop",
        },
    }


def _rank_pipeline(
    key: str,
    factor_id: str,
    *,
    basis: str = "ttm",
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{key}_input"
    winsor_id = f"{key}_winsor"
    z_id = f"{key}_sector_z"
    rank_id = f"{key}_rank"
    nodes = [
        _factor_node(input_id, factor_id, basis),
        {
            "id": winsor_id,
            "type": "winsorize",
            "config": {
                "group_by": ["trade_date"],
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            },
        },
        {
            "id": z_id,
            "type": "zscore",
            "config": {
                "group_by": ["trade_date", "sector"],
                "stddev_method": "population",
                "min_count": 5,
                "zero_std_policy": "invalid",
                "direction": "higher_better",
                "clip": 3.0,
            },
        },
        {
            "id": rank_id,
            "type": "dense_score",
            "config": {
                "group_by": ["trade_date"],
                "order": "desc",
                "scale": "0_100",
                "tie_method": "average",
            },
        },
    ]
    edges = [
        _edge(input_id, winsor_id, "input"),
        _edge(winsor_id, z_id, "input"),
        _edge(z_id, rank_id, "input"),
    ]
    return nodes, edges, rank_id


def build_module_graph(
    name: str = MODULE_MODEL_NAME,
    *,
    start_date: date = US_FULL[0],
    end_date: date = US_FULL[1],
) -> FactorLabGraphDto:
    """Build a complete-case PIT score for realized innovation efficiency and value."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    for key, factor_id in (
        ("innovative_roe", "iroe"),
        ("gross_profitability", "gross_profitability_pct"),
        ("rim_upside", "rim_upside_potential"),
    ):
        extra_nodes, extra_edges, output = _rank_pipeline(key, factor_id)
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        sources[key] = output
    nodes.extend(
        [
            {
                "id": "quality_value_score",
                "type": "weighted_score",
                "config": {
                    "weights": MODULE_WEIGHTS,
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "complete-case PIT confirmation: intangible-adjusted ROE, "
                        "asset profitability, and residual-income upside"
                    ),
                },
            },
            {
                "id": "module_rank_score",
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                    "tie_method": "average",
                },
            },
        ]
    )
    edges.extend(
        _edge(source, "quality_value_score", handle)
        for handle, source in sources.items()
    )
    edges.append(_edge("quality_value_score", "module_rank_score", "input"))
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": start_date,
            "end_date": end_date,
            "factor_data_mode": "point_in_time_snapshot",
            "snapshot_coverage_policy": "allow_missing_inputs",
            "universe": {
                "type": "market",
                "sector_codes": [],
                "industry_group_codes": [],
            },
            "rebalance": {
                "frequency": "quarterly",
                "signal_lag_days": 1,
                "transaction_cost_bps": PRIMARY_COST_BPS,
            },
        },
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "module_rank_score"},
    )


def build_blend_graph(
    module_run_id: str,
    confirmation_weight: float,
    name: str,
    *,
    source_run_id: str = SOURCE_RUN_ID,
) -> FactorLabGraphDto:
    if not 0.0 < confirmation_weight < 1.0:
        raise ValueError("confirmation_weight must be in (0, 1)")
    nodes = [
        _factor_node("anchor_score", _lab_factor_id(source_run_id), "lab"),
        _factor_node("confirmation_score", _lab_factor_id(module_run_id), "lab"),
        {"id": "anchor_floor", "type": "constant", "config": {"value": -1.0}},
        {
            "id": "blended_score",
            "type": "weighted_score",
            "config": {
                "weights": {
                    "anchor": 1.0 - confirmation_weight,
                    "confirmation": confirmation_weight,
                },
                "missing_weight_renormalize": True,
            },
        },
        {"id": "anchor_member", "type": "greater_than", "config": {}},
        {"id": "anchor_universe_only", "type": "condition_score", "config": {}},
        {
            "id": "final_rank_score",
            "type": "dense_score",
            "config": {
                "group_by": ["trade_date"],
                "order": "desc",
                "scale": "0_100",
                "tie_method": "average",
            },
        },
    ]
    edges = [
        _edge("anchor_score", "blended_score", "anchor"),
        _edge("confirmation_score", "blended_score", "confirmation"),
        _edge("anchor_score", "anchor_member", "left"),
        _edge("anchor_floor", "anchor_member", "right"),
        _edge("anchor_member", "anchor_universe_only", "condition"),
        _edge("blended_score", "anchor_universe_only", "score"),
        _edge("anchor_universe_only", "final_rank_score", "input"),
    ]
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": US_FULL[0],
            "end_date": US_FULL[1],
            "factor_data_mode": "raw",
            "snapshot_coverage_policy": "strict",
            "universe": {
                "type": "market",
                "sector_codes": [],
                "industry_group_codes": [],
            },
            "rebalance": {
                "frequency": "quarterly",
                "signal_lag_days": 1,
                "transaction_cost_bps": PRIMARY_COST_BPS,
            },
        },
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )


def _run_graph(
    service: FactorLabService,
    *,
    graph: FactorLabGraphDto | None = None,
    experiment_id: str | None = None,
):
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=graph,
            experiment_id=experiment_id,
            mode="history",
            history_start_date=US_FULL[0],
            history_end_date=US_FULL[1],
            history_rebalance_frequency="quarterly",
        )
    )


def _backtests(
    service: FactorLabService,
    run_id: str,
    periods: tuple[tuple[str, tuple[date, date]], ...],
) -> dict[str, Any]:
    return {
        label: _backtest(
            service,
            run_id,
            market="US",
            period=period,
            portfolio=PORTFOLIO,
            cost_bps=PRIMARY_COST_BPS,
        )
        for label, period in periods
    }


def _selection(metrics: dict[str, Any]) -> dict[str, float]:
    rows = [metrics["train"]["metrics"], metrics["validation"]["metrics"]]
    return {
        "worst_sharpe": min(float(row["sharpe"]) for row in rows),
        "mean_sharpe": mean(float(row["sharpe"]) for row in rows),
        "mean_cagr": mean(float(row["cagr"]) for row in rows),
        "worst_abs_mdd": max(abs(float(row["max_drawdown"])) for row in rows),
    }


def _selection_key(row: dict[str, Any]) -> tuple[float, float, float, float]:
    score = row["selection"]
    return (
        score["worst_sharpe"],
        score["mean_sharpe"],
        score["mean_cagr"],
        -score["worst_abs_mdd"],
    )


def _same_metrics(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        math.isclose(
            float(left["metrics"][key]),
            float(right["metrics"][key]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        for key in ("cumulative_return", "cagr", "max_drawdown", "sharpe")
    )


def run(*, finalize: bool, output_path: Path = OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source_before = service.get_experiment(SOURCE_EXPERIMENT_ID)
    source_json = source_before.graph.model_dump_json()
    source_hash = service.validate_graph(source_before.graph).graph_hash
    if source_hash != SOURCE_GRAPH_HASH:
        raise RuntimeError(f"source graph hash changed: {source_hash}")

    module_graph = build_module_graph(
        MODULE_MODEL_NAME if finalize else MODULE_MODEL_NAME + "__ResearchOnly"
    )
    module_validation = service.validate_graph(module_graph)
    if not module_validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in module_validation.errors])
    module_run = _run_graph(service, graph=module_graph)
    if module_run.status != "completed":
        raise RuntimeError(f"module run failed: {module_run.status}")

    selection_periods = (("train", US_TRAIN), ("validation", US_VALIDATION))
    baseline_selection_metrics = _backtests(
        service, SOURCE_RUN_ID, selection_periods
    )
    baseline_selection = _selection(baseline_selection_metrics)
    candidates: list[dict[str, Any]] = []
    for weight in SCREEN_WEIGHTS:
        graph = build_blend_graph(
            module_run.run_id,
            weight,
            f"{MODEL_NAME}__Research_w{weight:g}",
        )
        validation = service.validate_graph(graph)
        if not validation.valid:
            raise RuntimeError([_jsonable(issue) for issue in validation.errors])
        history = _run_graph(service, graph=graph)
        metrics = _backtests(service, history.run_id, selection_periods)
        candidates.append(
            {
                "weight": weight,
                "run_id": history.run_id,
                "graph_hash": history.graph_hash,
                "quality": _jsonable(history.quality),
                "metrics_50bps": metrics,
                "selection": _selection(metrics),
            }
        )
    selected = max(candidates, key=_selection_key)
    selected_passed = (
        selected["selection"]["worst_sharpe"]
        > baseline_selection["worst_sharpe"]
        and selected["selection"]["mean_sharpe"]
        >= baseline_selection["mean_sharpe"]
    )

    evaluation_periods = (("holdout", US_HOLDOUT), ("full", US_FULL))
    baseline_evaluation = _backtests(
        service, SOURCE_RUN_ID, evaluation_periods
    )
    selected_evaluation = _backtests(
        service, selected["run_id"], evaluation_periods
    )
    holdout_not_worse = (
        float(selected_evaluation["holdout"]["metrics"]["sharpe"])
        >= float(baseline_evaluation["holdout"]["metrics"]["sharpe"])
    )
    full_improved = (
        float(selected_evaluation["full"]["metrics"]["sharpe"])
        > float(baseline_evaluation["full"]["metrics"]["sharpe"])
        and abs(float(selected_evaluation["full"]["metrics"]["max_drawdown"]))
        <= abs(float(baseline_evaluation["full"]["metrics"]["max_drawdown"]))
    )
    final_gate = selected_passed and holdout_not_worse and full_improved

    payload: dict[str, Any] = {
        "model_name": MODEL_NAME,
        "status": "candidate_passed" if final_gate else "candidate_rejected",
        "source": {
            "experiment_id": SOURCE_EXPERIMENT_ID,
            "run_id": SOURCE_RUN_ID,
            "graph_hash": SOURCE_GRAPH_HASH,
        },
        "design": {
            "module_weights": MODULE_WEIGHTS,
            "screen_confirmation_weights": list(SCREEN_WEIGHTS),
            "selection_periods_only": {
                "train": [str(value) for value in US_TRAIN],
                "validation": [str(value) for value in US_VALIDATION],
            },
            "holdout_policy": "opened once after factor and weight freeze",
            "portfolio": asdict(PORTFOLIO),
            "transaction_cost_bps": PRIMARY_COST_BPS,
        },
        "module_research_run_id": module_run.run_id,
        "module_graph_hash": module_run.graph_hash,
        "module_quality": _jsonable(module_run.quality),
        "baseline_selection_metrics_50bps": baseline_selection_metrics,
        "baseline_selection": baseline_selection,
        "candidates": candidates,
        "selected_weight": selected["weight"],
        "selected_research_run_id": selected["run_id"],
        "selected_selection": selected["selection"],
        "selected_passed_pre_holdout_rule": selected_passed,
        "baseline_evaluation_50bps": baseline_evaluation,
        "selected_evaluation_50bps": selected_evaluation,
        "holdout_not_worse": holdout_not_worse,
        "full_sharpe_and_mdd_improved": full_improved,
        "final_gate_passed": final_gate,
        "finalized": False,
    }

    if finalize:
        if not final_gate:
            raise RuntimeError("frozen finalization gate did not pass")
        saved_module = service.save_experiment(
            FactorLabExperimentSaveRequestDto(graph=module_graph)
        )
        saved_module_run = _run_graph(
            service, experiment_id=saved_module.experiment_id
        )
        if saved_module_run.graph_hash != module_run.graph_hash:
            raise RuntimeError("saved module graph hash mismatch")
        final_graph = build_blend_graph(
            saved_module_run.run_id,
            float(selected["weight"]),
            MODEL_NAME,
        )
        saved = service.save_experiment(
            FactorLabExperimentSaveRequestDto(graph=final_graph)
        )
        saved_run = _run_graph(service, experiment_id=saved.experiment_id)
        saved_full = _backtest(
            service,
            saved_run.run_id,
            market="US",
            period=US_FULL,
            portfolio=PORTFOLIO,
            cost_bps=PRIMARY_COST_BPS,
        )
        if not _same_metrics(selected_evaluation["full"], saved_full):
            raise RuntimeError("saved strategy does not reproduce research metrics")
        cost_sensitivity = {
            str(int(cost)): _backtest(
                service,
                saved_run.run_id,
                market="US",
                period=US_FULL,
                portfolio=PORTFOLIO,
                cost_bps=cost,
            )
            for cost in COSTS_BPS
        }
        full_result = service.run_backtest(
            saved_run.run_id,
            FactorLabBacktestRequestDto(
                top_percent=PORTFOLIO.top_percent,
                start_date=US_FULL[0],
                end_date=US_FULL[1],
                rebalance_frequency="quarterly",
                market="US",
                benchmarks=["US_NASDAQ", "US_SP500"],
                max_positions=PORTFOLIO.max_positions,
                transaction_cost_bps=PRIMARY_COST_BPS,
            ),
        )
        payload.update(
            {
                "status": "finalized",
                "finalized": True,
                "experiment_id": saved.experiment_id,
                "history_run_id": saved_run.run_id,
                "saved_graph_hash": saved_run.graph_hash,
                "module_experiment_id": saved_module.experiment_id,
                "module_history_run_id": saved_module_run.run_id,
                "module_saved_graph_hash": saved_module_run.graph_hash,
                "cost_sensitivity": cost_sensitivity,
                "benchmark_metrics": _benchmark_metrics(full_result),
            }
        )

    source_after = service.get_experiment(SOURCE_EXPERIMENT_ID)
    payload["source_unchanged"] = (
        source_after.graph.model_dump_json() == source_json
        and service.validate_graph(source_after.graph).graph_hash
        == SOURCE_GRAPH_HASH
    )
    if not payload["source_unchanged"]:
        raise RuntimeError("source experiment changed during the workflow")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = run(finalize=args.finalize, output_path=args.output)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
