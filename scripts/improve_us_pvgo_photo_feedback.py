from __future__ import annotations

"""Final bounded search for screenshot-derived overlays on the latest US PVGO run."""

from dataclasses import asdict
from datetime import date
import argparse
import json
import math
from pathlib import Path
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.finalize_cross_market_pvgo_benchmark_strategy import _benchmark_metrics
from scripts.improve_us_pvgo_quality_value_confirmation import (
    COSTS_BPS,
    PORTFOLIO,
    PRIMARY_COST_BPS,
    SCREEN_WEIGHTS,
    SOURCE_EXPERIMENT_ID,
    SOURCE_GRAPH_HASH,
    SOURCE_RUN_ID,
    _backtests,
    _run_graph,
    _same_metrics,
    _selection,
    _selection_key,
    build_blend_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import _jsonable
from scripts.search_cross_market_pvgo_strategy import US_FULL, US_HOLDOUT, US_TRAIN, US_VALIDATION, _backtest


MODEL_NAME = "Arcana_US_PVGO_RnDIntensitySanity_Quarterly_2016_2026_20260907"
OUTPUT = Path("deliverables/us_pvgo_photo_feedback_improvement_20260907.json")
FACTOR_SPECS: dict[str, dict[str, str]] = {
    "innovative_roe": {
        "factor_id": "iroe",
        "basis": "ttm",
        "direction": "higher_better",
        "role": "reward R&D-capitalized return on equity rather than spending alone",
    },
    "rim_upside": {
        "factor_id": "rim_upside_potential",
        "basis": "ttm",
        "direction": "higher_better",
        "role": "require residual-income value headroom after expectations are priced",
    },
    "rnd_intensity_sanity": {
        "factor_id": "rnd_to_market_cap",
        "basis": "ttm",
        "direction": "lower_better",
        "role": "penalize extreme R&D/market-cap ratios that can be inflated by price collapse",
    },
}
MODULE_HISTORY_START = date(2016, 4, 1)


def build_single_factor_module(
    key: str,
    *,
    name: str,
):
    from api.service.dto import FactorLabGraphDto

    spec = FACTOR_SPECS[key]
    nodes = [
        {
            "id": "factor_input",
            "type": "factor_input",
            "config": {
                "factor_id": spec["factor_id"],
                "financial_basis": spec["basis"],
                "missing_policy": "drop",
            },
        },
        {
            "id": "factor_winsor",
            "type": "winsorize",
            "config": {
                "group_by": ["trade_date"],
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            },
        },
        {
            "id": "factor_sector_z",
            "type": "zscore",
            "config": {
                "group_by": ["trade_date", "sector"],
                "stddev_method": "population",
                "min_count": 5,
                "zero_std_policy": "invalid",
                "direction": spec["direction"],
                "clip": 3.0,
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
    edges = [
        {
            "id": "input_to_winsor",
            "source": "factor_input",
            "target": "factor_winsor",
            "target_handle": "input",
        },
        {
            "id": "winsor_to_z",
            "source": "factor_winsor",
            "target": "factor_sector_z",
            "target_handle": "input",
        },
        {
            "id": "z_to_rank",
            "source": "factor_sector_z",
            "target": "module_rank_score",
            "target_handle": "input",
        },
    ]
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": US_FULL[0],
            "end_date": US_FULL[1],
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


def _run_module(
    service: FactorLabService,
    *,
    graph=None,
    experiment_id: str | None = None,
):
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=graph,
            experiment_id=experiment_id,
            mode="history",
            history_start_date=MODULE_HISTORY_START,
            history_end_date=US_FULL[1],
            history_rebalance_frequency="quarterly",
        )
    )


def run(*, finalize: bool, output_path: Path = OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source_before = service.get_experiment(SOURCE_EXPERIMENT_ID)
    source_json = source_before.graph.model_dump_json()
    if service.validate_graph(source_before.graph).graph_hash != SOURCE_GRAPH_HASH:
        raise RuntimeError("source graph hash changed before bounded search")

    selection_periods = (("train", US_TRAIN), ("validation", US_VALIDATION))
    baseline_selection_metrics = _backtests(service, SOURCE_RUN_ID, selection_periods)
    baseline_selection = _selection(baseline_selection_metrics)
    modules: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for key, spec in FACTOR_SPECS.items():
        module_graph = build_single_factor_module(
            key,
            name=f"Arcana_US_PVGO_PhotoModule_{key}_Research_20260907",
        )
        validation = service.validate_graph(module_graph)
        if not validation.valid:
            raise RuntimeError([_jsonable(issue) for issue in validation.errors])
        module_run = _run_module(service, graph=module_graph)
        modules[key] = {
            "spec": spec,
            "run_id": module_run.run_id,
            "graph_hash": module_run.graph_hash,
            "quality": _jsonable(module_run.quality),
        }
        for weight in SCREEN_WEIGHTS:
            graph = build_blend_graph(
                module_run.run_id,
                weight,
                f"{MODEL_NAME}__Research_{key}_w{weight:g}",
            )
            history = _run_graph(service, graph=graph)
            metrics = _backtests(service, history.run_id, selection_periods)
            candidates.append(
                {
                    "factor_key": key,
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
    baseline_evaluation = _backtests(service, SOURCE_RUN_ID, evaluation_periods)
    selected_evaluation = _backtests(service, selected["run_id"], evaluation_periods)
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
        "status": "candidate_passed" if final_gate else "candidate_rejected",
        "source": {
            "experiment_id": SOURCE_EXPERIMENT_ID,
            "run_id": SOURCE_RUN_ID,
            "graph_hash": SOURCE_GRAPH_HASH,
        },
        "bounded_search": {
            "factor_specs": FACTOR_SPECS,
            "weights": list(SCREEN_WEIGHTS),
            "candidate_count": len(candidates),
            "selection_data": "2016-2019 train plus 2020-2023 validation only",
            "holdout_policy": "2024-2026 opened once after selection",
            "portfolio": asdict(PORTFOLIO),
            "transaction_cost_bps": PRIMARY_COST_BPS,
        },
        "modules": modules,
        "baseline_selection_metrics_50bps": baseline_selection_metrics,
        "baseline_selection": baseline_selection,
        "candidates": candidates,
        "selected": selected,
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
            raise RuntimeError("bounded-search finalization gate did not pass")
        selected_key = str(selected["factor_key"])
        module_graph = build_single_factor_module(
            selected_key,
            name=(
                "Arcana_US_PVGO_PhotoModule_"
                f"{selected_key}_Quarterly_2016_2026_20260907"
            ),
        )
        saved_module = service.save_experiment(
            FactorLabExperimentSaveRequestDto(graph=module_graph)
        )
        saved_module_run = _run_module(
            service,
            experiment_id=saved_module.experiment_id,
        )
        final_name = (
            MODEL_NAME
            if selected_key == "rnd_intensity_sanity"
            else "Arcana_US_PVGO_"
            + selected_key.title().replace("_", "")
            + "_Quarterly_2016_2026_20260907"
        )
        final_graph = build_blend_graph(
            saved_module_run.run_id,
            float(selected["weight"]),
            final_name,
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
            raise RuntimeError("saved graph does not reproduce the frozen candidate")
        costs = {
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
                "model_name": final_name,
                "experiment_id": saved.experiment_id,
                "history_run_id": saved_run.run_id,
                "saved_graph_hash": saved_run.graph_hash,
                "module_experiment_id": saved_module.experiment_id,
                "module_history_run_id": saved_module_run.run_id,
                "module_saved_graph_hash": saved_module_run.graph_hash,
                "cost_sensitivity": costs,
                "benchmark_metrics": _benchmark_metrics(full_result),
            }
        )

    source_after = service.get_experiment(SOURCE_EXPERIMENT_ID)
    payload["source_unchanged"] = (
        source_after.graph.model_dump_json() == source_json
        and service.validate_graph(source_after.graph).graph_hash == SOURCE_GRAPH_HASH
    )
    if not payload["source_unchanged"]:
        raise RuntimeError("source experiment changed during bounded search")
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
