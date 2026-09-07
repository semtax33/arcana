from __future__ import annotations

"""Second-stage, pre-holdout refinement of the deduplicated Korean PVGO core."""

from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from statistics import mean
from typing import Any

from api.service.dto import FactorLabExperimentSaveRequestDto, FactorLabRunRequestDto
from api.service.factor_lab_service import FactorLabService
from scripts.optimize_kr_pvgo_expectations_alpha import (
    BENCHMARKS,
    EARLY_HISTORY,
    FINAL_COSTS_BPS,
    HOLDOUT,
    LATE_HISTORY,
    SELECTION_COST_BPS,
    SOURCE_MODEL_NAME,
    TRAIN,
    VALIDATION,
    CandidateSpec,
    GateSpec,
    PortfolioSpec,
    _backtest,
    _graph_hash,
    _jsonable,
    _pareto_front,
    _run_history,
    _stitched_diagnostic,
    _write_checkpoint,
    build_candidate_graph,
    selection_score,
)


MODEL_NAME = (
    "Arcana_KR_PVGO_ExpectationsAlpha_Quarterly_"
    "2002_2026_CoreRefined_20260906"
)
DEFAULT_OUTPUT = Path(
    "deliverables/kr_pvgo_expectations_alpha_refinement_20260906.json"
)

WEIGHT_CANDIDATES: dict[str, dict[str, float]] = {
    "gap_only": {"gap": 1.0},
    "quality_only": {"quality": 1.0},
    "compression_only": {"compression": 1.0},
    "g60_q20_c20": {"gap": 0.60, "quality": 0.20, "compression": 0.20},
    "g50_q30_c20": {"gap": 0.50, "quality": 0.30, "compression": 0.20},
    "g50_q20_c30": {"gap": 0.50, "quality": 0.20, "compression": 0.30},
    "g45_q30_c25": {"gap": 0.45, "quality": 0.30, "compression": 0.25},
    "g40_q40_c20": {"gap": 0.40, "quality": 0.40, "compression": 0.20},
    "g40_q30_c30": {"gap": 0.40, "quality": 0.30, "compression": 0.30},
    "g40_q20_c40": {"gap": 0.40, "quality": 0.20, "compression": 0.40},
    "g30_q40_c30": {"gap": 0.30, "quality": 0.40, "compression": 0.30},
    "g30_q30_c40": {"gap": 0.30, "quality": 0.30, "compression": 0.40},
    "g20_q40_c40": {"gap": 0.20, "quality": 0.40, "compression": 0.40},
}

WEIGHT_SEARCH_PORTFOLIO = PortfolioSpec(top_percent=30.0, max_positions=50)
GATE_SEARCH_PORTFOLIOS = tuple(
    PortfolioSpec(top_percent=value, max_positions=50)
    for value in (20.0, 25.0, 30.0, 35.0)
)


def _selection_row(
    *,
    key: str,
    label: str,
    portfolio: PortfolioSpec,
    train_result: dict[str, Any],
    validation_result: dict[str, Any],
) -> dict[str, Any]:
    train = train_result["metrics"]
    validation = validation_result["metrics"]
    return {
        "key": key,
        "candidate": label,
        "portfolio": asdict(portfolio),
        "selection_score": selection_score(train, validation),
        "worst_sharpe": min(float(train["sharpe"]), float(validation["sharpe"])),
        "mean_sharpe": mean([float(train["sharpe"]), float(validation["sharpe"])]),
        "mean_cagr": mean([float(train["cagr"]), float(validation["cagr"])]),
        "worst_abs_mdd": max(
            abs(float(train["max_drawdown"])),
            abs(float(validation["max_drawdown"])),
        ),
    }


