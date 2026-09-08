from __future__ import annotations

"""Test a Soros-reflexivity price/target-price extension in Factor Lab.

The frozen Minervini + Zweig + PVGO model is the required economic foundation.
Every research candidate is evaluated on the same target-price-covered sample,
so an apparent improvement cannot come merely from changing data coverage.

Two competing interpretations are tested:

* ``target_gap``: a low price/target-price ratio leaves analyst-implied upside;
* ``price_lead``: a high ratio means price is leading a slow-moving consensus,
  which may be the recognition leg of a reflexive feedback loop.

The 2024-2026 holdout is not used to choose direction, weight, or portfolio.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Literal

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.build_us_minervini_zweig_pvgo_cagr import (
    CANDIDATES as BASE_CANDIDATES,
    build_composite_graph as build_base_composite_graph,
    build_minervini_module_graph,
    build_pvgo_module_graph,
    build_zweig_module_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import (
    PortfolioSpec,
    _jsonable,
    _write_checkpoint,
)


BASE_MODEL_NAME = (
    "Arcana_US_Minervini_Zweig_PVGO_CAGR_Quarterly_2016_2026_20260907__Final"
)
MODEL_STEM = (
    "Arcana_US_Minervini_Zweig_PVGO_SorosReflexivity_CAGR_"
    "Quarterly_2017_2026_20260908"
)
FINAL_MODEL_NAME = MODEL_STEM + "__Final"
LIVE_SCREEN_MODEL_NAME = MODEL_STEM + "__LiveScreen"
OUTPUT = Path("deliverables/us_soros_price_target_reflexivity_cagr_20260908.json")

FULL = (date(2017, 10, 2), date(2026, 9, 4))
TRAIN = (date(2017, 10, 2), date(2021, 12, 31))
VALIDATION = (date(2022, 1, 3), date(2023, 12, 29))
PRE_HOLDOUT = (TRAIN[0], VALIDATION[1])
HOLDOUT = (date(2024, 1, 2), FULL[1])
SELECTION_COST_BPS = 50.0
COST_SENSITIVITY_BPS = (20.0, 50.0, 100.0)
TARGET_WEIGHTS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
NON_FINANCIAL_GICS_SECTORS = (
    "10",
    "15",
    "20",
    "25",
    "30",
    "35",
    "45",
    "50",
    "55",
    "60",
)


@dataclass(frozen=True)
class ReflexivitySpec:
    style: Literal["control", "additive", "interaction"]
    direction: Literal["target_gap", "price_lead"]
    target_weight: float = 0.0


def _candidate_specs() -> dict[str, ReflexivitySpec]:
    specs = {
        "matched_control": ReflexivitySpec(
            style="control",
            direction="target_gap",
            target_weight=0.0,
        )
    }
    for direction in ("target_gap", "price_lead"):
        for weight in TARGET_WEIGHTS:
            specs[f"{direction}__w{int(weight * 100):02d}"] = ReflexivitySpec(
                style="additive",
                direction=direction,
                target_weight=weight,
            )
        specs[f"{direction}__interaction"] = ReflexivitySpec(
            style="interaction",
            direction=direction,
        )
    return specs


CANDIDATES = _candidate_specs()
PORTFOLIOS = (
    PortfolioSpec(top_percent=3.0, max_positions=10),
    PortfolioSpec(top_percent=5.0, max_positions=20),
    PortfolioSpec(top_percent=10.0, max_positions=30),
)


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _experiment(name: str, *, factor_data_mode: str) -> dict[str, Any]:
    return {
        "name": name,
        "market": "US",
        "start_date": FULL[0].isoformat(),
        "end_date": FULL[1].isoformat(),
        "factor_data_mode": factor_data_mode,
        "snapshot_coverage_policy": "allow_missing_inputs",
        "universe": {
            "type": "market",
            "sector_codes": list(NON_FINANCIAL_GICS_SECTORS),
            "industry_group_codes": [],
        },
        "rebalance": {
            "frequency": "quarterly",
            "signal_lag_days": 1,
            "transaction_cost_bps": SELECTION_COST_BPS,
        },
    }


def build_target_price_module_graph(
    direction: Literal["target_gap", "price_lead"],
    *,
    name: str | None = None,
) -> FactorLabGraphDto:
    if direction not in {"target_gap", "price_lead"}:
        raise ValueError("direction must be target_gap or price_lead")
    score_direction = "lower_better" if direction == "target_gap" else "higher_better"
    module_name = name or MODEL_STEM + f"__{direction}Module"
    nodes = [
        {
            "id": "price_to_target_input",
            "type": "factor_input",
            "config": {
                "factor_id": "us_price_to_target_price",
                "financial_basis": "ttm",
                "missing_policy": "drop",
            },
        },
        {
            "id": "price_to_target_winsor",
            "type": "winsorize",
            "config": {
                "group_by": ["trade_date"],
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            },
        },
        {
            "id": "price_to_target_score",
            "type": "shrunk_zscore",
            "config": {
                "group_key": "sector",
                "min_market_count": 20,
                "min_group_count": 20,
                "shrinkage_strength": 20,
                "direction": score_direction,
                "clip": 3.0,
                "research_design": (
                    "target_gap ranks analyst-implied upside; price_lead ranks market "
                    "recognition ahead of a slow-moving target-price consensus"
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
            },
        },
    ]
    edges = [
        _edge("price_to_target_input", "price_to_target_winsor", "input"),
        _edge("price_to_target_winsor", "price_to_target_score", "input"),
        _edge("price_to_target_score", "module_rank_score", "input"),
    ]
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(module_name, factor_data_mode="point_in_time_snapshot"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "module_rank_score"},
    )


def _lab_factor_id(run_id: str) -> str:
    return f"lab_{run_id.replace('-', '')}"


def build_reflexivity_graph(
    name: str,
    spec: ReflexivitySpec,
    *,
    base_run_id: str,
    target_run_ids: dict[str, str],
) -> FactorLabGraphDto:
    if spec.direction not in target_run_ids:
        raise ValueError(f"missing target module for {spec.direction}")
    if spec.style == "additive" and not (0.0 < spec.target_weight < 1.0):
        raise ValueError("additive target weight must be between zero and one")
    if spec.style != "additive" and not math.isclose(spec.target_weight, 0.0):
        raise ValueError("only additive candidates may set target_weight")

    nodes: list[dict[str, Any]] = [
        {
            "id": "base_strategy_input",
            "type": "factor_input",
            "config": {
                "factor_id": _lab_factor_id(base_run_id),
                "financial_basis": "lab",
                "missing_policy": "drop",
            },
        },
        {
            "id": "target_price_input",
            "type": "factor_input",
            "config": {
                "factor_id": _lab_factor_id(target_run_ids[spec.direction]),
                "financial_basis": "lab",
                "missing_policy": "drop",
            },
        },
    ]
    edges: list[dict[str, str]] = []

    if spec.style in {"control", "additive"}:
        target_weight = spec.target_weight
        nodes.append(
            {
                "id": "reflexivity_score",
                "type": "weighted_score",
                "config": {
                    "weights": {
                        "base": 1.0 - target_weight,
                        "target": target_weight,
                    },
                    # Inner-join both inputs.  The zero-weight control therefore
                    # has exactly the same target-price-covered sample.
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "matched control or a bounded target-price sleeve on top of "
                        "the frozen Minervini-Zweig-PVGO score"
                    ),
                },
            }
        )
        edges.extend(
            [
                _edge("base_strategy_input", "reflexivity_score", "base"),
                _edge("target_price_input", "reflexivity_score", "target"),
            ]
        )
    else:
        nodes.append(
            {
                "id": "reflexivity_score",
                "type": "mul",
                "config": {
                    "research_design": (
                        "non-linear reflexivity interaction: both the economic base "
                        "and target-price recognition must rank highly"
                    )
                },
            }
        )
        edges.extend(
            [
                _edge("base_strategy_input", "reflexivity_score", "left"),
                _edge("target_price_input", "reflexivity_score", "right"),
            ]
        )

    nodes.append(
        {
            "id": "final_rank_score",
            "type": "dense_score",
            "config": {
                "group_by": ["trade_date"],
                "order": "desc",
                "scale": "0_100",
            },
        }
    )
    edges.append(_edge("reflexivity_score", "final_rank_score", "input"))
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(name, factor_data_mode="raw"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )


def selection_tuple(
    *,
    train: dict[str, Any],
    validation: dict[str, Any],
    pre_holdout: dict[str, Any],
) -> tuple[tuple[float, float, float, float], bool, list[str]]:
    """Return a literal CAGR-first ordering plus minimal robustness guardrails."""

    segments = {"train": train, "validation": validation}
    failures: list[str] = []
    for label, metrics in segments.items():
        if float(metrics["cagr"]) <= 0.0:
            failures.append(f"{label} CAGR is not positive")
        sharpe = metrics.get("sharpe")
        if sharpe is None or float(sharpe) < 0.25:
            failures.append(f"{label} Sharpe is below 0.25")
        if abs(float(metrics["max_drawdown"])) > 0.55:
            failures.append(f"{label} absolute MDD exceeds 55%")
    worst_cagr = min(float(metrics["cagr"]) for metrics in segments.values())
    worst_sharpe = min(float(metrics["sharpe"]) for metrics in segments.values())
    worst_mdd = max(abs(float(metrics["max_drawdown"])) for metrics in segments.values())
    ordering = (
        float(pre_holdout["cagr"]),
        worst_cagr,
        worst_sharpe,
        -worst_mdd,
    )
    return ordering, not failures, failures


def _save_and_run_history(
    service: FactorLabService,
    graph: FactorLabGraphDto,
) -> tuple[str, Any]:
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])
    experiment = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    run = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="history",
            history_start_date=FULL[0],
            history_end_date=FULL[1],
            history_rebalance_frequency="quarterly",
        )
    )
    return experiment.experiment_id, run


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    cost_bps: float,
) -> dict[str, Any]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=portfolio.top_percent,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
            market="US",
            benchmarks=["US_NASDAQ", "US_SP500"],
            max_positions=portfolio.max_positions,
            transaction_cost_bps=cost_bps,
        ),
    )
    return {
        "period": [str(value) for value in period],
        "portfolio": asdict(portfolio),
        "transaction_cost_bps": cost_bps,
        "metrics": _jsonable(result.summary),
        "annual_returns": [_jsonable(value) for value in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def _completed_run(payload: dict[str, Any], section: str, key: str) -> str | None:
    row = ((payload.get(section) or {}).get(key) or {})
    if row.get("status") != "completed":
        return None
    value = str(row.get("run_id") or "")
    return value or None


def _set_screen_as_of(graph: FactorLabGraphDto, as_of_date: date) -> FactorLabGraphDto:
    """Return a copy whose screen resolver is allowed to reach the requested date."""

    payload = graph.model_dump(mode="json")
    payload["experiment"]["end_date"] = as_of_date.isoformat()
    return FactorLabGraphDto(**payload)


def _screen_graph(
    service: FactorLabService,
    graph: FactorLabGraphDto,
    *,
    as_of_date: date,
):
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=_set_screen_as_of(graph, as_of_date),
            mode="screen",
        )
    )


def _run_live_screen(
    service: FactorLabService,
    *,
    selected_spec: ReflexivitySpec,
    selected_portfolio: PortfolioSpec,
    as_of_date: date,
) -> dict[str, Any]:
    """Recompute every raw module before composing an as-of-date screen.

    A composite backed by quarterly history runs naturally stops at the last
    scheduled quarter end.  Screening the raw modules first gives every
    derived lab factor a fresh row on the effective current market date.
    """

    module_graphs = {
        "pvgo": build_pvgo_module_graph(MODEL_STEM + "__LivePVGOModule"),
        "minervini": build_minervini_module_graph(
            MODEL_STEM + "__LiveMinerviniModule"
        ),
        "zweig": build_zweig_module_graph(MODEL_STEM + "__LiveZweigModule"),
    }
    base_module_runs: dict[str, str] = {}
    module_payload: dict[str, Any] = {}
    for key, graph in module_graphs.items():
        result = _screen_graph(service, graph, as_of_date=as_of_date)
        base_module_runs[key] = result.run_id
        module_payload[key] = {
            "run_id": result.run_id,
            "factor_id": result.factor_id,
            "status": result.status,
            "quality": _jsonable(result.quality),
        }

    base_graph = build_base_composite_graph(
        MODEL_STEM + "__LiveBaseComposite",
        BASE_CANDIDATES["pvgo_tilt"],
        module_run_ids=base_module_runs,
    )
    base_screen = _screen_graph(service, base_graph, as_of_date=as_of_date)
    module_payload["base_composite"] = {
        "run_id": base_screen.run_id,
        "factor_id": base_screen.factor_id,
        "status": base_screen.status,
        "quality": _jsonable(base_screen.quality),
    }

    target_graph = build_target_price_module_graph(
        selected_spec.direction,
        name=MODEL_STEM + "__LiveTargetPriceModule",
    )
    target_screen = _screen_graph(service, target_graph, as_of_date=as_of_date)
    module_payload["target_price"] = {
        "direction": selected_spec.direction,
        "run_id": target_screen.run_id,
        "factor_id": target_screen.factor_id,
        "status": target_screen.status,
        "quality": _jsonable(target_screen.quality),
    }

    live_graph = build_reflexivity_graph(
        LIVE_SCREEN_MODEL_NAME,
        selected_spec,
        base_run_id=base_screen.run_id,
        target_run_ids={selected_spec.direction: target_screen.run_id},
    )
    live_graph = _set_screen_as_of(live_graph, as_of_date)
    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=live_graph)
    )
    final_screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )
    effective_date = final_screen.quality.date_coverage.get("max")
    eligible_count = int(final_screen.quality.security_coverage)
    position_count = min(
        selected_portfolio.max_positions,
        max(
            1,
            math.ceil(eligible_count * selected_portfolio.top_percent / 100.0),
        ),
    )
    return {
        "classification": "on_demand_diagnostic_not_quarterly_rebalance_signal",
        "requested_as_of_date": str(as_of_date),
        "effective_trade_date": str(effective_date) if effective_date else None,
        "experiment_id": saved.experiment_id,
        "run_id": final_screen.run_id,
        "factor_id": final_screen.factor_id,
        "status": final_screen.status,
        "quality": _jsonable(final_screen.quality),
        "portfolio": asdict(selected_portfolio),
        "eligible_row_count": eligible_count,
        "selected_position_count": position_count,
        "module_runs": module_payload,
        "top20": [_jsonable(row) for row in final_screen.rows[:20]],
        "selected_positions": [
            _jsonable(row) for row in final_screen.rows[:position_count]
        ],
        "note": (
            "The production strategy remains quarterly.  This on-demand screen "
            "recomputes raw modules between scheduled quarter ends and therefore "
            "is a freshness diagnostic unless separately backtested at that frequency."
        ),
    }


def _official_quarterly_screen(
    service: FactorLabService,
    run: Any,
    portfolio: PortfolioSpec,
) -> dict[str, Any]:
    effective_date = run.quality.date_coverage.get("max")
    preview = service.preview_node(run.run_id, limit=1000)
    eligible_count = sum(
        str(row.trade_date) == str(effective_date) and row.is_valid
        for row in preview.rows
    )
    position_count = min(
        portfolio.max_positions,
        max(1, math.ceil(eligible_count * portfolio.top_percent / 100.0)),
    )
    return {
        "classification": "backtest_aligned_quarterly_signal",
        "effective_trade_date": str(effective_date) if effective_date else None,
        "source_history_run_id": run.run_id,
        "quality": _jsonable(run.quality),
        "portfolio": asdict(portfolio),
        "eligible_row_count": eligible_count,
        "selected_position_count": position_count,
        "top20": [_jsonable(row) for row in run.rows[:20]],
        "selected_positions": [_jsonable(row) for row in run.rows[:position_count]],
        "note": (
            "This is the last scheduled quarterly signal and remains the official "
            "portfolio until the next quarterly rebalance."
        ),
    }


def refresh_as_of_screen(
    output_path: Path = OUTPUT,
    *,
    as_of_date: date,
    stability_runs: int = 2,
) -> dict[str, Any]:
    if stability_runs < 1 or stability_runs > 3:
        raise ValueError("stability_runs must be between 1 and 3")
    if not output_path.exists():
        raise FileNotFoundError(
            f"research output does not exist; run the full workflow first: {output_path}"
        )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    selection = payload.get("selection") or {}
    if not selection.get("spec"):
        raise RuntimeError("research output has no frozen selection")
    selected_spec = ReflexivitySpec(**selection["spec"])
    selected_portfolio = PortfolioSpec(**selection["portfolio"])
    service = FactorLabService()
    screens = [
        _run_live_screen(
            service,
            selected_spec=selected_spec,
            selected_portfolio=selected_portfolio,
            as_of_date=as_of_date,
        )
        for _ in range(stability_runs)
    ]
    signatures = [
        {
            "run_id": screen["run_id"],
            "effective_trade_date": screen["effective_trade_date"],
            "eligible_row_count": screen["eligible_row_count"],
            "selected_tickers": [
                str(row.get("ticker") or row.get("security_id") or "")
                for row in screen["selected_positions"]
            ],
        }
        for screen in screens
    ]
    comparable_signatures = [
        (
            row["effective_trade_date"],
            row["eligible_row_count"],
            tuple(row["selected_tickers"]),
        )
        for row in signatures
    ]
    stable = len(set(comparable_signatures)) == 1
    screen = screens[-1]
    screen["source_stability"] = {
        "stable": stable,
        "run_count": stability_runs,
        "observations": signatures,
        "action": (
            "screen may be consumed as a repeatable diagnostic"
            if stable
            else "wait for source ingestion to finish and rerun --screen-only"
        ),
    }
    history_run_id = str((payload.get("final") or {}).get("history_run_id") or "")
    if not history_run_id:
        raise RuntimeError("research output has no final history run")
    official = _official_quarterly_screen(
        service,
        service.get_run(history_run_id),
        selected_portfolio,
    )
    payload.setdefault("final", {})["as_of_screen"] = screen
    payload["final"]["official_quarterly_screen"] = official
    # Keep the convenient top-level aliases pointed at the genuinely refreshed
    # live screen rather than the final history graph's last quarter-end row.
    payload["final"]["screen_experiment_id"] = screen["experiment_id"]
    payload["final"]["screen_run_id"] = screen["run_id"]
    payload["final"]["screen_quality"] = screen["quality"]
    payload["final"]["screen_top20"] = screen["top20"]
    _write_checkpoint(output_path, payload)
    return payload


def run(
    output_path: Path = OUTPUT,
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    service = FactorLabService()
    payload: dict[str, Any] = {
        "status": "researching",
        "design": {
            "base_model": BASE_MODEL_NAME,
            "factor": "us_price_to_target_price = close / point-in-time consensus target price",
            "hypotheses": {
                "target_gap": "lower price/target is better: analyst-implied upside remains",
                "price_lead": (
                    "higher price/target is better: market price leads a slow-moving consensus"
                ),
                "interaction": (
                    "target-price score multiplies the Minervini-Zweig-PVGO base score"
                ),
            },
            "matched_sample_policy": (
                "all candidates, including the zero-weight control, require the same "
                "valid target-price observation"
            ),
            "full": [str(value) for value in FULL],
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "pre_holdout": [str(value) for value in PRE_HOLDOUT],
            "holdout": [str(value) for value in HOLDOUT],
            "holdout_policy": "excluded from direction, weight, and portfolio selection",
            "selection_rule": (
                "maximize pre-holdout CAGR; tie-break by worst segment CAGR, worst "
                "Sharpe, then MDD after guardrails"
            ),
            "selection_cost_bps": SELECTION_COST_BPS,
            "target_weight_grid": list(TARGET_WEIGHTS),
            "portfolios": [asdict(value) for value in PORTFOLIOS],
            "candidate_count": len(CANDIDATES),
            "signal_lag_days": 1,
        },
        "modules": {},
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["modules"].update(previous.get("modules") or {})
        payload["candidates"].update(previous.get("candidates") or {})

    base_run_id = _completed_run(payload, "modules", "base")
    if base_run_id:
        try:
            if service.get_run(base_run_id).status != "completed":
                base_run_id = None
        except Exception:
            base_run_id = None
    if not base_run_id:
        base = service.get_experiment_by_name(BASE_MODEL_NAME)
        print("[SOROS-PT] materializing frozen base", flush=True)
        base_run = service.run_graph(
            FactorLabRunRequestDto(
                experiment_id=base.experiment_id,
                mode="history",
                history_start_date=FULL[0],
                history_end_date=FULL[1],
                history_rebalance_frequency="quarterly",
            )
        )
        base_run_id = base_run.run_id
        payload["modules"]["base"] = {
            "status": "completed",
            "experiment_id": base.experiment_id,
            "run_id": base_run_id,
            "graph_hash": base_run.graph_hash,
            "quality": _jsonable(base_run.quality),
        }
        _write_checkpoint(output_path, payload)

    target_run_ids: dict[str, str] = {}
    for direction in ("target_gap", "price_lead"):
        cached = _completed_run(payload, "modules", direction)
        if cached:
            try:
                if service.get_run(cached).status == "completed":
                    target_run_ids[direction] = cached
                    continue
            except Exception:
                pass
        print(f"[SOROS-PT] materializing {direction} target module", flush=True)
        graph = build_target_price_module_graph(direction)
        experiment_id, target_run = _save_and_run_history(service, graph)
        target_run_ids[direction] = target_run.run_id
        payload["modules"][direction] = {
            "status": "completed",
            "experiment_id": experiment_id,
            "run_id": target_run.run_id,
            "graph_hash": target_run.graph_hash,
            "quality": _jsonable(target_run.quality),
        }
        _write_checkpoint(output_path, payload)

    ranking: list[dict[str, Any]] = []
    for key, spec in CANDIDATES.items():
        cached = (payload.get("candidates") or {}).get(key) or {}
        if cached.get("status") == "completed":
            ranking.extend(cached.get("selection_rows") or [])
            continue
        print(f"[SOROS-PT] materializing candidate {key}", flush=True)
        graph = build_reflexivity_graph(
            MODEL_STEM + f"__Research__{key}",
            spec,
            base_run_id=base_run_id,
            target_run_ids=target_run_ids,
        )
        experiment_id, candidate_run = _save_and_run_history(service, graph)
        rows: list[dict[str, Any]] = []
        for portfolio in PORTFOLIOS:
            train = _backtest(
                service,
                candidate_run.run_id,
                period=TRAIN,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            validation = _backtest(
                service,
                candidate_run.run_id,
                period=VALIDATION,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            pre_holdout = _backtest(
                service,
                candidate_run.run_id,
                period=PRE_HOLDOUT,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            ordering, feasible, failures = selection_tuple(
                train=train["metrics"],
                validation=validation["metrics"],
                pre_holdout=pre_holdout["metrics"],
            )
            row = {
                "key": f"{key}__top{portfolio.top_percent:g}__max{portfolio.max_positions}",
                "candidate": key,
                "spec": asdict(spec),
                "portfolio": asdict(portfolio),
                "selection_ordering": list(ordering),
                "feasible": feasible,
                "guardrail_failures": failures,
                "train": train,
                "validation": validation,
                "pre_holdout": pre_holdout,
            }
            rows.append(row)
            ranking.append(row)
        payload["candidates"][key] = {
            "status": "completed",
            "spec": asdict(spec),
            "experiment_id": experiment_id,
            "run_id": candidate_run.run_id,
            "graph_hash": candidate_run.graph_hash,
            "quality": _jsonable(candidate_run.quality),
            "selection_rows": rows,
        }
        _write_checkpoint(output_path, payload)

    enhanced = [row for row in ranking if row["candidate"] != "matched_control"]
    feasible_enhanced = [row for row in enhanced if row["feasible"]]
    selection_pool = feasible_enhanced or enhanced
    if not selection_pool:
        raise RuntimeError("no completed enhanced candidate/portfolio combinations")
    selected = max(
        selection_pool,
        key=lambda row: tuple(row["selection_ordering"]),
    )

    same_portfolio_controls = [
        row
        for row in ranking
        if row["candidate"] == "matched_control"
        and row["portfolio"] == selected["portfolio"]
    ]
    if len(same_portfolio_controls) != 1:
        raise RuntimeError("matched control for selected portfolio is unavailable")
    matched_control = same_portfolio_controls[0]
    pre_holdout_cagr_delta = (
        float(selected["pre_holdout"]["metrics"]["cagr"])
        - float(matched_control["pre_holdout"]["metrics"]["cagr"])
    )

    selected_spec = CANDIDATES[str(selected["candidate"])]
    selected_portfolio = PortfolioSpec(**selected["portfolio"])
    final_graph = build_reflexivity_graph(
        FINAL_MODEL_NAME,
        selected_spec,
        base_run_id=base_run_id,
        target_run_ids=target_run_ids,
    )
    final_experiment_id, final_run = _save_and_run_history(service, final_graph)
    final_full = _backtest(
        service,
        final_run.run_id,
        period=FULL,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    final_holdout = _backtest(
        service,
        final_run.run_id,
        period=HOLDOUT,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    control_run_id = str(payload["candidates"]["matched_control"]["run_id"])
    control_full = _backtest(
        service,
        control_run_id,
        period=FULL,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    control_holdout = _backtest(
        service,
        control_run_id,
        period=HOLDOUT,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    costs = {
        f"{cost:g}": _backtest(
            service,
            final_run.run_id,
            period=FULL,
            portfolio=selected_portfolio,
            cost_bps=cost,
        )
        for cost in COST_SENSITIVITY_BPS
    }
    live_screen = _run_live_screen(
        service,
        selected_spec=selected_spec,
        selected_portfolio=selected_portfolio,
        as_of_date=as_of_date or date.today(),
    )
    official_quarterly_screen = _official_quarterly_screen(
        service,
        final_run,
        selected_portfolio,
    )

    payload.update(
        {
            "status": "completed",
            "selection": {
                "candidate": selected["candidate"],
                "spec": asdict(selected_spec),
                "portfolio": selected["portfolio"],
                "feasible": selected["feasible"],
                "guardrail_failures": selected["guardrail_failures"],
                "train": selected["train"],
                "validation": selected["validation"],
                "pre_holdout": selected["pre_holdout"],
                "matched_control_same_portfolio": matched_control,
                "pre_holdout_cagr_delta_vs_matched_control": pre_holdout_cagr_delta,
                "promotion_on_pre_holdout": pre_holdout_cagr_delta > 0.0,
            },
            "final": {
                "classification": (
                    "promoted_reflexivity_extension"
                    if pre_holdout_cagr_delta > 0.0
                    else "research_shadow_do_not_replace_base"
                ),
                "model_name": FINAL_MODEL_NAME,
                "experiment_id": final_experiment_id,
                "history_run_id": final_run.run_id,
                "screen_experiment_id": live_screen["experiment_id"],
                "screen_run_id": live_screen["run_id"],
                "graph_hash": final_run.graph_hash,
                "quality": _jsonable(final_run.quality),
                "full_50bps": final_full,
                "holdout_50bps": final_holdout,
                "matched_control_full_50bps": control_full,
                "matched_control_holdout_50bps": control_holdout,
                "cost_sensitivity": costs,
                "screen_quality": live_screen["quality"],
                "screen_top20": live_screen["top20"],
                "as_of_screen": live_screen,
                "official_quarterly_screen": official_quarterly_screen,
            },
            "limitations": [
                "Target prices are analyst opinions and may follow price instead of leading it.",
                "The target-price-covered sample is materially smaller than the base universe.",
                "Delisted-security history is incomplete, so survivorship bias remains.",
                "The holdout is evaluated after selection and is not recycled into the weights.",
                "This is a research backtest, not a forecast or investment recommendation.",
            ],
        }
    )
    _write_checkpoint(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--stability-runs", type=int, default=2)
    args = parser.parse_args()
    if args.screen_only:
        payload = refresh_as_of_screen(
            args.output,
            as_of_date=args.as_of_date,
            stability_runs=args.stability_runs,
        )
    else:
        payload = run(args.output, as_of_date=args.as_of_date)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "selection": payload["selection"]["candidate"],
                "classification": payload["final"]["classification"],
                "experiment_id": payload["final"]["experiment_id"],
                "history_run_id": payload["final"]["history_run_id"],
                "screen_requested_as_of": payload["final"]["as_of_screen"][
                    "requested_as_of_date"
                ],
                "screen_effective_date": payload["final"]["as_of_screen"][
                    "effective_trade_date"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
