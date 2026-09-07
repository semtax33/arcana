from __future__ import annotations

"""Optimize the Korean PVGO Expectations Alpha without touching its source model.

Research graphs are executed inline.  Candidate selection uses only the 2002-2012
training segment and the 2016-2020 validation segment.  The 2021-2026 holdout is
evaluated once after the winner is frozen.  Only that frozen winner is saved as a
new FactorLab experiment.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import pandas as pd

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.build_kr_pvgo_expectations_alpha import (
    MODEL_NAME as SOURCE_MODEL_NAME,
    build_graph as build_source_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


MODEL_NAME = (
    "Arcana_KR_PVGO_ExpectationsAlpha_Quarterly_"
    "2002_2026_RiskConfirmed_20260906"
)
EARLY_HISTORY = (date(2002, 4, 1), date(2012, 12, 31))
LATE_HISTORY = (date(2016, 4, 1), date(2026, 9, 4))
TRAIN = EARLY_HISTORY
VALIDATION = (date(2016, 4, 1), date(2020, 12, 31))
HOLDOUT = (date(2021, 1, 4), LATE_HISTORY[1])
SELECTION_COST_BPS = 50.0
FINAL_COSTS_BPS = (20.0, 50.0, 100.0)
BENCHMARKS = ("KOSPI200", "KOSDAQ")
DEFAULT_OUTPUT = Path(
    "deliverables/kr_pvgo_expectations_alpha_optimization_20260906.json"
)


@dataclass(frozen=True)
class GateSpec:
    factor_id: str
    operator: str
    value: float
    financial_basis: str = "ttm"


@dataclass(frozen=True)
class CandidateSpec:
    weights: dict[str, float]
    deduplicate_quality: bool = False
    include_momentum: bool = False
    include_low_volatility: bool = False
    use_shrunk_zscore: bool = False
    min_market_cap_mil: float | None = None
    require_positive_normalized_nopat: bool = False
    hard_gates: tuple[GateSpec, ...] = ()


@dataclass(frozen=True)
class PortfolioSpec:
    top_percent: float
    max_positions: int


BASELINE_WEIGHTS = {
    "gap": 0.40,
    "quality": 0.25,
    "compression": 0.20,
    "raw": 0.15,
}
RISK_CONFIRMED_WEIGHTS = {
    "gap": 0.30,
    "quality": 0.20,
    "compression": 0.15,
    "momentum": 0.20,
    "low_volatility": 0.15,
}

CANDIDATES: dict[str, CandidateSpec] = {
    "baseline": CandidateSpec(weights=BASELINE_WEIGHTS),
    "deduplicated_core": CandidateSpec(
        weights={"gap": 0.45, "quality": 0.30, "compression": 0.25},
        deduplicate_quality=True,
    ),
    "deduplicated_momentum": CandidateSpec(
        weights={
            "gap": 0.35,
            "quality": 0.25,
            "compression": 0.20,
            "momentum": 0.20,
        },
        deduplicate_quality=True,
        include_momentum=True,
    ),
    "deduplicated_low_volatility": CandidateSpec(
        weights={
            "gap": 0.35,
            "quality": 0.25,
            "compression": 0.20,
            "low_volatility": 0.20,
        },
        deduplicate_quality=True,
        include_low_volatility=True,
    ),
    "risk_confirmed": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
    ),
    "risk_confirmed_shrunk": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
        use_shrunk_zscore=True,
    ),
    "risk_confirmed_mcap_50b": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
        min_market_cap_mil=50_000.0,
    ),
    "risk_confirmed_mcap_100b": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
        min_market_cap_mil=100_000.0,
    ),
    "risk_confirmed_mcap_300b": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
        min_market_cap_mil=300_000.0,
    ),
    "risk_confirmed_mcap_100b_positive_nopat": CandidateSpec(
        weights=RISK_CONFIRMED_WEIGHTS,
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
        min_market_cap_mil=100_000.0,
        require_positive_normalized_nopat=True,
    ),
}

PORTFOLIOS = (
    PortfolioSpec(top_percent=10.0, max_positions=30),
    PortfolioSpec(top_percent=20.0, max_positions=50),
    PortfolioSpec(top_percent=30.0, max_positions=50),
    PortfolioSpec(top_percent=40.0, max_positions=50),
)


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def _graph_hash(graph: FactorLabGraphDto) -> str:
    payload = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _factor_pipeline(
    *,
    stem: str,
    factor_id: str,
    direction: str,
    use_shrunk_zscore: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    zscore_id = f"{stem}_sector_z"
    if use_shrunk_zscore:
        zscore_type = "shrunk_zscore"
        zscore_config = {
            "group_key": "sector",
            "min_market_count": 20,
            "min_group_count": 20,
            "shrinkage_strength": 20,
            "direction": direction,
            "clip": 3.0,
        }
    else:
        zscore_type = "zscore"
        zscore_config = {
            "group_by": ["trade_date", "sector"],
            "stddev_method": "population",
            "min_count": 5,
            "zero_std_policy": "invalid",
            "direction": direction,
            "clip": 3.0,
        }
    nodes = [
        {
            "id": input_id,
            "type": "factor_input",
            "config": {
                "factor_id": factor_id,
                "financial_basis": "ttm",
                "missing_policy": "drop",
            },
        },
        {
            "id": winsor_id,
            "type": "winsorize",
            "config": {
                "group_by": ["trade_date"],
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            },
        },
        {"id": zscore_id, "type": zscore_type, "config": zscore_config},
    ]
    return (
        nodes,
        [
            _edge(input_id, winsor_id, "input"),
            _edge(winsor_id, zscore_id, "input"),
        ],
        zscore_id,
    )


def _remove_nodes(payload: dict[str, Any], node_ids: set[str]) -> None:
    payload["nodes"] = [
        node for node in payload["nodes"] if str(node["id"]) not in node_ids
    ]
    payload["edges"] = [
        edge
        for edge in payload["edges"]
        if str(edge["source"]) not in node_ids and str(edge["target"]) not in node_ids
    ]


def _convert_existing_zscores_to_shrunk(payload: dict[str, Any]) -> None:
    for node in payload["nodes"]:
        if node["type"] != "zscore":
            continue
        direction = str(node.get("config", {}).get("direction") or "as_is")
        node["type"] = "shrunk_zscore"
        node["config"] = {
            "group_key": "sector",
            "min_market_count": 20,
            "min_group_count": 20,
            "shrinkage_strength": 20,
            "direction": direction,
            "clip": 3.0,
        }


def build_candidate_graph(
    name: str,
    spec: CandidateSpec,
    *,
    start_date: date,
    end_date: date,
) -> FactorLabGraphDto:
    if name == SOURCE_MODEL_NAME:
        raise ValueError("an optimization candidate must not overwrite the source model")
    if not math.isclose(sum(spec.weights.values()), 1.0, abs_tol=1e-12):
        raise ValueError("candidate weights must sum to one")

    payload = deepcopy(build_source_graph(name=name).model_dump(mode="json"))
    payload["experiment"].update(
        {
            "name": name,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "snapshot_coverage_policy": "allow_missing_inputs",
        }
    )
    if spec.deduplicate_quality:
        _remove_nodes(payload, {"quality_sum", "quality_sector_z"})
    if spec.use_shrunk_zscore:
        _convert_existing_zscores_to_shrunk(payload)

    nodes = payload["nodes"]
    edges = payload["edges"]
    source_nodes = {
        "gap": "gap_sector_z",
        "quality": (
            "roiic_sector_z" if spec.deduplicate_quality else "quality_sector_z"
        ),
        "compression": "compression_sector_z",
        "raw": "raw_sector_z",
    }
    if spec.include_momentum:
        extra_nodes, extra_edges, output = _factor_pipeline(
            stem="momentum",
            factor_id="risk_adj_mom",
            direction="higher_better",
            use_shrunk_zscore=spec.use_shrunk_zscore,
        )
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        source_nodes["momentum"] = output
    if spec.include_low_volatility:
        extra_nodes, extra_edges, output = _factor_pipeline(
            stem="low_volatility",
            factor_id="vol_12_1_ann",
            direction="lower_better",
            use_shrunk_zscore=spec.use_shrunk_zscore,
        )
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        source_nodes["low_volatility"] = output

    weighted = next(node for node in nodes if node["id"] == "expectations_alpha")
    weighted["config"].update(
        {
            "weights": dict(spec.weights),
            "missing_weight_renormalize": True,
            "research_design": (
                "PVGO expectations foundation plus orthogonal price confirmation; "
                "raw PVGO is not embedded in quality when deduplicate_quality is true"
            ),
        }
    )
    edges[:] = [edge for edge in edges if edge["target"] != "expectations_alpha"]
    for handle in spec.weights:
        if handle not in source_nodes:
            raise ValueError(f"no source node configured for weight handle: {handle}")
        edges.append(_edge(source_nodes[handle], "expectations_alpha", handle))

    gate_nodes: list[str] = []
    if spec.min_market_cap_mil is not None:
        nodes.extend(
            [
                {
                    "id": "market_cap_input",
                    "type": "factor_input",
                    "config": {
                        "factor_id": "mcap_mil",
                        "financial_basis": "annual",
                        "missing_policy": "drop",
                    },
                },
                {
                    "id": "market_cap_floor",
                    "type": "constant",
                    "config": {"value": spec.min_market_cap_mil},
                },
                {
                    "id": "market_cap_eligible",
                    "type": "greater_than",
                    "config": {
                        "research_design": "exclude microcaps before final ranking"
                    },
                },
            ]
        )
        edges.extend(
            [
                _edge("market_cap_input", "market_cap_eligible", "left"),
                _edge("market_cap_floor", "market_cap_eligible", "right"),
            ]
        )
        gate_nodes.append("market_cap_eligible")
    if spec.require_positive_normalized_nopat:
        nodes.extend(
            [
                {
                    "id": "normalized_nopat_input",
                    "type": "factor_input",
                    "config": {
                        "factor_id": "normalized_nopat_5y",
                        "financial_basis": "ttm",
                        "missing_policy": "drop",
                    },
                },
                {"id": "zero_nopat_floor", "type": "constant", "config": {"value": 0.0}},
                {
                    "id": "positive_normalized_nopat",
                    "type": "greater_than",
                    "config": {
                        "research_design": (
                            "PVGO is admitted only when normalized operating earning power is positive"
                        )
                    },
                },
            ]
        )
        edges.extend(
            [
                _edge("normalized_nopat_input", "positive_normalized_nopat", "left"),
                _edge("zero_nopat_floor", "positive_normalized_nopat", "right"),
            ]
        )
        gate_nodes.append("positive_normalized_nopat")
    for index, gate in enumerate(spec.hard_gates, start=1):
        if gate.operator not in {"greater_than", "less_than"}:
            raise ValueError(f"unsupported hard-gate operator: {gate.operator}")
        stem = f"hard_gate_{index}"
        input_id = f"{stem}_input"
        floor_id = f"{stem}_threshold"
        condition_id = f"{stem}_condition"
        nodes.extend(
            [
                {
                    "id": input_id,
                    "type": "factor_input",
                    "config": {
                        "factor_id": gate.factor_id,
                        "financial_basis": gate.financial_basis,
                        "missing_policy": "drop",
                    },
                },
                {"id": floor_id, "type": "constant", "config": {"value": gate.value}},
                {
                    "id": condition_id,
                    "type": gate.operator,
                    "config": {
                        "research_design": (
                            f"hard confirmation gate: {gate.factor_id} "
                            f"{gate.operator} {gate.value:g}"
                        )
                    },
                },
            ]
        )
        edges.extend(
            [
                _edge(input_id, condition_id, "left"),
                _edge(floor_id, condition_id, "right"),
            ]
        )
        gate_nodes.append(condition_id)

    if gate_nodes:
        eligibility = gate_nodes[0]
        for index, other in enumerate(gate_nodes[1:], start=1):
            combined = "eligibility_gate" if index == len(gate_nodes) - 1 else f"eligibility_gate_{index}"
            nodes.append(
                {
                    "id": combined,
                    "type": "and",
                    "config": {"research_design": "all economic eligibility conditions"},
                }
            )
            edges.extend(
                [
                    _edge(eligibility, combined, "left"),
                    _edge(other, combined, "right"),
                ]
            )
            eligibility = combined
        if len(gate_nodes) == 1:
            nodes.append(
                {
                    "id": "eligibility_gate",
                    "type": "and",
                    "config": {"research_design": "single gate identity"},
                }
            )
            nodes.append({"id": "eligibility_true", "type": "constant", "config": {"value": 1.0}})
            edges.extend(
                [
                    _edge(eligibility, "eligibility_gate", "left"),
                    _edge("eligibility_true", "eligibility_gate", "right"),
                ]
            )
            eligibility = "eligibility_gate"
        nodes.append(
            {
                "id": "eligible_strategy_score",
                "type": "condition_score",
                "config": {"research_design": "economic eligibility before ranking"},
            }
        )
        edges[:] = [edge for edge in edges if edge["target"] != "final_rank_score"]
        edges.extend(
            [
                _edge(eligibility, "eligible_strategy_score", "condition"),
                _edge("expectations_alpha", "eligible_strategy_score", "score"),
                _edge("eligible_strategy_score", "final_rank_score", "input"),
            ]
        )

    return FactorLabGraphDto(**payload)


def _finite_metric(metrics: dict[str, Any], name: str) -> float | None:
    value = metrics.get(name)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def selection_score(train: dict[str, Any], validation: dict[str, Any]) -> float:
    """Robust pre-holdout score aligned to Sharpe, MDD, and CAGR."""

    train_sharpe = _finite_metric(train, "sharpe")
    validation_sharpe = _finite_metric(validation, "sharpe")
    train_cagr = _finite_metric(train, "cagr")
    validation_cagr = _finite_metric(validation, "cagr")
    train_mdd = _finite_metric(train, "max_drawdown")
    validation_mdd = _finite_metric(validation, "max_drawdown")
    if None in {
        train_sharpe,
        validation_sharpe,
        train_cagr,
        validation_cagr,
        train_mdd,
        validation_mdd,
    }:
        return float("-inf")
    sharpes = [float(train_sharpe), float(validation_sharpe)]
    cagrs = [float(train_cagr), float(validation_cagr)]
    worst_drawdown = max(abs(float(train_mdd)), abs(float(validation_mdd)))
    sharpe_floor_penalty = 0.75 * max(0.0, 1.0 - min(sharpes))
    return (
        min(sharpes)
        + 0.50 * mean(sharpes)
        + 0.35 * mean(cagrs)
        - 0.40 * worst_drawdown
        - sharpe_floor_penalty
    )


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    transaction_cost_bps: float,
) -> tuple[dict[str, Any], pd.Series]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=portfolio.top_percent,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
            market="KR",
            benchmarks=list(BENCHMARKS),
            max_positions=portfolio.max_positions,
            transaction_cost_bps=transaction_cost_bps,
        ),
    )
    nav = pd.Series(
        [point.strategy_nav for point in result.equity_curve],
        index=pd.to_datetime([point.trade_date for point in result.equity_curve]),
        dtype="float64",
    ).sort_index()
    returns = nav.pct_change(fill_method=None).dropna()
    return (
        {
            "period": [str(period[0]), str(period[1])],
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": transaction_cost_bps,
            "metrics": _jsonable(result.summary),
            "newey_west_mean_test": newey_west_mean_test(returns),
            "rebalance_count": len(result.rebalance_history),
            "warnings": list(result.warnings),
        },
        nav,
    )


def _run_history(
    service: FactorLabService,
    graph: FactorLabGraphDto,
    *,
    period: tuple[date, date],
    experiment_id: str | None = None,
) -> Any:
    graph = graph.model_copy(deep=True)
    graph.experiment.start_date = period[0]
    graph.experiment.end_date = period[1]
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=None if experiment_id else graph,
            experiment_id=experiment_id,
            mode="history",
            history_start_date=period[0],
            history_end_date=period[1],
            history_rebalance_frequency="quarterly",
        )
    )


def _candidate_key(label: str, portfolio: PortfolioSpec) -> str:
    return f"{label}__top{portfolio.top_percent:g}__max{portfolio.max_positions}"


def _pareto_front(rows: list[dict[str, Any]]) -> list[str]:
    def dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_values = (
            left["worst_sharpe"],
            left["mean_cagr"],
            -left["worst_abs_mdd"],
        )
        right_values = (
            right["worst_sharpe"],
            right["mean_cagr"],
            -right["worst_abs_mdd"],
        )
        return all(a >= b for a, b in zip(left_values, right_values, strict=True)) and any(
            a > b for a, b in zip(left_values, right_values, strict=True)
        )

    return [
        row["key"]
        for row in rows
        if not any(dominates(other, row) for other in rows if other is not row)
    ]


def _stitched_diagnostic(
    early_nav: pd.Series,
    late_nav: pd.Series,
    *,
    transaction_cost_bps: float,
) -> dict[str, Any]:
    if early_nav.empty or late_nav.empty:
        return {"status": "unavailable", "reason": "one segment has no NAV"}
    early = early_nav / float(early_nav.iloc[0])
    late = late_nav / float(late_nav.iloc[0]) * float(early.iloc[-1])
    combined = pd.concat([early, late]).sort_index()
    combined = combined.loc[~combined.index.duplicated(keep="last")]
    observed_returns = pd.concat(
        [early.pct_change(fill_method=None), late.pct_change(fill_method=None)]
    ).dropna()
    elapsed_years = (LATE_HISTORY[1] - EARLY_HISTORY[0]).days / 365.25
    drawdown = combined / combined.cummax() - 1.0
    volatility = float(observed_returns.std(ddof=1) * np.sqrt(252))
    annualized_mean = float(observed_returns.mean() * 252)
    return {
        "status": "diagnostic_only",
        "transaction_cost_bps": transaction_cost_bps,
        "cumulative_return": float(combined.iloc[-1] - 1.0),
        "elapsed_period_cagr_with_cash_gap": float(
            combined.iloc[-1] ** (1.0 / elapsed_years) - 1.0
        ),
        "max_drawdown_across_observed_segments": float(drawdown.min()),
        "observed_day_volatility": volatility,
        "observed_day_sharpe": (
            annualized_mean / volatility if volatility > 0 else None
        ),
        "cash_gap": ["2013-01-01", "2016-03-31"],
        "cash_gap_return_assumption": 0.0,
        "warning": "not a continuous FactorLab backtest",
    }


def _write_checkpoint(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


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
            "source_model": source_before,
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "holdout": [str(value) for value in HOLDOUT],
            "selection_cost_bps": SELECTION_COST_BPS,
            "holdout_policy": "evaluated once after candidate and portfolio freeze",
            "objective": "maximize robust Sharpe, maximize CAGR, minimize absolute MDD",
        },
        "candidates": {},
    }

    ranking_rows: list[dict[str, Any]] = []
    runtime: dict[str, tuple[Any, Any, CandidateSpec, PortfolioSpec]] = {}
    for label, spec in CANDIDATES.items():
        print(f"[KR-PVGO-OPT] history candidate={label} segment=early", flush=True)
        graph = build_candidate_graph(
            f"{MODEL_NAME}__research__{label}",
            spec,
            start_date=EARLY_HISTORY[0],
            end_date=LATE_HISTORY[1],
        )
        try:
            early_history = _run_history(service, graph, period=EARLY_HISTORY)
            print(f"[KR-PVGO-OPT] history candidate={label} segment=late", flush=True)
            late_history = _run_history(service, graph, period=LATE_HISTORY)
        except Exception as exc:
            payload["candidates"][label] = {
                "spec": asdict(spec),
                "status": "history_failed",
                "error": str(exc),
            }
            _write_checkpoint(output_path, payload)
            continue

        candidate_payload: dict[str, Any] = {
            "spec": asdict(spec),
            "graph_hash": _graph_hash(graph),
            "history": {
                "early_run_id": early_history.run_id,
                "early_quality": _jsonable(early_history.quality),
                "late_run_id": late_history.run_id,
                "late_quality": _jsonable(late_history.quality),
            },
            "portfolios": {},
        }
        for portfolio in PORTFOLIOS:
            key = _candidate_key(label, portfolio)
            print(f"[KR-PVGO-OPT] selection candidate={key}", flush=True)
            try:
                train_result, _ = _backtest(
                    service,
                    early_history.run_id,
                    period=TRAIN,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                validation_result, _ = _backtest(
                    service,
                    late_history.run_id,
                    period=VALIDATION,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                train_metrics = train_result["metrics"]
                validation_metrics = validation_result["metrics"]
                score = selection_score(train_metrics, validation_metrics)
                row = {
                    "key": key,
                    "candidate": label,
                    "portfolio": asdict(portfolio),
                    "selection_score": score,
                    "worst_sharpe": min(
                        float(train_metrics["sharpe"]),
                        float(validation_metrics["sharpe"]),
                    ),
                    "mean_sharpe": mean(
                        [
                            float(train_metrics["sharpe"]),
                            float(validation_metrics["sharpe"]),
                        ]
                    ),
                    "mean_cagr": mean(
                        [
                            float(train_metrics["cagr"]),
                            float(validation_metrics["cagr"]),
                        ]
                    ),
                    "worst_abs_mdd": max(
                        abs(float(train_metrics["max_drawdown"])),
                        abs(float(validation_metrics["max_drawdown"])),
                    ),
                }
                candidate_payload["portfolios"][key] = {
                    "train": train_result,
                    "validation": validation_result,
                    "selection": row,
                }
                ranking_rows.append(row)
                runtime[key] = (early_history, late_history, spec, portfolio)
            except Exception as exc:
                candidate_payload["portfolios"][key] = {
                    "status": "backtest_failed",
                    "error": str(exc),
                }
        payload["candidates"][label] = candidate_payload
        _write_checkpoint(output_path, payload)

    if not ranking_rows:
        raise RuntimeError("no optimization candidate completed")
    ranking_rows.sort(key=lambda row: float(row["selection_score"]), reverse=True)
    winner = ranking_rows[0]
    winner_key = str(winner["key"])
    _, _, winner_spec, winner_portfolio = runtime[winner_key]
    payload["selection"] = {
        "ranking": ranking_rows,
        "pareto_front": _pareto_front(ranking_rows),
        "winner": winner,
    }
    _write_checkpoint(output_path, payload)

    print(f"[KR-PVGO-OPT] frozen winner={winner_key}", flush=True)
    final_graph = build_candidate_graph(
        MODEL_NAME,
        winner_spec,
        start_date=EARLY_HISTORY[0],
        end_date=LATE_HISTORY[1],
    )
    saved = service.save_experiment(
        FactorLabExperimentSaveRequestDto(graph=final_graph)
    )
    if saved.experiment_id == source_before["experiment_id"]:
        raise RuntimeError("optimized experiment reused the source experiment ID")
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
        raise RuntimeError("source strategy changed during optimization")

    payload["final"] = {
        "model_name": MODEL_NAME,
        "experiment_id": saved.experiment_id,
        "graph_hash_sha256": _graph_hash(final_graph),
        "winner_key": winner_key,
        "spec": asdict(winner_spec),
        "portfolio": asdict(winner_portfolio),
        "history_run_ids": {
            "early": final_early.run_id,
            "late": final_late.run_id,
        },
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
        "limitations": [
            "2013-2015 remains absent from FactorLab PIT snapshots, so results are segmented.",
            "Delisted-security history is incomplete; survivor bias is not fully eliminated.",
            "Candidate selection is multiple testing; holdout is reported once and was not used to retune.",
            "The backtest has no ADV, spread, market-impact, or capacity model beyond flat bps costs.",
        ],
    }
    _write_checkpoint(output_path, payload)
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
