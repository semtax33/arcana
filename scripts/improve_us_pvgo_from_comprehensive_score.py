from __future__ import annotations

"""Add a transparent, balanced confirmation layer to the latest US PVGO model.

The source strategy is deliberately treated as immutable.  The new FactorLab graph
uses the saved source run as its 90% anchor, then adds two 5% signals inspired by
the supplied comprehensive-score screenshots:

* a five-item 1/0 checklist spanning growth, profitability, cash yield, liquidity,
  and six-month trend; and
* sector-neutral R&D-to-market-cap rank, awarded only when at least three of those
  five checks pass.

This avoids treating a high R&D/market-cap ratio caused by a collapsing market cap
as an unconditional positive signal.
"""

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
    "Arcana_US_PVGO_BalancedChecklistConfirmation_"
    "Quarterly_2016_2026_20260907"
)
MODULE_MODEL_NAME = (
    "Arcana_US_PVGO_BalancedChecklistModule_"
    "Quarterly_2016_2026_20260907"
)
RESEARCH_NAME = MODEL_NAME + "__ResearchOnly"
MODULE_RESEARCH_NAME = MODULE_MODEL_NAME + "__ResearchOnly"
OUTPUT = Path("deliverables/us_pvgo_balanced_checklist_confirmation_20260907.json")
PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)
PRIMARY_COST_BPS = 50.0
COSTS_BPS = (20.0, 50.0, 100.0)

BASE_WEIGHT = 0.90
CHECKLIST_WEIGHT = 0.05
QUALIFIED_RND_WEIGHT = 0.05
MIN_CHECKLIST_PASSES = 3


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"{source}__to__{target}__{target_handle}",
        "source": source,
        "target": target,
        "target_handle": target_handle,
    }


def _factor_node(
    node_id: str,
    factor_id: str,
    financial_basis: str,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "factor_input",
        "config": {
            "factor_id": factor_id,
            "financial_basis": financial_basis,
            "missing_policy": "drop",
        },
    }