def _evaluate_group(
    service: FactorLabService,
    *,
    group: str,
    specs: dict[str, CandidateSpec],
    portfolios: tuple[PortfolioSpec, ...],
    output_path: Path,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, tuple[CandidateSpec, PortfolioSpec]]]:
    rows: list[dict[str, Any]] = []
    runtime: dict[str, tuple[CandidateSpec, PortfolioSpec]] = {}
    group_payload: dict[str, Any] = {}
    payload[group] = group_payload
    for label, spec in specs.items():
        graph = build_candidate_graph(
            f"{MODEL_NAME}__research__{group}__{label}",
            spec,
            start_date=EARLY_HISTORY[0],
            end_date=LATE_HISTORY[1],
        )
        print(f"[KR-PVGO-REFINE] history group={group} candidate={label} early", flush=True)
        try:
            early = _run_history(service, graph, period=EARLY_HISTORY)
            print(f"[KR-PVGO-REFINE] history group={group} candidate={label} late", flush=True)
            late = _run_history(service, graph, period=LATE_HISTORY)
        except Exception as exc:
            group_payload[label] = {"spec": asdict(spec), "error": str(exc)}
            _write_checkpoint(output_path, payload)
            continue
        item: dict[str, Any] = {
            "spec": asdict(spec),
            "graph_hash": _graph_hash(graph),
            "history_run_ids": {"early": early.run_id, "late": late.run_id},
            "history_quality": {
                "early": _jsonable(early.quality),
                "late": _jsonable(late.quality),
            },
            "portfolios": {},
        }
        for portfolio in portfolios:
            key = (
                f"{group}__{label}__top{portfolio.top_percent:g}"
                f"__max{portfolio.max_positions}"
            )
            print(f"[KR-PVGO-REFINE] selection {key}", flush=True)
            try:
                train_result, _ = _backtest(
                    service,
                    early.run_id,
                    period=TRAIN,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                validation_result, _ = _backtest(
                    service,
                    late.run_id,
                    period=VALIDATION,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                row = _selection_row(
                    key=key,
                    label=label,
                    portfolio=portfolio,
                    train_result=train_result,
                    validation_result=validation_result,
                )
                item["portfolios"][key] = {
                    "train": train_result,
                    "validation": validation_result,
                    "selection": row,
                }
                rows.append(row)
                runtime[key] = (spec, portfolio)
            except Exception as exc:
                item["portfolios"][key] = {"error": str(exc)}
        group_payload[label] = item
        _write_checkpoint(output_path, payload)
    rows.sort(key=lambda row: float(row["selection_score"]), reverse=True)
    return rows, runtime


def _gate_specs(weights: dict[str, float]) -> dict[str, CandidateSpec]:
    def spec(*gates: GateSpec) -> CandidateSpec:
        return CandidateSpec(
            weights=weights,
            deduplicate_quality=True,
            hard_gates=tuple(gates),
        )

    trend_positive = GateSpec("tr_12_1", "greater_than", 0.0)
    trend_above_minus_10 = GateSpec("tr_12_1", "greater_than", -0.10)
    drawdown_above_minus_40 = GateSpec(
        "mdd1yr_12_1_pct", "greater_than", -40.0
    )
    volatility_under_60 = GateSpec("vol_12_1_ann", "less_than", 0.60)
    return {
        "none": spec(),
        "trend_positive": spec(trend_positive),
        "trend_above_minus_10": spec(trend_above_minus_10),
        "drawdown_above_minus_40": spec(drawdown_above_minus_40),
        "volatility_under_60": spec(volatility_under_60),
        "beta_under_1_5": spec(GateSpec("beta", "less_than", 1.50)),
        "near_52w_high": spec(
            GateSpec("high52w_gap_pct", "greater_than", -30.0)
        ),
        "trend_and_volatility": spec(trend_positive, volatility_under_60),
        "trend_and_drawdown": spec(trend_positive, drawdown_above_minus_40),
    }


def _choose_with_sharpe_constraint(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise RuntimeError("no refinement candidates completed")
    feasible = [row for row in rows if float(row["worst_sharpe"]) > 1.0]
    return max(feasible or rows, key=lambda row: float(row["selection_score"]))


def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source = service.get_experiment_by_name(SOURCE_MODEL_NAME)
    source_before = {
        "name": source.graph.experiment.name,
        "experiment_id": source.experiment_id,
        "graph_hash_sha256": _graph_hash(source.graph),
    }
    payload: dict[str, Any] = {
        "design": {
            "source": source_before,
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "holdout": [str(value) for value in HOLDOUT],
            "selection_cost_bps": SELECTION_COST_BPS,
            "benchmarks": list(BENCHMARKS),
            "adaptive_warning": (
                "This is a second research stage after the first-stage holdout was observed; "
                "the reported holdout is therefore diagnostic, not pristine OOS evidence."
            ),
        }
    }
    weight_specs = {
        label: CandidateSpec(weights=weights, deduplicate_quality=True)
        for label, weights in WEIGHT_CANDIDATES.items()
    }
    weight_rows, weight_runtime = _evaluate_group(
        service,
        group="weight_search",
        specs=weight_specs,
        portfolios=(WEIGHT_SEARCH_PORTFOLIO,),
        output_path=output_path,
        payload=payload,
    )
    weight_winner = _choose_with_sharpe_constraint(weight_rows)
    winner_weight_spec, _ = weight_runtime[str(weight_winner["key"])]
    payload["weight_selection"] = {
        "ranking": weight_rows,
        "pareto_front": _pareto_front(weight_rows),
        "winner": weight_winner,
    }
    _write_checkpoint(output_path, payload)

    gate_rows, gate_runtime = _evaluate_group(
        service,
        group="gate_search",
        specs=_gate_specs(dict(winner_weight_spec.weights)),
        portfolios=GATE_SEARCH_PORTFOLIOS,
        output_path=output_path,
        payload=payload,
    )
    gate_winner = _choose_with_sharpe_constraint(gate_rows)
    winner_spec, winner_portfolio = gate_runtime[str(gate_winner["key"])]
    payload["gate_selection"] = {
        "ranking": gate_rows,
        "pareto_front": _pareto_front(gate_rows),
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in gate_rows
        ),
        "winner": gate_winner,
    }
    _write_checkpoint(output_path, payload)

    print(f"[KR-PVGO-REFINE] frozen winner={gate_winner['key']}", flush=True)
    final_graph = build_candidate_graph(
        MODEL_NAME,
        winner_spec,
        start_date=EARLY_HISTORY[0],
        end_date=LATE_HISTORY[1],
    )
    saved = service.save_experiment(FactorLabExperimentSaveRequestDto(graph=final_graph))
    if saved.experiment_id == source_before["experiment_id"]:
        raise RuntimeError("refined experiment reused the source experiment ID")
    final_early = _run_history(
        service,
        final_graph,
        period=EARLY_HISTORY,
        experiment_id=saved.experiment_id,
    )
    final_late = _run_history(
        service,
        final_graph,
        period=LATE_HISTORY,
        experiment_id=saved.experiment_id,
    )
    holdout, _ = _backtest(
        service,
        final_late.run_id,
        period=HOLDOUT,
        portfolio=winner_portfolio,
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    costs: dict[str, Any] = {}
    for cost in FINAL_COSTS_BPS:
        early_result, early_nav = _backtest(
            service,
            final_early.run_id,
            period=EARLY_HISTORY,
            portfolio=winner_portfolio,
            transaction_cost_bps=cost,
        )
        late_result, late_nav = _backtest(
            service,
            final_late.run_id,
            period=LATE_HISTORY,
            portfolio=winner_portfolio,
            transaction_cost_bps=cost,
        )
        costs[f"cost_{int(cost)}bps"] = {
            "early": early_result,
            "late": late_result,
            "stitched_diagnostic": _stitched_diagnostic(
                early_nav,
                late_nav,
                transaction_cost_bps=cost,
            ),
        }
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )
    source_after_model = service.get_experiment_by_name(SOURCE_MODEL_NAME)
    source_after = {
        "name": source_after_model.graph.experiment.name,
        "experiment_id": source_after_model.experiment_id,
        "graph_hash_sha256": _graph_hash(source_after_model.graph),
    }
    if source_after != source_before:
        raise RuntimeError("source strategy changed during refinement")
    payload["final"] = {
        "model_name": MODEL_NAME,
        "experiment_id": saved.experiment_id,
        "graph_hash_sha256": _graph_hash(final_graph),
        "spec": asdict(winner_spec),
        "portfolio": asdict(winner_portfolio),
        "history_run_ids": {"early": final_early.run_id, "late": final_late.run_id},
        "history_quality": {
            "early": _jsonable(final_early.quality),
            "late": _jsonable(final_late.quality),
        },
        "holdout_50bps": holdout,
        "cost_scenarios": costs,
        "screen_run_id": screen.run_id,
        "screen_quality": _jsonable(screen.quality),
        "screen_top20": [_jsonable(row) for row in screen.rows[:20]],
        "source_preservation": {
            "before": source_before,
            "after": source_after,
            "unchanged": source_before == source_after,
        },
    }
    _write_checkpoint(output_path, payload)
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))

