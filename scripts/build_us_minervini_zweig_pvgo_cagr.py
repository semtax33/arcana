from __future__ import annotations

"""Research and materialize a US Minervini + Zweig + PVGO strategy.

The workflow is deliberately modular:

* Minervini module: price persistence, proximity to the 52-week high, and a
  50/150/200-day moving-average hierarchy.
* Zweig module: realized earnings/sales growth plus earnings surprise and
  analyst-revision confirmation.
* PVGO module: the gap between justified and market-implied growth, improving
  earning power relative to price, and incremental returns above WACC.

Each module is first materialized by FactorLab from point-in-time snapshots.
The bounded weight/portfolio search then combines only those immutable module
outputs.  Candidate selection uses 2016-2023 data and does not read the
2024-2026 holdout until a single specification has been frozen.

Run from the project root with::

    python -m scripts.build_us_minervini_zweig_pvgo_cagr
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.discover_kr_pvgo_multifactor_strategy import (
    CandidateSpec as PvgoCandidateSpec,
    build_graph as build_pvgo_candidate_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import (
    PortfolioSpec,
    _graph_hash,
    _jsonable,
    _write_checkpoint,
)


MODEL_STEM = "Arcana_US_Minervini_Zweig_PVGO_CAGR_Quarterly_2016_2026_20260907"
FINAL_MODEL_NAME = MODEL_STEM + "__Final"
OUTPUT = Path("deliverables/us_minervini_zweig_pvgo_cagr_20260907.json")

FULL = (date(2016, 1, 4), date(2026, 9, 4))
TRAIN = (date(2016, 1, 4), date(2019, 12, 31))
VALIDATION = (date(2020, 1, 2), date(2023, 12, 29))
PRE_HOLDOUT = (TRAIN[0], VALIDATION[1])
HOLDOUT = (date(2024, 1, 2), FULL[1])
SELECTION_COST_BPS = 50.0
COST_SENSITIVITY_BPS = (20.0, 50.0, 100.0)
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

MINERVINI_WEIGHTS = {
    "return_12m": 0.30,
    "return_6m": 0.20,
    "return_3m": 0.10,
    "near_high": 0.15,
    "risk_adjusted_momentum": 0.25,
}
ZWEIG_WEIGHTS = {
    "eps_growth": 0.25,
    "sales_growth": 0.15,
    "eps_surprise": 0.15,
    "eps_revision": 0.20,
    "eps_revision_acceleration": 0.15,
    "eps_dispersion": 0.10,
}
PVGO_RELATIVE_WEIGHTS = {"gap": 0.45, "quality": 0.30, "compression": 0.25}


@dataclass(frozen=True)
class CompositeSpec:
    pvgo: float
    minervini: float
    zweig: float

    def weights(self) -> dict[str, float]:
        return {
            "pvgo": self.pvgo,
            "minervini": self.minervini,
            "zweig": self.zweig,
        }


# This is a bounded, theory-led search rather than a free-form optimizer.
CANDIDATES: dict[str, CompositeSpec] = {
    "balanced": CompositeSpec(pvgo=0.30, minervini=0.40, zweig=0.30),
    "trend_tilt": CompositeSpec(pvgo=0.25, minervini=0.45, zweig=0.30),
    "earnings_tilt": CompositeSpec(pvgo=0.25, minervini=0.35, zweig=0.40),
    "pvgo_tilt": CompositeSpec(pvgo=0.40, minervini=0.35, zweig=0.25),
}
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


def _factor_pipeline(
    *,
    stem: str,
    factor_id: str,
    direction: str,
    financial_basis: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    score_id = f"{stem}_score"
    return (
        [
            {
                "id": input_id,
                "type": "factor_input",
                "config": {
                    "factor_id": factor_id,
                    "financial_basis": financial_basis,
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
            {
                "id": score_id,
                "type": "shrunk_zscore",
                "config": {
                    "group_key": "sector",
                    "min_market_count": 20,
                    "min_group_count": 20,
                    "shrinkage_strength": 20,
                    "direction": direction,
                    "clip": 3.0,
                },
            },
        ],
        [
            _edge(input_id, winsor_id, "input"),
            _edge(winsor_id, score_id, "input"),
        ],
        score_id,
    )


def _and_chain(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    conditions: list[str],
    *,
    stem: str,
) -> str:
    if len(conditions) < 2:
        raise ValueError("an AND chain requires at least two conditions")
    result = conditions[0]
    for index, condition in enumerate(conditions[1:], start=1):
        node_id = f"{stem}_{index}"
        nodes.append(
            {
                "id": node_id,
                "type": "and",
                "config": {"research_design": "all precommitted conditions must pass"},
            }
        )
        edges.extend(
            [
                _edge(result, node_id, "left"),
                _edge(condition, node_id, "right"),
            ]
        )
        result = node_id
    return result


def _binary_condition(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    node_id: str,
    left: str,
    right: str,
    operator: str = "greater_than",
    research_design: str,
) -> str:
    nodes.append(
        {
            "id": node_id,
            "type": operator,
            "config": {"research_design": research_design},
        }
    )
    edges.extend([_edge(left, node_id, "left"), _edge(right, node_id, "right")])
    return node_id


def build_minervini_module_graph(
    name: str = MODEL_STEM + "__MinerviniModule",
) -> FactorLabGraphDto:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    factors = {
        "return_12m": ("tr_12_1", "higher_better", "annual"),
        "return_6m": ("tr_6_1", "higher_better", "annual"),
        "return_3m": ("tr_3_1", "higher_better", "annual"),
        "near_high": ("high52w_gap_pct", "higher_better", "annual"),
        "risk_adjusted_momentum": ("risk_adj_mom", "higher_better", "annual"),
    }
    for stem, (factor_id, direction, basis) in factors.items():
        extra_nodes, extra_edges, output = _factor_pipeline(
            stem=stem,
            factor_id=factor_id,
            direction=direction,
            financial_basis=basis,
        )
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        sources[stem] = output

    nodes.append(
        {
            "id": "minervini_score",
            "type": "weighted_score",
            "config": {
                "weights": dict(MINERVINI_WEIGHTS),
                "missing_weight_renormalize": False,
                "research_design": (
                    "persistent multi-horizon price strength with risk-adjusted confirmation"
                ),
            },
        }
    )
    edges.extend(
        _edge(source, "minervini_score", handle)
        for handle, source in sources.items()
    )

    # Reuse the raw price inputs for the trend template.  Positive 6/12-month
    # returns and a near-high rule stand in for price-above-MA because the
    # historical factor catalog exposes moving averages but not a PIT close
    # factor on every signal date.
    for stem, value in (
        ("zero_12m", 0.0),
        ("zero_6m", 0.0),
        ("near_high_floor", -25.0),
    ):
        nodes.append({"id": stem, "type": "constant", "config": {"value": value}})
    conditions = [
        _binary_condition(
            nodes,
            edges,
            node_id="return_12m_positive",
            left="return_12m_input",
            right="zero_12m",
            research_design="12-1 month return is positive",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="return_6m_positive",
            left="return_6m_input",
            right="zero_6m",
            research_design="6-1 month return is positive",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="within_25pct_of_high",
            left="near_high_input",
            right="near_high_floor",
            research_design="price is within 25 percent of its 52-week high",
        ),
    ]

    for stem, factor_id in (("ma50", "ma_50"), ("ma150", "ma_150"), ("ma200", "ma_200")):
        nodes.append(
            {
                "id": f"{stem}_input",
                "type": "factor_input",
                "config": {
                    "factor_id": factor_id,
                    "financial_basis": "annual",
                    "missing_policy": "drop",
                },
            }
        )
    conditions.extend(
        [
            _binary_condition(
                nodes,
                edges,
                node_id="ma50_above_ma150",
                left="ma50_input",
                right="ma150_input",
                research_design="50-day moving average is above the 150-day average",
            ),
            _binary_condition(
                nodes,
                edges,
                node_id="ma150_above_ma200",
                left="ma150_input",
                right="ma200_input",
                research_design="150-day moving average is above the 200-day average",
            ),
        ]
    )
    trend_template = _and_chain(nodes, edges, conditions, stem="trend_template")
    nodes.extend(
        [
            {
                "id": "eligible_minervini_score",
                "type": "condition_score",
                "config": {
                    "research_design": (
                        "Minervini trend template is an eligibility rule, not a source of fitted weights"
                    )
                },
            },
            {
                "id": "module_rank_score",
                "type": "dense_score",
                "config": {"group_by": ["trade_date"], "order": "desc", "scale": "0_100"},
            },
        ]
    )
    edges.extend(
        [
            _edge(trend_template, "eligible_minervini_score", "condition"),
            _edge("minervini_score", "eligible_minervini_score", "score"),
            _edge("eligible_minervini_score", "module_rank_score", "input"),
        ]
    )
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(name, factor_data_mode="point_in_time_snapshot"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "module_rank_score"},
    )


def build_zweig_module_graph(
    name: str = MODEL_STEM + "__ZweigModule",
) -> FactorLabGraphDto:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    factors = {
        "eps_growth": ("eps_yoy_pct", "higher_better", "ttm"),
        "sales_growth": ("sales_growth_1y", "higher_better", "ttm"),
        "eps_surprise": ("us_eps_surprise_pct", "higher_better", "annual"),
        "eps_revision": ("us_eps_revision_30d_pct", "higher_better", "annual"),
        "eps_revision_acceleration": (
            "us_eps_revision_acceleration_30d_pct",
            "higher_better",
            "annual",
        ),
        "eps_dispersion": ("us_eps_dispersion_pct", "lower_better", "annual"),
    }
    for stem, (factor_id, direction, basis) in factors.items():
        extra_nodes, extra_edges, output = _factor_pipeline(
            stem=stem,
            factor_id=factor_id,
            direction=direction,
            financial_basis=basis,
        )
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        sources[stem] = output
    nodes.extend(
        [
            {
                "id": "zweig_score",
                "type": "weighted_score",
                "config": {
                    "weights": dict(ZWEIG_WEIGHTS),
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "realized earnings and sales growth confirmed by surprise, revisions, "
                        "revision acceleration, and low forecast disagreement"
                    ),
                },
            },
            {
                "id": "module_rank_score",
                "type": "dense_score",
                "config": {"group_by": ["trade_date"], "order": "desc", "scale": "0_100"},
            },
        ]
    )
    edges.extend(_edge(source, "zweig_score", handle) for handle, source in sources.items())
    edges.append(_edge("zweig_score", "module_rank_score", "input"))
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(name, factor_data_mode="point_in_time_snapshot"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "module_rank_score"},
    )


def build_pvgo_module_graph(
    name: str = MODEL_STEM + "__PVGOModule",
) -> FactorLabGraphDto:
    graph = build_pvgo_candidate_graph(
        name,
        PvgoCandidateSpec(
            core_weight=1.0,
            min_market_cap_mil=1_000.0,
            require_positive_normalized_nopat=True,
        ),
        start_date=FULL[0],
        end_date=FULL[1],
    )
    graph.experiment.market = "US"
    graph.experiment.rebalance.transaction_cost_bps = SELECTION_COST_BPS
    payload = graph.model_dump(mode="json")
    disconnected_raw_nodes = {"raw_input", "raw_winsor", "raw_sector_z"}
    payload["nodes"] = [
        node for node in payload["nodes"] if node["id"] not in disconnected_raw_nodes
    ]
    payload["edges"] = [
        edge
        for edge in payload["edges"]
        if edge["source"] not in disconnected_raw_nodes
        and edge["target"] not in disconnected_raw_nodes
    ]
    return FactorLabGraphDto(**payload)


def _lab_factor_id(run_id: str) -> str:
    return f"lab_{run_id.replace('-', '')}"


def build_composite_graph(
    name: str,
    spec: CompositeSpec,
    *,
    module_run_ids: dict[str, str],
) -> FactorLabGraphDto:
    weights = spec.weights()
    if set(module_run_ids) != set(weights):
        raise ValueError("module_run_ids must contain pvgo, minervini, and zweig")
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-12):
        raise ValueError("composite weights must sum to one")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for module in weights:
        input_id = f"{module}_module_input"
        nodes.append(
            {
                "id": input_id,
                "type": "factor_input",
                "config": {
                    "factor_id": _lab_factor_id(module_run_ids[module]),
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                },
            }
        )
    momentum_confirmation_weight = spec.minervini + spec.zweig
    nodes.extend(
        [
            {
                "id": "momentum_confirmation_score",
                "type": "weighted_score",
                "config": {
                    "weights": {
                        "minervini": spec.minervini / momentum_confirmation_weight,
                        "zweig": spec.zweig / momentum_confirmation_weight,
                    },
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "price momentum and earnings momentum must both be present"
                    ),
                },
            },
            {
                "id": "composite_score",
                "type": "weighted_score",
                "config": {
                    "weights": {
                        "pvgo": spec.pvgo,
                        "momentum_confirmation": momentum_confirmation_weight,
                    },
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "PVGO prices growth expectations; Minervini confirms demand; "
                        "Zweig confirms the earnings path"
                    ),
                },
            },
            {
                "id": "final_rank_score",
                "type": "dense_score",
                "config": {"group_by": ["trade_date"], "order": "desc", "scale": "0_100"},
            },
        ]
    )
    edges.extend(
        [
            _edge(
                "minervini_module_input",
                "momentum_confirmation_score",
                "minervini",
            ),
            _edge("zweig_module_input", "momentum_confirmation_score", "zweig"),
            _edge("pvgo_module_input", "composite_score", "pvgo"),
            _edge(
                "momentum_confirmation_score",
                "composite_score",
                "momentum_confirmation",
            ),
        ]
    )
    edges.append(_edge("composite_score", "final_rank_score", "input"))
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(name, factor_data_mode="raw"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )


def selection_score(
    *,
    train: dict[str, Any],
    validation: dict[str, Any],
    pre_holdout: dict[str, Any],
) -> tuple[float, bool, list[str]]:
    """CAGR-first objective with explicit robustness guardrails."""

    periods = {"train": train, "validation": validation}
    failures: list[str] = []
    for label, metrics in periods.items():
        if float(metrics["cagr"]) <= 0.0:
            failures.append(f"{label} CAGR is not positive")
        sharpe = metrics.get("sharpe")
        if sharpe is None or float(sharpe) < 0.40:
            failures.append(f"{label} Sharpe is below 0.40")
        if abs(float(metrics["max_drawdown"])) > 0.50:
            failures.append(f"{label} absolute MDD exceeds 50%")
    worst_cagr = min(float(value["cagr"]) for value in periods.values())
    worst_sharpe = min(float(value["sharpe"]) for value in periods.values())
    worst_mdd = max(abs(float(value["max_drawdown"])) for value in periods.values())
    score = (
        0.70 * float(pre_holdout["cagr"])
        + 0.20 * worst_cagr
        + 0.05 * worst_sharpe
        - 0.05 * worst_mdd
    )
    return score, not failures, failures


def _run_history(
    service: FactorLabService,
    graph: FactorLabGraphDto,
) -> tuple[str, str, Any]:
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
    return experiment.experiment_id, validation.graph_hash, run


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
        "period": [str(period[0]), str(period[1])],
        "portfolio": asdict(portfolio),
        "transaction_cost_bps": cost_bps,
        "metrics": _jsonable(result.summary),
        "annual_returns": [_jsonable(row) for row in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def _completed_run(payload: dict[str, Any], key: str) -> str | None:
    row = (payload.get("modules") or {}).get(key) or {}
    if row.get("status") != "completed":
        return None
    value = str(row.get("run_id") or "")
    return value or None


def _module_graphs() -> dict[str, FactorLabGraphDto]:
    return {
        "pvgo": build_pvgo_module_graph(),
        "minervini": build_minervini_module_graph(),
        "zweig": build_zweig_module_graph(),
    }


def run(output_path: Path = OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    payload: dict[str, Any] = {
        "status": "researching",
        "design": {
            "market": "US",
            "full": [str(value) for value in FULL],
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "pre_holdout": [str(value) for value in PRE_HOLDOUT],
            "holdout": [str(value) for value in HOLDOUT],
            "holdout_policy": "sealed until one candidate and portfolio are selected",
            "selection_cost_bps": SELECTION_COST_BPS,
            "objective": "CAGR-first with positive-CAGR, Sharpe, and drawdown guardrails",
            "factor_data_mode": "point_in_time_snapshot modules with one-day signal lag",
            "per_policy": "PER and earnings yield are excluded; PVGO replaces the valuation role",
            "module_weights": {
                "minervini": MINERVINI_WEIGHTS,
                "zweig": ZWEIG_WEIGHTS,
                "pvgo": PVGO_RELATIVE_WEIGHTS,
            },
            "candidate_weights": {
                key: value.weights() for key, value in CANDIDATES.items()
            },
            "portfolios": [asdict(value) for value in PORTFOLIOS],
        },
        "modules": {},
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["modules"].update(previous.get("modules") or {})
        payload["candidates"].update(previous.get("candidates") or {})

    module_run_ids: dict[str, str] = {}
    for key, graph in _module_graphs().items():
        cached_run_id = _completed_run(payload, key)
        if cached_run_id:
            try:
                cached = service.get_run(cached_run_id)
            except Exception:
                cached = None
            if cached is not None and cached.status == "completed":
                module_run_ids[key] = cached_run_id
                continue
        print(f"[MZ-PVGO] materializing {key} module", flush=True)
        experiment_id, graph_hash, result = _run_history(service, graph)
        module_run_ids[key] = result.run_id
        payload["modules"][key] = {
            "status": "completed",
            "model_name": graph.experiment.name,
            "experiment_id": experiment_id,
            "run_id": result.run_id,
            "graph_hash": graph_hash,
            "quality": _jsonable(result.quality),
        }
        _write_checkpoint(output_path, payload)

    ranking: list[dict[str, Any]] = []
    for candidate_key, spec in CANDIDATES.items():
        graph = build_composite_graph(
            MODEL_STEM + f"__Research__{candidate_key}",
            spec,
            module_run_ids=module_run_ids,
        )
        candidate = payload["candidates"].get(candidate_key) or {}
        if candidate.get("status") == "completed":
            ranking.extend(candidate.get("selection_rows") or [])
            continue
        print(f"[MZ-PVGO] materializing composite {candidate_key}", flush=True)
        experiment_id, graph_hash, result = _run_history(service, graph)
        rows: list[dict[str, Any]] = []
        for portfolio in PORTFOLIOS:
            train = _backtest(
                service,
                result.run_id,
                period=TRAIN,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            validation = _backtest(
                service,
                result.run_id,
                period=VALIDATION,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            pre_holdout = _backtest(
                service,
                result.run_id,
                period=PRE_HOLDOUT,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            score, feasible, failures = selection_score(
                train=train["metrics"],
                validation=validation["metrics"],
                pre_holdout=pre_holdout["metrics"],
            )
            row = {
                "key": (
                    f"{candidate_key}__top{portfolio.top_percent:g}"
                    f"__max{portfolio.max_positions}"
                ),
                "candidate": candidate_key,
                "portfolio": asdict(portfolio),
                "selection_score": score,
                "feasible": feasible,
                "guardrail_failures": failures,
                "train": train,
                "validation": validation,
                "pre_holdout": pre_holdout,
            }
            rows.append(row)
            ranking.append(row)
        payload["candidates"][candidate_key] = {
            "status": "completed",
            "spec": asdict(spec),
            "experiment_id": experiment_id,
            "run_id": result.run_id,
            "graph_hash": graph_hash,
            "quality": _jsonable(result.quality),
            "selection_rows": rows,
        }
        _write_checkpoint(output_path, payload)

    feasible_rows = [row for row in ranking if row["feasible"]]
    selection_pool = feasible_rows or ranking
    if not selection_pool:
        raise RuntimeError("no completed candidate/portfolio combinations")
    selection_pool.sort(
        key=lambda row: (
            float(row["selection_score"]),
            float(row["pre_holdout"]["metrics"]["cagr"]),
        ),
        reverse=True,
    )
    selected = selection_pool[0]
    selected_spec = CANDIDATES[str(selected["candidate"])]
    selected_portfolio = PortfolioSpec(**selected["portfolio"])

    # Only this point opens the holdout.  Recreate the lab-only graph with a
    # stable production name so FactorLab screening can reuse the same modules.
    final_graph = build_composite_graph(
        FINAL_MODEL_NAME,
        selected_spec,
        module_run_ids=module_run_ids,
    )
    final_experiment_id, final_graph_hash, final_run = _run_history(service, final_graph)
    holdout = _backtest(
        service,
        final_run.run_id,
        period=HOLDOUT,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    full = _backtest(
        service,
        final_run.run_id,
        period=FULL,
        portfolio=selected_portfolio,
        cost_bps=SELECTION_COST_BPS,
    )
    cost_sensitivity = {
        f"{cost:g}": _backtest(
            service,
            final_run.run_id,
            period=FULL,
            portfolio=selected_portfolio,
            cost_bps=cost,
        )
        for cost in COST_SENSITIVITY_BPS
    }
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=final_experiment_id, mode="screen")
    )
    payload.update(
        {
            "status": "completed",
            "selection": {
                "candidate": selected["candidate"],
                "spec": asdict(selected_spec),
                "portfolio": asdict(selected_portfolio),
                "selection_score": selected["selection_score"],
                "feasible": selected["feasible"],
                "guardrail_failures": selected["guardrail_failures"],
                "train": selected["train"],
                "validation": selected["validation"],
                "pre_holdout": selected["pre_holdout"],
            },
            "final": {
                "model_name": FINAL_MODEL_NAME,
                "experiment_id": final_experiment_id,
                "history_run_id": final_run.run_id,
                "screen_run_id": screen.run_id,
                "graph_hash": final_graph_hash,
                "quality": _jsonable(final_run.quality),
                "holdout_50bps": holdout,
                "full_50bps": full,
                "cost_sensitivity": cost_sensitivity,
                "screen_quality": _jsonable(screen.quality),
                "screen_top20": [_jsonable(row) for row in screen.rows[:20]],
            },
            "limitations": [
                "Delisted-security history is incomplete, so survivorship bias is not fully eliminated.",
                "The 2024-2026 holdout is evaluated once after selection and is not recycled into weights.",
                "This is a research backtest, not a forecast or a guarantee of future returns.",
            ],
        }
    )
    _write_checkpoint(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = run(args.output)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "selection": payload["selection"],
                "final": payload["final"],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