def _build_single_stage_research_graph(
    name: str = MODEL_NAME,
    *,
    start_date: date = US_FULL[0],
    end_date: date = US_FULL[1],
    source_run_id: str = SOURCE_RUN_ID,
) -> FactorLabGraphDto:
    """Build the screenshot-informed overlay without altering the source graph."""
    nodes: list[dict[str, Any]] = [
        _factor_node("base_score", _lab_factor_id(source_run_id), "lab"),
        _factor_node("rnd_to_market_cap", "rnd_to_market_cap", "ttm"),
        _factor_node("gross_profitability", "gross_profitability_pct", "ttm"),
        _factor_node("sales_growth_3y", "sales_growth_3y", "ttm"),
        _factor_node("fcf_to_ev_yield", "fcf_to_ev_yield", "ttm"),
        _factor_node("current_ratio", "current_ratio", "ttm"),
        _factor_node("return_6m", "tr_6_1", "annual"),
        {"id": "zero", "type": "constant", "config": {"value": 0.0}},
        {"id": "one", "type": "constant", "config": {"value": 1.0}},
        {"id": "base_floor", "type": "constant", "config": {"value": -1.0}},
        {
            "id": "checklist_pass_floor",
            "type": "constant",
            "config": {"value": float(MIN_CHECKLIST_PASSES) - 0.5},
        },
        {"id": "growth_positive", "type": "greater_than", "config": {}},
        {"id": "profitability_positive", "type": "greater_than", "config": {}},
        {"id": "cash_yield_positive", "type": "greater_than", "config": {}},
        {"id": "liquidity_above_one", "type": "greater_than", "config": {}},
        {"id": "return_6m_positive", "type": "greater_than", "config": {}},
        {"id": "growth_plus_profitability", "type": "add", "config": {}},
        {"id": "cash_plus_liquidity", "type": "add", "config": {}},
        {"id": "fundamental_pass_count", "type": "add", "config": {}},
        {"id": "checklist_raw", "type": "add", "config": {}},
        {
            "id": "checklist_rank",
            "type": "dense_score",
            "config": {
                "group_by": ["trade_date"],
                "order": "desc",
                "scale": "0_100",
                "tie_method": "average",
            },
        },
        {"id": "checklist_minimum_met", "type": "greater_than", "config": {}},
        {
            "id": "rnd_winsor",
            "type": "winsorize",
            "config": {
                "group_by": ["trade_date"],
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            },
        },
        {
            "id": "rnd_sector_z",
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
            "id": "rnd_rank",
            "type": "dense_score",
            "config": {
                "group_by": ["trade_date"],
                "order": "desc",
                "scale": "0_100",
                "tie_method": "average",
            },
        },
        {"id": "qualified_rnd_rank", "type": "condition_score", "config": {}},
        {
            "id": "balanced_confirmation",
            "type": "weighted_score",
            "config": {
                "weights": {
                    "anchor": BASE_WEIGHT,
                    "checklist": CHECKLIST_WEIGHT,
                    "qualified_rnd": QUALIFIED_RND_WEIGHT,
                },
                "missing_weight_renormalize": True,
                "research_design": (
                    "90% immutable source score plus a 5-item 1/0 checklist and "
                    "checklist-qualified sector-neutral R&D intensity"
                ),
            },
        },
        {"id": "source_member", "type": "greater_than", "config": {}},
        {"id": "source_universe_only", "type": "condition_score", "config": {}},
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
        _edge("sales_growth_3y", "growth_positive", "left"),
        _edge("zero", "growth_positive", "right"),
        _edge("gross_profitability", "profitability_positive", "left"),
        _edge("zero", "profitability_positive", "right"),
        _edge("fcf_to_ev_yield", "cash_yield_positive", "left"),
        _edge("zero", "cash_yield_positive", "right"),
        _edge("current_ratio", "liquidity_above_one", "left"),
        _edge("one", "liquidity_above_one", "right"),
        _edge("return_6m", "return_6m_positive", "left"),
        _edge("zero", "return_6m_positive", "right"),
        _edge("growth_positive", "growth_plus_profitability", "left"),
        _edge("profitability_positive", "growth_plus_profitability", "right"),
        _edge("cash_yield_positive", "cash_plus_liquidity", "left"),
        _edge("liquidity_above_one", "cash_plus_liquidity", "right"),
        _edge("growth_plus_profitability", "fundamental_pass_count", "left"),
        _edge("cash_plus_liquidity", "fundamental_pass_count", "right"),
        _edge("fundamental_pass_count", "checklist_raw", "left"),
        _edge("return_6m_positive", "checklist_raw", "right"),
        _edge("checklist_raw", "checklist_rank", "input"),
        _edge("checklist_raw", "checklist_minimum_met", "left"),
        _edge("checklist_pass_floor", "checklist_minimum_met", "right"),
        _edge("rnd_to_market_cap", "rnd_winsor", "input"),
        _edge("rnd_winsor", "rnd_sector_z", "input"),
        _edge("rnd_sector_z", "rnd_rank", "input"),
        _edge("checklist_minimum_met", "qualified_rnd_rank", "condition"),
        _edge("rnd_rank", "qualified_rnd_rank", "score"),
        _edge("base_score", "balanced_confirmation", "anchor"),
        _edge("checklist_rank", "balanced_confirmation", "checklist"),
        _edge("qualified_rnd_rank", "balanced_confirmation", "qualified_rnd"),
        _edge("base_score", "source_member", "left"),
        _edge("base_floor", "source_member", "right"),
        _edge("source_member", "source_universe_only", "condition"),
        _edge("balanced_confirmation", "source_universe_only", "score"),
        _edge("source_universe_only", "final_rank_score", "input"),
    ]
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": start_date,
            "end_date": end_date,
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


