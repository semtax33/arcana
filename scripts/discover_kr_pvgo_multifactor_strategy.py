from __future__ import annotations

"""Discover new FactorLab sleeves around the fixed refined Korean PVGO core.

The three PVGO inputs and their directions are invariants.  Research candidates
only change the total mass assigned to that core, add economically distinct
sleeves, and optionally retain the beta gate.  Candidate selection never reads
the 2021-2026 holdout.
"""

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import date
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from api.service.dto import (
    FactorLabExperimentSaveRequestDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.optimize_kr_pvgo_expectations_alpha import (
    FINAL_COSTS_BPS,
    HOLDOUT,
    EARLY_HISTORY,
    LATE_HISTORY,
    SELECTION_COST_BPS,
    TRAIN,
    VALIDATION,
    CandidateSpec as BaseCandidateSpec,
    GateSpec,
    PortfolioSpec,
    _backtest,
    _edge,
    _graph_hash,
    _jsonable,
    _run_history,
    _stitched_diagnostic,
    _write_checkpoint,
    build_candidate_graph,
    selection_score,
)


MODEL_STEM = (
    "Arcana_KR_PVGO_Multifactor_Quarterly_2002_2026_20260906"
)
SCREEN_OUTPUT = Path(
    "deliverables/kr_pvgo_multifactor_screen_20260906.json"
)
COMBINATION_OUTPUT = Path(
    "deliverables/kr_pvgo_multifactor_combinations_20260906.json"
)
FINAL_OUTPUT = Path(
    "deliverables/kr_pvgo_multifactor_strategy_20260906.json"
)
FINAL_MODEL_NAME = (
    "Arcana_KR_PVGO_AssetDiscipline_LowVol_Quarterly_2002_2026_20260906"
)
REFINED_SOURCE_MODEL_NAME = (
    "Arcana_KR_PVGO_ExpectationsAlpha_Quarterly_"
    "2002_2026_CoreRefined_20260906"
)
CORE_RELATIVE_WEIGHTS = {
    "gap": 0.45,
    "quality": 0.30,
    "compression": 0.25,
}
SCREEN_PORTFOLIO = PortfolioSpec(top_percent=30.0, max_positions=50)
SCREEN_WEIGHTS = (0.10, 0.20, 0.30, 0.40)
COMBINATION_PORTFOLIOS = (
    PortfolioSpec(top_percent=30.0, max_positions=50),
)
FROZEN_FINAL_PORTFOLIO = PortfolioSpec(top_percent=30.0, max_positions=50)


@dataclass(frozen=True)
class SleeveSpec:
    key: str
    factor_id: str
    weight: float
    direction: str
    financial_basis: str
    neutralize_by_sector: bool = True
    economic_role: str = ""


@dataclass(frozen=True)
class CandidateSpec:
    core_weight: float
    sleeves: tuple[SleeveSpec, ...] = ()
    beta_gate: float | None = None
    min_market_cap_mil: float | None = None
    require_positive_normalized_nopat: bool = False
    require_complete_factor_case: bool = False
    extra_gates: tuple[GateSpec, ...] = ()


SLEEVE_CATALOG: dict[str, SleeveSpec] = {
    "trend_quality": SleeveSpec(
        key="trend_quality",
        factor_id="k_ratio_3y",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="persistent risk-adjusted compounding rather than a one-off price jump",
    ),
    "price_momentum": SleeveSpec(
        key="price_momentum",
        factor_id="tr_12_1",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="slow diffusion of information over months 12 through 1",
    ),
    "risk_adjusted_momentum": SleeveSpec(
        key="risk_adjusted_momentum",
        factor_id="risk_adj_mom",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="price confirmation scaled by realized risk",
    ),
    "near_52w_high": SleeveSpec(
        key="near_52w_high",
        factor_id="high52w_gap_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="anchoring and gradual information diffusion near prior highs",
    ),
    "low_volatility": SleeveSpec(
        key="low_volatility",
        factor_id="vol_12_1_ann",
        weight=0.0,
        direction="lower_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="limit the contribution of lottery-like high-volatility equities",
    ),
    "shareholder_yield": SleeveSpec(
        key="shareholder_yield",
        factor_id="shareholder_yield",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        economic_role="cash distributions and net repurchases returned to owners",
    ),
    "economic_profit_yield": SleeveSpec(
        key="economic_profit_yield",
        factor_id="economic_profit_yield",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="current economic profit relative to market value",
    ),
    "cash_to_debt": SleeveSpec(
        key="cash_to_debt",
        factor_id="cash_to_debt",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="balance-sheet liquidity buffer against refinancing shocks",
    ),
    "current_ratio": SleeveSpec(
        key="current_ratio",
        factor_id="current_ratio",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="short-term liquidity resilience",
    ),
    "rnd_to_market_cap": SleeveSpec(
        key="rnd_to_market_cap",
        factor_id="rnd_to_market_cap",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="innovation investment relative to the price paid",
    ),
    "intangible_pvgo_gap": SleeveSpec(
        key="intangible_pvgo_gap",
        factor_id="intangible_adjusted_pvgo_gap_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="PVGO gap after capitalizing knowledge and organization investment",
    ),
    "intangible_pvgo_compression": SleeveSpec(
        key="intangible_pvgo_compression",
        factor_id="intangible_adjusted_pvgo_compression_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="improving intangible-adjusted earning power relative to price",
    ),
    "external_financing_discipline": SleeveSpec(
        key="external_financing_discipline",
        factor_id="net_external_financing_pct",
        weight=0.0,
        direction="lower_better",
        financial_basis="annual",
        economic_role="avoid firms dependent on potentially dilutive external finance",
    ),
    "asset_growth_discipline": SleeveSpec(
        key="asset_growth_discipline",
        factor_id="asset_yoy_pct",
        weight=0.0,
        direction="lower_better",
        financial_basis="ttm",
        economic_role="avoid aggressive balance-sheet expansion with weak future returns",
    ),
    "operating_margin_growth": SleeveSpec(
        key="operating_margin_growth",
        factor_id="operating_margin_growth_1y",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="fundamental confirmation through improving operating economics",
    ),
    "gross_profitability": SleeveSpec(
        key="gross_profitability",
        factor_id="gross_profitability_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="operating surplus produced by the installed asset base",
    ),
    "fcf_to_ev_yield": SleeveSpec(
        key="fcf_to_ev_yield",
        factor_id="fcf_to_ev_yield",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="cash earnings yield relative to the price of the operating business",
    ),
    "total_accruals_discipline": SleeveSpec(
        key="total_accruals_discipline",
        factor_id="percent_total_accruals_pct",
        weight=0.0,
        direction="lower_better",
        financial_basis="ttm",
        economic_role="prefer earnings supported by cash rather than accounting accruals",
    ),
    "roic_wacc_spread": SleeveSpec(
        key="roic_wacc_spread",
        factor_id="roic_wacc_spread",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="installed capital earns more than its opportunity cost",
    ),
    "delta_economic_profit": SleeveSpec(
        key="delta_economic_profit",
        factor_id="delta_economic_profit",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="improving economic profit after charging for capital",
    ),
    "earnings_growth": SleeveSpec(
        key="earnings_growth",
        factor_id="eps_yoy_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="realized earnings confirmation of the PVGO thesis",
    ),
    "sales_growth": SleeveSpec(
        key="sales_growth",
        factor_id="sales_growth_1y",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="top-line confirmation that growth opportunities are becoming revenue",
    ),
    "inventory_growth_discipline": SleeveSpec(
        key="inventory_growth_discipline",
        factor_id="inventory_growth_1y_pct",
        weight=0.0,
        direction="lower_better",
        financial_basis="ttm",
        economic_role="avoid inventory expansion that outruns realized demand",
    ),
    "operating_margin": SleeveSpec(
        key="operating_margin",
        factor_id="opm",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="durable operating economics before financing effects",
    ),
    "rpr_value": SleeveSpec(
        key="rpr_value",
        factor_id="rpr",
        weight=0.0,
        direction="higher_better",
        financial_basis="ttm",
        economic_role="value support from a residual-profit-based price ratio",
    ),
    "market_cap_quality": SleeveSpec(
        key="market_cap_quality",
        factor_id="mcap_mil",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="reduce microcap, shell-company, and trading-friction exposure",
    ),
    "drawdown_control": SleeveSpec(
        key="drawdown_control",
        factor_id="mdd1yr_12_1_pct",
        weight=0.0,
        direction="higher_better",
        financial_basis="annual",
        neutralize_by_sector=False,
        economic_role="prefer equities with less severe recent peak-to-trough loss",
    ),
}


def _frozen_final_spec() -> CandidateSpec:
    return CandidateSpec(
        core_weight=0.40,
        sleeves=(
            _weighted_sleeve("asset_growth_discipline", 0.30),
            _weighted_sleeve("low_volatility", 0.30),
        ),
        beta_gate=1.5,
    )


def _validate_spec(spec: CandidateSpec) -> None:
    if not 0.0 < spec.core_weight <= 1.0:
        raise ValueError("core_weight must be in (0, 1]")
    if any(sleeve.weight <= 0.0 for sleeve in spec.sleeves):
        raise ValueError("every sleeve weight must be positive")
    if len({sleeve.key for sleeve in spec.sleeves}) != len(spec.sleeves):
        raise ValueError("sleeve keys must be unique")
    if not math.isclose(
        spec.core_weight + sum(sleeve.weight for sleeve in spec.sleeves),
        1.0,
        abs_tol=1e-12,
    ):
        raise ValueError("core and sleeve weights must sum to one")


def _sleeve_pipeline(
    sleeve: SleeveSpec,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{sleeve.key}_input"
    winsor_id = f"{sleeve.key}_winsor"
    zscore_id = f"{sleeve.key}_z"
    group_by = (
        ["trade_date", "sector"]
        if sleeve.neutralize_by_sector
        else ["trade_date"]
    )
    return (
        [
            {
                "id": input_id,
                "type": "factor_input",
                "config": {
                    "factor_id": sleeve.factor_id,
                    "financial_basis": sleeve.financial_basis,
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
                "id": zscore_id,
                "type": "zscore",
                "config": {
                    "group_by": group_by,
                    "stddev_method": "population",
                    "min_count": 5 if sleeve.neutralize_by_sector else 20,
                    "zero_std_policy": "invalid",
                    "direction": sleeve.direction,
                    "clip": 3.0,
                },
            },
        ],
        [
            _edge(input_id, winsor_id, "input"),
            _edge(winsor_id, zscore_id, "input"),
        ],
        zscore_id,
    )


def build_graph(
    name: str,
    spec: CandidateSpec,
    *,
    start_date: date,
    end_date: date,
):
    _validate_spec(spec)
    beta_gates = (
        (GateSpec("beta", "less_than", spec.beta_gate),)
        if spec.beta_gate is not None
        else ()
    )
    gates = beta_gates + spec.extra_gates
    graph = build_candidate_graph(
        name,
        BaseCandidateSpec(
            weights=dict(CORE_RELATIVE_WEIGHTS),
            deduplicate_quality=True,
            hard_gates=gates,
            min_market_cap_mil=spec.min_market_cap_mil,
            require_positive_normalized_nopat=(
                spec.require_positive_normalized_nopat
            ),
        ),
        start_date=start_date,
        end_date=end_date,
    )
    payload = graph.model_dump(mode="json")
    nodes = payload["nodes"]
    edges = payload["edges"]

    for node in nodes:
        if node["id"] == "expectations_alpha":
            node["id"] = "multifactor_score"
    for edge in edges:
        if edge["source"] == "expectations_alpha":
            edge["source"] = "multifactor_score"
        if edge["target"] == "expectations_alpha":
            edge["target"] = "multifactor_score"

    score_sources = {
        "gap": "gap_sector_z",
        "quality": "roiic_sector_z",
        "compression": "compression_sector_z",
    }
    for sleeve in spec.sleeves:
        extra_nodes, extra_edges, output = _sleeve_pipeline(sleeve)
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        score_sources[sleeve.key] = output

    weights = {
        key: value * spec.core_weight
        for key, value in CORE_RELATIVE_WEIGHTS.items()
    }
    weights.update({sleeve.key: sleeve.weight for sleeve in spec.sleeves})
    score_node = next(node for node in nodes if node["id"] == "multifactor_score")
    score_node["config"] = {
        "weights": weights,
        "missing_weight_renormalize": not spec.require_complete_factor_case,
        "research_design": (
            "fixed refined PVGO core plus economically distinct confirmation sleeves"
        ),
    }
    edges[:] = [edge for edge in edges if edge["target"] != "multifactor_score"]
    for handle, source in score_sources.items():
        edges.append(_edge(source, "multifactor_score", handle))

    return type(graph)(**payload)


def _screen_specs() -> dict[str, CandidateSpec]:
    specs = {
        "core_no_beta": CandidateSpec(core_weight=1.0),
        "core_beta_1_5": CandidateSpec(core_weight=1.0, beta_gate=1.5),
    }
    for key, template in SLEEVE_CATALOG.items():
        for weight in SCREEN_WEIGHTS:
            sleeve = SleeveSpec(**{**asdict(template), "weight": weight})
            specs[f"{key}__w{int(weight * 100)}"] = CandidateSpec(
                core_weight=1.0 - weight,
                sleeves=(sleeve,),
            )
    return specs


def _selection_row(
    *,
    key: str,
    spec: CandidateSpec,
    portfolio: PortfolioSpec,
    train: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    train_metrics = train["metrics"]
    validation_metrics = validation["metrics"]
    return {
        "key": key,
        "spec": asdict(spec),
        "portfolio": asdict(portfolio),
        "selection_score": selection_score(train_metrics, validation_metrics),
        "worst_sharpe": min(
            float(train_metrics["sharpe"]),
            float(validation_metrics["sharpe"]),
        ),
        "mean_sharpe": mean(
            [float(train_metrics["sharpe"]), float(validation_metrics["sharpe"])]
        ),
        "mean_cagr": mean(
            [float(train_metrics["cagr"]), float(validation_metrics["cagr"])]
        ),
        "worst_abs_mdd": max(
            abs(float(train_metrics["max_drawdown"])),
            abs(float(validation_metrics["max_drawdown"])),
        ),
    }


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
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


def _pareto_keys(rows: list[dict[str, Any]]) -> list[str]:
    return [
        str(row["key"])
        for row in rows
        if not any(_dominates(other, row) for other in rows if other is not row)
    ]


def run_screen(output_path: Path = SCREEN_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    specs = _screen_specs()
    payload: dict[str, Any] = {
        "design": {
            "model_stem": MODEL_STEM,
            "fixed_core_relative_weights": CORE_RELATIVE_WEIGHTS,
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "holdout_policy": "not accessed during screening",
            "selection_cost_bps": SELECTION_COST_BPS,
            "portfolio": asdict(SCREEN_PORTFOLIO),
        },
        "candidates": {},
    }
    rows: list[dict[str, Any]] = []
    for key, spec in specs.items():
        print(f"[KR-PVGO-MULTI] screen={key} early", flush=True)
        graph = build_graph(
            f"{MODEL_STEM}__screen__{key}",
            spec,
            start_date=EARLY_HISTORY[0],
            end_date=LATE_HISTORY[1],
        )
        try:
            early = _run_history(service, graph, period=EARLY_HISTORY)
            print(f"[KR-PVGO-MULTI] screen={key} late", flush=True)
            late = _run_history(service, graph, period=LATE_HISTORY)
            train, _ = _backtest(
                service,
                early.run_id,
                period=TRAIN,
                portfolio=SCREEN_PORTFOLIO,
                transaction_cost_bps=SELECTION_COST_BPS,
            )
            validation, _ = _backtest(
                service,
                late.run_id,
                period=VALIDATION,
                portfolio=SCREEN_PORTFOLIO,
                transaction_cost_bps=SELECTION_COST_BPS,
            )
            row = _selection_row(
                key=key,
                spec=spec,
                portfolio=SCREEN_PORTFOLIO,
                train=train,
                validation=validation,
            )
            rows.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_ids": {"early": early.run_id, "late": late.run_id},
                "history_quality": {
                    "early": _jsonable(early.quality),
                    "late": _jsonable(late.quality),
                },
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)

    rows.sort(key=lambda row: float(row["selection_score"]), reverse=True)
    payload["screening"] = {
        "ranking": rows,
        "pareto_front": _pareto_keys(rows),
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in rows
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def _weighted_sleeve(key: str, weight: float) -> SleeveSpec:
    return replace(SLEEVE_CATALOG[key], weight=weight)


def _combination_specs() -> dict[str, CandidateSpec]:
    specs: dict[str, CandidateSpec] = {}
    for low_volatility in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        specs[f"lowvol_{int(low_volatility * 100)}"] = CandidateSpec(
            core_weight=1.0 - low_volatility,
            sleeves=(
                _weighted_sleeve("low_volatility", low_volatility),
            ),
        )
    for asset_growth in (0.30, 0.40, 0.50):
        specs[f"asset_{int(asset_growth * 100)}"] = CandidateSpec(
            core_weight=1.0 - asset_growth,
            sleeves=(
                _weighted_sleeve("asset_growth_discipline", asset_growth),
            ),
        )
    for asset_growth in (0.10, 0.20, 0.30, 0.40):
        for low_volatility in (0.10, 0.20, 0.30, 0.40):
            core_weight = 1.0 - asset_growth - low_volatility
            if core_weight < 0.30:
                continue
            key = (
                f"asset_{int(asset_growth * 100)}"
                f"__lowvol_{int(low_volatility * 100)}"
            )
            specs[key] = CandidateSpec(
                core_weight=core_weight,
                sleeves=(
                    _weighted_sleeve("asset_growth_discipline", asset_growth),
                    _weighted_sleeve("low_volatility", low_volatility),
                ),
            )
    for asset_growth, low_volatility in (
        (0.20, 0.30),
        (0.20, 0.40),
        (0.30, 0.30),
        (0.30, 0.40),
    ):
        key = (
            f"asset_{int(asset_growth * 100)}"
            f"__lowvol_{int(low_volatility * 100)}__beta_1_5"
        )
        specs[key] = CandidateSpec(
            core_weight=1.0 - asset_growth - low_volatility,
            sleeves=(
                _weighted_sleeve("asset_growth_discipline", asset_growth),
                _weighted_sleeve("low_volatility", low_volatility),
            ),
            beta_gate=1.5,
        )
    return specs


def run_combinations(
    output_path: Path = COMBINATION_OUTPUT,
) -> dict[str, Any]:
    service = FactorLabService()
    payload: dict[str, Any] = {
        "design": {
            "model_stem": MODEL_STEM,
            "fixed_core_relative_weights": CORE_RELATIVE_WEIGHTS,
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "holdout_policy": "not accessed during combination search",
            "selection_cost_bps": SELECTION_COST_BPS,
            "portfolio_grid": [asdict(value) for value in COMBINATION_PORTFOLIOS],
            "economic_scope": (
                "combine the two complementary, coverage-audited single-factor leaders"
            ),
        },
        "candidates": {},
    }
    rows: list[dict[str, Any]] = []
    for candidate_key, spec in _combination_specs().items():
        graph = build_graph(
            f"{MODEL_STEM}__combine__{candidate_key}",
            spec,
            start_date=EARLY_HISTORY[0],
            end_date=LATE_HISTORY[1],
        )
        print(f"[KR-PVGO-MULTI] combine={candidate_key} early", flush=True)
        try:
            early = _run_history(service, graph, period=EARLY_HISTORY)
            print(f"[KR-PVGO-MULTI] combine={candidate_key} late", flush=True)
            late = _run_history(service, graph, period=LATE_HISTORY)
            candidate_payload: dict[str, Any] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_ids": {"early": early.run_id, "late": late.run_id},
                "history_quality": {
                    "early": _jsonable(early.quality),
                    "late": _jsonable(late.quality),
                },
                "portfolios": {},
            }
            for portfolio in COMBINATION_PORTFOLIOS:
                portfolio_key = (
                    f"{candidate_key}__top{portfolio.top_percent:g}"
                    f"__max{portfolio.max_positions}"
                )
                train, _ = _backtest(
                    service,
                    early.run_id,
                    period=TRAIN,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                validation, _ = _backtest(
                    service,
                    late.run_id,
                    period=VALIDATION,
                    portfolio=portfolio,
                    transaction_cost_bps=SELECTION_COST_BPS,
                )
                row = _selection_row(
                    key=portfolio_key,
                    spec=spec,
                    portfolio=portfolio,
                    train=train,
                    validation=validation,
                )
                rows.append(row)
                candidate_payload["portfolios"][portfolio_key] = {
                    "train": train,
                    "validation": validation,
                    "selection": row,
                }
            payload["candidates"][candidate_key] = candidate_payload
        except Exception as exc:
            payload["candidates"][candidate_key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)

    rows.sort(key=lambda row: float(row["selection_score"]), reverse=True)
    feasible = [row for row in rows if float(row["worst_sharpe"]) > 1.0]
    payload["selection"] = {
        "ranking": rows,
        "pareto_front": _pareto_keys(rows),
        "sharpe_constraint_feasible_count": len(feasible),
        "winner": (feasible or rows)[0] if rows else None,
    }
    _write_checkpoint(output_path, payload)
    return payload


def _experiment_identity(model: Any) -> dict[str, str]:
    return {
        "name": model.graph.experiment.name,
        "experiment_id": model.experiment_id,
        "graph_hash_sha256": _graph_hash(model.graph),
    }


def _reachable_factor_ids(graph: Any, execution_order: list[str]) -> list[str]:
    reachable = set(execution_order)
    return sorted(
        {
            str(node.config["factor_id"])
            for node in graph.nodes
            if node.id in reachable
            and node.type == "factor_input"
            and node.config.get("factor_id")
        }
    )


def run_final(output_path: Path = FINAL_OUTPUT) -> dict[str, Any]:
    combinations = json.loads(
        COMBINATION_OUTPUT.read_text(encoding="utf-8")
    )
    selected = combinations["selection"]["winner"]
    expected_key = "asset_30__lowvol_30__beta_1_5__top30__max50"
    if selected["key"] != expected_key:
        raise RuntimeError(
            f"frozen winner drifted: expected {expected_key}, got {selected['key']}"
        )

    service = FactorLabService()
    source = service.get_experiment_by_name(REFINED_SOURCE_MODEL_NAME)
    source_before = _experiment_identity(source)
    spec = _frozen_final_spec()
    graph = build_graph(
        FINAL_MODEL_NAME,
        spec,
        start_date=EARLY_HISTORY[0],
        end_date=LATE_HISTORY[1],
    )
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])

    # Re-run the frozen graph before saving.  A concurrent data rebuild must not
    # silently turn a previously passing research candidate into a saved claim.
    early_inline = _run_history(service, graph, period=EARLY_HISTORY)
    late_inline = _run_history(service, graph, period=LATE_HISTORY)
    inline_evidence: dict[str, dict[str, Any]] = {}
    for segment, run_id, period in (
        ("train", early_inline.run_id, TRAIN),
        ("validation", late_inline.run_id, VALIDATION),
        ("holdout", late_inline.run_id, HOLDOUT),
    ):
        result, _ = _backtest(
            service,
            run_id,
            period=period,
            portfolio=FROZEN_FINAL_PORTFOLIO,
            transaction_cost_bps=SELECTION_COST_BPS,
        )
        inline_evidence[segment] = result
    failing = {
        segment: result["metrics"]["sharpe"]
        for segment, result in inline_evidence.items()
        if float(result["metrics"]["sharpe"]) <= 1.0
    }
    if failing:
        raise RuntimeError(f"frozen Sharpe gate failed before save: {failing}")

    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    if saved.experiment_id == source_before["experiment_id"]:
        raise RuntimeError("new strategy reused the refined source experiment ID")
    early = _run_history(
        service,
        graph,
        period=EARLY_HISTORY,
        experiment_id=saved.experiment_id,
    )
    late = _run_history(
        service,
        graph,
        period=LATE_HISTORY,
        experiment_id=saved.experiment_id,
    )

    goal_evidence: dict[str, dict[str, Any]] = {}
    for segment, run_id, period in (
        ("train", early.run_id, TRAIN),
        ("validation", late.run_id, VALIDATION),
        ("holdout", late.run_id, HOLDOUT),
    ):
        result, _ = _backtest(
            service,
            run_id,
            period=period,
            portfolio=FROZEN_FINAL_PORTFOLIO,
            transaction_cost_bps=SELECTION_COST_BPS,
        )
        goal_evidence[segment] = result
    if any(
        float(result["metrics"]["sharpe"]) <= 1.0
        for result in goal_evidence.values()
    ):
        raise RuntimeError("saved experiment failed the Sharpe-above-1 gate")

    costs: dict[str, Any] = {}
    for cost in FINAL_COSTS_BPS:
        early_result, early_nav = _backtest(
            service,
            early.run_id,
            period=EARLY_HISTORY,
            portfolio=FROZEN_FINAL_PORTFOLIO,
            transaction_cost_bps=cost,
        )
        late_result, late_nav = _backtest(
            service,
            late.run_id,
            period=LATE_HISTORY,
            portfolio=FROZEN_FINAL_PORTFOLIO,
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
    source_after = _experiment_identity(
        service.get_experiment_by_name(REFINED_SOURCE_MODEL_NAME)
    )
    if source_after != source_before:
        raise RuntimeError("refined PVGO source changed while saving the new strategy")

    factor_ids = _reachable_factor_ids(graph, validation.execution_order)
    payload = {
        "design": {
            "selection_source": str(COMBINATION_OUTPUT),
            "screening_source": str(SCREEN_OUTPUT),
            "holdout_policy": (
                "the predeclared winner alone was opened on 2021-2026; no retuning"
            ),
            "transaction_cost_gate_bps": SELECTION_COST_BPS,
            "multiple_testing_warning": (
                "raw Sharpe ratios are not deflated for the number and dependence of trials"
            ),
        },
        "final": {
            "model_name": FINAL_MODEL_NAME,
            "experiment_id": saved.experiment_id,
            "graph_hash_sha256": _graph_hash(graph),
            "spec": asdict(spec),
            "portfolio": asdict(FROZEN_FINAL_PORTFOLIO),
            "factor_ids": factor_ids,
            "objective": (
                "maximize robust Sharpe, maximize CAGR, minimize absolute MDD"
            ),
            "selected_on_pareto_front": selected["key"]
            in combinations["selection"]["pareto_front"],
            "pre_holdout_selection": selected,
            "goal_evidence_50bps": goal_evidence,
            "cost_scenarios": costs,
            "history_run_ids": {"early": early.run_id, "late": late.run_id},
            "history_quality": {
                "early": _jsonable(early.quality),
                "late": _jsonable(late.quality),
            },
            "screen_run_id": screen.run_id,
            "screen_quality": _jsonable(screen.quality),
            "screen_top20": [_jsonable(row) for row in screen.rows[:20]],
            "source_preservation": {
                "before": source_before,
                "after": source_after,
                "unchanged": source_before == source_after,
            },
            "limitations": [
                "2013-2015 lacks the fixed PVGO core inputs, so no continuous claim is made.",
                "Delisted-security history remains incomplete.",
                "Flat bps costs do not model spread, impact, ADV, or capacity.",
                "Multiple-testing-adjusted Sharpe is not available from the current FactorLab API.",
            ],
        },
    }
    _write_checkpoint(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=("screen", "combinations", "final"),
        default="screen",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.stage == "screen":
        result = run_screen(args.output or SCREEN_OUTPUT)
        summary = result["screening"]
    elif args.stage == "combinations":
        result = run_combinations(args.output or COMBINATION_OUTPUT)
        summary = result["selection"]
    elif args.stage == "final":
        result = run_final(args.output or FINAL_OUTPUT)
        summary = result["final"]
    else:  # pragma: no cover - argparse prevents this branch
        raise ValueError(args.stage)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