def build_checklist_module_graph(
    name: str = MODULE_MODEL_NAME,
    *,
    start_date: date = US_FULL[0],
    end_date: date = US_FULL[1],
) -> FactorLabGraphDto:
    """Build the raw-factor module against immutable PIT snapshots only."""
    payload = _build_single_stage_research_graph(
        name,
        start_date=start_date,
        end_date=end_date,
    ).model_dump(mode="json")
    removed = {
        "base_score",
        "base_floor",
        "balanced_confirmation",
        "source_member",
        "source_universe_only",
        "final_rank_score",
    }
    payload["nodes"] = [
        node for node in payload["nodes"] if node["id"] not in removed
    ]
    payload["edges"] = [
        edge
        for edge in payload["edges"]
        if edge["source"] not in removed and edge["target"] not in removed
    ]
    payload["nodes"].extend(
        [
            {
                "id": "checklist_confirmation",
                "type": "weighted_score",
                "config": {
                    "weights": {"checklist": 0.50, "qualified_rnd": 0.50},
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "PIT-only 50% checklist rank plus 50% checklist-qualified "
                        "sector-neutral R&D intensity rank"
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
    payload["edges"].extend(
        [
            _edge("checklist_rank", "checklist_confirmation", "checklist"),
            _edge("qualified_rnd_rank", "checklist_confirmation", "qualified_rnd"),
            _edge("checklist_confirmation", "module_rank_score", "input"),
        ]
    )
    payload["experiment"]["factor_data_mode"] = "point_in_time_snapshot"
    payload["experiment"]["snapshot_coverage_policy"] = "allow_missing_inputs"
    payload["outputs"] = {"final_node_id": "module_rank_score"}
    return FactorLabGraphDto(**payload)


def build_balanced_confirmation_graph(
    confirmation_run_id: str,
    name: str = MODEL_NAME,
    *,
    start_date: date = US_FULL[0],
    end_date: date = US_FULL[1],
    source_run_id: str = SOURCE_RUN_ID,
) -> FactorLabGraphDto:
    """Blend two immutable lab runs while retaining only source PVGO members."""
    nodes = [
        _factor_node("anchor_score", _lab_factor_id(source_run_id), "lab"),
        _factor_node(
            "checklist_confirmation_score",
            _lab_factor_id(confirmation_run_id),
            "lab",
        ),
        {"id": "anchor_floor", "type": "constant", "config": {"value": -1.0}},
        {
            "id": "balanced_confirmation",
            "type": "weighted_score",
            "config": {
                "weights": {
                    "anchor": BASE_WEIGHT,
                    "confirmation": CHECKLIST_WEIGHT + QUALIFIED_RND_WEIGHT,
                },
                "missing_weight_renormalize": True,
                "research_design": (
                    "90% immutable PVGO source run plus 10% immutable PIT "
                    "balanced-checklist module"
                ),
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
        _edge("anchor_score", "balanced_confirmation", "anchor"),
        _edge(
            "checklist_confirmation_score",
            "balanced_confirmation",
            "confirmation",
        ),
        _edge("anchor_score", "anchor_member", "left"),
        _edge("anchor_floor", "anchor_member", "right"),
        _edge("anchor_member", "anchor_universe_only", "condition"),
        _edge("balanced_confirmation", "anchor_universe_only", "score"),
        _edge("anchor_universe_only", "final_rank_score", "input"),
    ]
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": start_date,
            "end_date": end_date,
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


def _metrics_by_period(service: FactorLabService, run_id: str) -> dict[str, Any]:
    return {
        label: _backtest(
            service,
            run_id,
            market="US",
            period=period,
            portfolio=PORTFOLIO,
            cost_bps=PRIMARY_COST_BPS,
        )
        for label, period in (
            ("train", US_TRAIN),
            ("validation", US_VALIDATION),
            ("holdout", US_HOLDOUT),
            ("full", US_FULL),
        )
    }


def _is_improvement(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for label in ("train", "validation", "holdout", "full"):
        before = float(baseline[label]["metrics"]["sharpe"])
        after = float(candidate[label]["metrics"]["sharpe"])
        if after + 1e-12 < before:
            reasons.append(f"{label} Sharpe fell from {before:.6f} to {after:.6f}")
    before_mdd = abs(float(baseline["full"]["metrics"]["max_drawdown"]))
    after_mdd = abs(float(candidate["full"]["metrics"]["max_drawdown"]))
    if after_mdd > before_mdd + 1e-12:
        reasons.append(
            f"full absolute MDD rose from {before_mdd:.6f} to {after_mdd:.6f}"
        )
    return not reasons, reasons


def run(*, finalize: bool, output_path: Path = OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source_before = service.get_experiment(SOURCE_EXPERIMENT_ID)
    source_before_json = source_before.graph.model_dump_json()
    source_validation = service.validate_graph(source_before.graph)
    if source_validation.graph_hash != SOURCE_GRAPH_HASH:
        raise RuntimeError(
            "source graph hash changed before improvement: "
            f"{source_validation.graph_hash}"
        )

    module_graph = build_checklist_module_graph(
        MODULE_MODEL_NAME if finalize else MODULE_RESEARCH_NAME
    )
    module_validation = service.validate_graph(module_graph)
    if not module_validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in module_validation.errors])
    module_inline = _run_graph(service, graph=module_graph)
    if module_inline.status != "completed":
        raise RuntimeError(f"research module history run failed: {module_inline.status}")

    graph = build_balanced_confirmation_graph(
        module_inline.run_id,
        MODEL_NAME if finalize else RESEARCH_NAME,
    )
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])
    inline = _run_graph(service, graph=graph)
    if inline.status != "completed":
        raise RuntimeError(f"research history run failed: {inline.status}")

    baseline = _metrics_by_period(service, SOURCE_RUN_ID)
    candidate = _metrics_by_period(service, inline.run_id)
    improved, rejection_reasons = _is_improvement(baseline, candidate)

    payload: dict[str, Any] = {
        "model_name": graph.experiment.name,
        "status": "candidate_passed" if improved else "candidate_rejected",
        "source": {
            "experiment_id": SOURCE_EXPERIMENT_ID,
            "run_id": SOURCE_RUN_ID,
            "graph_hash": SOURCE_GRAPH_HASH,
        },
        "pit_confirmation_module": {
            "model_name": module_graph.experiment.name,
            "research_run_id": module_inline.run_id,
            "graph_hash": module_inline.graph_hash,
            "quality": _jsonable(module_inline.quality),
        },
        "design": {
            "base_weight": BASE_WEIGHT,
            "checklist_weight": CHECKLIST_WEIGHT,
            "qualified_rnd_weight": QUALIFIED_RND_WEIGHT,
            "checklist": [
                "sales_growth_3y > 0",
                "gross_profitability_pct > 0",
                "fcf_to_ev_yield > 0",
                "current_ratio > 1",
                "tr_6_1 > 0",
            ],
            "minimum_checklist_passes_for_rnd_credit": MIN_CHECKLIST_PASSES,
            "raw_checklist_scoring": "pass=1, fail=0; cross-sectional dense score",
            "portfolio": asdict(PORTFOLIO),
        },
        "research_run_id": inline.run_id,
        "graph_hash": inline.graph_hash,
        "quality": _jsonable(inline.quality),
        "baseline_50bps": baseline,
        "candidate_50bps": candidate,
        "selection_rule_passed": improved,
        "selection_rule_failures": rejection_reasons,
        "finalized": False,
    }

    if finalize:
        if not improved:
            raise RuntimeError(
                "candidate does not dominate the source under the frozen rule: "
                + "; ".join(rejection_reasons)
            )
        saved_module = service.save_experiment(
            FactorLabExperimentSaveRequestDto(graph=module_graph)
        )
        saved_module_run = _run_graph(
            service,
            experiment_id=saved_module.experiment_id,
        )
        if saved_module_run.graph_hash != module_inline.graph_hash:
            raise RuntimeError("inline and saved module graph hashes differ")
        final_graph = build_balanced_confirmation_graph(
            saved_module_run.run_id,
            MODEL_NAME,
        )
        saved = service.save_experiment(
            FactorLabExperimentSaveRequestDto(graph=final_graph)
        )
        saved_run = _run_graph(service, experiment_id=saved.experiment_id)
        expected_saved_graph_hash = service.validate_graph(final_graph).graph_hash
        if saved_run.graph_hash != expected_saved_graph_hash:
            raise RuntimeError("saved final graph hash differs from validation")
        saved_primary = _backtest(
            service,
            saved_run.run_id,
            market="US",
            period=US_FULL,
            portfolio=PORTFOLIO,
            cost_bps=PRIMARY_COST_BPS,
        )
        for metric in ("cumulative_return", "cagr", "max_drawdown", "sharpe"):
            expected = float(candidate["full"]["metrics"][metric])
            actual = float(saved_primary["metrics"][metric])
            if not math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-12):
                raise RuntimeError(f"saved result mismatch for {metric}")
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
                "model_name": MODEL_NAME,
                "status": "finalized",
                "finalized": True,
                "experiment_id": saved.experiment_id,
                "history_run_id": saved_run.run_id,
                "saved_graph_hash": saved_run.graph_hash,
                "confirmation_module_experiment_id": saved_module.experiment_id,
                "confirmation_module_history_run_id": saved_module_run.run_id,
                "confirmation_module_saved_graph_hash": saved_module_run.graph_hash,
                "cost_sensitivity": cost_sensitivity,
                "benchmark_metrics": _benchmark_metrics(full_result),
            }
        )

    source_after = service.get_experiment(SOURCE_EXPERIMENT_ID)
    payload["source_unchanged"] = (
        source_after.graph.model_dump_json() == source_before_json
        and service.validate_graph(source_after.graph).graph_hash == SOURCE_GRAPH_HASH
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
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="save a fresh experiment only if the frozen dominance rule passes",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = run(finalize=args.finalize, output_path=args.output)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
