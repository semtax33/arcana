from __future__ import annotations

"""Run the frozen A/B/C/D PVGO accounting-treatment experiment."""

from copy import deepcopy
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
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
from scripts.build_us_clean_raw_pvgo import (
    MODEL_NAME as CLEAN_RAW_MODEL_NAME,
    build_graph as build_clean_raw_graph,
)
from scripts.build_us_intangible_adjusted_pvgo import (
    END_DATE,
    FACTOR_NODE_IDS,
    MAX_POSITIONS,
    REBALANCE_FREQUENCY,
    START_DATE,
    TOP_PERCENT,
    _edge,
    _factor_pipeline,
    _ablation_graph,
    build_graph as build_adjusted_graph,
)
from scripts.build_us_pvgo_expectations_alpha import (
    build_graph as build_legacy_graph,
)
from scripts.factor_lab_research_diagnostics import (
    load_and_run_factor_model,
    newey_west_mean_test,
)


LEGACY_CONTROL_NAME = "Arcana_US_LegacyRawPVGO_FrozenControl_Quarterly_20260829"
ADJUSTED_MODEL_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_ExpectationsAlpha_Quarterly_20260829"
)
ADJUSTED_LEVEL_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_LevelOnly_FrozenControl_Quarterly_20260829"
)
MATCHED_MODEL_NAMES = {
    "B_clean_raw_matched": (
        "Arcana_US_CleanRawPVGO_MatchedSample_Quarterly_20260829"
    ),
    "C_clean_intangible_matched": (
        "Arcana_US_IntangibleAdjustedPVGO_MatchedSample_Quarterly_20260829"
    ),
    "D_adjusted_level_only_matched": (
        "Arcana_US_IntangibleAdjustedPVGO_LevelOnly_MatchedSample_Quarterly_20260829"
    ),
}
MODEL_LABELS = {
    "A_legacy_raw": LEGACY_CONTROL_NAME,
    "B_clean_raw": CLEAN_RAW_MODEL_NAME,
    "C_clean_intangible": ADJUSTED_MODEL_NAME,
    "D_adjusted_level_only": ADJUSTED_LEVEL_NAME,
}
COST_SCENARIOS_BPS = (20.0, 50.0, 100.0)
DEFAULT_OUTPUT = Path("deliverables/pvgo_clean_control_experiment_20260829.json")


def _rename_graph(graph: FactorLabGraphDto, name: str) -> FactorLabGraphDto:
    payload = deepcopy(graph.model_dump(mode="json"))
    payload["experiment"]["name"] = name
    payload["experiment"]["start_date"] = START_DATE
    payload["experiment"]["end_date"] = END_DATE
    return FactorLabGraphDto(**payload)


def build_frozen_graphs() -> dict[str, FactorLabGraphDto]:
    adjusted = build_adjusted_graph()
    adjusted_level = _ablation_graph(
        adjusted,
        sleeve="expectation_level",
        source_node_id=FACTOR_NODE_IDS["expectation_level"],
    )
    return {
        "A_legacy_raw": _rename_graph(build_legacy_graph(), LEGACY_CONTROL_NAME),
        "B_clean_raw": build_clean_raw_graph(),
        "C_clean_intangible": adjusted,
        "D_adjusted_level_only": _rename_graph(
            adjusted_level,
            ADJUSTED_LEVEL_NAME,
        ),
    }


def build_matched_sample_graphs() -> dict[str, FactorLabGraphDto]:
    """Use one graph topology and one complete-case universe for B/C/D."""

    payload = deepcopy(build_adjusted_graph().model_dump(mode="json"))
    raw_specs = (
        (
            "matched_raw_expectation_level",
            "equity_pvgo_pct",
            "lower_better",
            "raw_expectation_level",
        ),
        (
            "matched_raw_quality",
            "roe_cost_of_equity_spread_pct",
            "higher_better",
            "raw_quality",
        ),
        (
            "matched_raw_expectation_change",
            "equity_pvgo_compression_pct",
            "higher_better",
            "raw_expectation_change",
        ),
    )
    for stem, factor_id, direction, handle in raw_specs:
        nodes, edges, output_node = _factor_pipeline(
            stem=stem,
            factor_id=factor_id,
            direction=direction,
        )
        payload["nodes"].extend(nodes)
        payload["edges"].extend(edges)
        payload["edges"].append(
            _edge(output_node, "intangible_expectations_alpha", handle)
        )

    payload["nodes"].extend(
        [
            {
                "id": "matched_raw_earnings_input",
                "type": "factor_input",
                "config": {
                    "factor_id": "normalized_earnings_5y",
                    "financial_basis": "ttm",
                    "missing_policy": "drop",
                },
            },
            {
                "id": "matched_raw_zero_earnings",
                "type": "constant",
                "config": {"value": 0.0},
            },
            {
                "id": "matched_positive_raw_earnings",
                "type": "greater_than",
                "config": {},
            },
            {
                "id": "matched_eligibility_gate",
                "type": "and",
                "config": {
                    "research_design": (
                        "same security-date sample: USD 1bn, raw and adjusted "
                        "normalized earnings positive, and all six factor inputs present"
                    )
                },
            },
        ]
    )
    payload["edges"].extend(
        [
            _edge(
                "matched_raw_earnings_input",
                "matched_positive_raw_earnings",
                "left",
            ),
            _edge(
                "matched_raw_zero_earnings",
                "matched_positive_raw_earnings",
                "right",
            ),
            _edge("eligibility_gate", "matched_eligibility_gate", "left"),
            _edge(
                "matched_positive_raw_earnings",
                "matched_eligibility_gate",
                "right",
            ),
        ]
    )
    for edge in payload["edges"]:
        if (
            edge["target"] == "eligible_expectations_alpha"
            and edge["target_handle"] == "condition"
        ):
            edge["source"] = "matched_eligibility_gate"
            edge["id"] = (
                "edge_matched_eligibility_gate_eligible_expectations_alpha_condition"
            )

    adjusted_handles = ("expectation_level", "quality", "expectation_change")
    raw_handles = (
        "raw_expectation_level",
        "raw_quality",
        "raw_expectation_change",
    )
    weights_by_label = {
        "B_clean_raw_matched": {
            **{handle: 0.0 for handle in adjusted_handles},
            **{handle: 1.0 / 3.0 for handle in raw_handles},
        },
        "C_clean_intangible_matched": {
            **{handle: 1.0 / 3.0 for handle in adjusted_handles},
            **{handle: 0.0 for handle in raw_handles},
        },
        "D_adjusted_level_only_matched": {
            "expectation_level": 1.0,
            "quality": 0.0,
            "expectation_change": 0.0,
            **{handle: 0.0 for handle in raw_handles},
        },
    }
    result = {}
    for label, weights in weights_by_label.items():
        graph_payload = deepcopy(payload)
        graph_payload["experiment"]["name"] = MATCHED_MODEL_NAMES[label]
        score_node = next(
            node
            for node in graph_payload["nodes"]
            if node["id"] == "intangible_expectations_alpha"
        )
        score_node["config"]["weights"] = weights
        score_node["config"]["missing_weight_renormalize"] = False
        score_node["config"]["research_design"] = (
            "matched complete-case six-factor accounting contrast; zero-weight "
            "inputs enforce identical security-date availability"
        )
        result[label] = FactorLabGraphDto(**graph_payload)
    return result


def _history_run(
    service: FactorLabService,
    graph: FactorLabGraphDto,
) -> tuple[Any, Any]:
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in validation.errors]
        )
    experiment = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    history = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )
    return experiment, history


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    transaction_cost_bps: float,
) -> tuple[dict[str, Any], pd.Series, dict[str, set[str]]]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=START_DATE,
            end_date=END_DATE,
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="US",
            benchmarks=["US_SP500", "US_NASDAQ"],
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=transaction_cost_bps,
        ),
    )
    nav = pd.Series(
        [point.strategy_nav for point in result.equity_curve],
        index=pd.to_datetime([point.trade_date for point in result.equity_curve]),
        dtype="float64",
    ).sort_index()
    returns = nav.pct_change(fill_method=None).dropna()
    holdings = {
        str(rebalance.signal_date): {
            position.security_id for position in rebalance.positions
        }
        for rebalance in result.rebalance_history
    }
    payload = {
        "transaction_cost_bps": transaction_cost_bps,
        "metrics": asdict(result.summary),
        "annual_returns": [asdict(item) for item in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "newey_west_mean_test": newey_west_mean_test(returns),
        "warnings": result.warnings,
    }
    if transaction_cost_bps == 20.0:
        payload["ff5_momentum_regression"] = load_and_run_factor_model(returns)
    return payload, returns, holdings


def _paired_diagnostic(
    left_returns: pd.Series,
    right_returns: pd.Series,
    left_holdings: dict[str, set[str]],
    right_holdings: dict[str, set[str]],
) -> dict[str, Any]:
    aligned = pd.concat(
        [left_returns.rename("left"), right_returns.rename("right")],
        axis=1,
        join="inner",
    ).dropna()
    active = aligned["left"] - aligned["right"]
    nw = newey_west_mean_test(active)
    annualized_active_return = float(active.mean() * 252)
    tracking_error = float(active.std(ddof=1) * np.sqrt(252))
    overlap = []
    common_rebalances = sorted(set(left_holdings) & set(right_holdings))
    for rebalance_date in common_rebalances:
        left = left_holdings[rebalance_date]
        right = right_holdings[rebalance_date]
        union = left | right
        overlap.append(len(left & right) / len(union) if union else np.nan)
    return {
        "observations": len(active),
        "annualized_arithmetic_active_return": annualized_active_return,
        "annualized_tracking_error": tracking_error,
        "information_ratio": (
            annualized_active_return / tracking_error if tracking_error else None
        ),
        "terminal_active_wealth": float((1.0 + active).prod()),
        "newey_west_mean_test": nw,
        "mean_rebalance_jaccard_overlap": float(np.nanmean(overlap)),
        "paired_rebalance_count": len(common_rebalances),
        "interpretation": "left minus right; inference uses paired daily net returns",
    }


def _factor_inputs(graph: FactorLabGraphDto) -> dict[str, str]:
    return {
        node.id: str(node.config.get("factor_id"))
        for node in graph.nodes
        if node.type == "factor_input"
    }


def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    graphs = build_frozen_graphs()
    matched_graphs = build_matched_sample_graphs()
    models: dict[str, Any] = {}
    primary_returns: dict[str, pd.Series] = {}
    primary_holdings: dict[str, dict[str, set[str]]] = {}

    for label, graph in graphs.items():
        print(f"[PVGO-CONTROL] validating/running {label}", flush=True)
        experiment, history = _history_run(service, graph)
        cost_results = {}
        for cost in COST_SCENARIOS_BPS:
            print(f"[PVGO-CONTROL] backtest {label} cost={cost:g}bp", flush=True)
            backtest, returns, holdings = _backtest(
                service,
                history.run_id,
                transaction_cost_bps=cost,
            )
            cost_results[f"cost_{int(cost)}bps"] = backtest
            if cost == 20.0:
                primary_returns[label] = returns
                primary_holdings[label] = holdings
        models[label] = {
            "model_name": graph.experiment.name,
            "experiment_id": experiment.experiment_id,
            "history_run_id": history.run_id,
            "graph_hash": history.graph_hash,
            "factor_inputs": _factor_inputs(graph),
            "history_quality": history.quality.model_dump(mode="json"),
            "cost_scenarios": cost_results,
        }

    matched_models: dict[str, Any] = {}
    for label, graph in matched_graphs.items():
        print(f"[PVGO-CONTROL] validating/running {label}", flush=True)
        experiment, history = _history_run(service, graph)
        cost_results = {}
        for cost in COST_SCENARIOS_BPS:
            print(f"[PVGO-CONTROL] backtest {label} cost={cost:g}bp", flush=True)
            backtest, returns, holdings = _backtest(
                service,
                history.run_id,
                transaction_cost_bps=cost,
            )
            cost_results[f"cost_{int(cost)}bps"] = backtest
            if cost == 20.0:
                primary_returns[label] = returns
                primary_holdings[label] = holdings
        matched_models[label] = {
            "model_name": graph.experiment.name,
            "experiment_id": experiment.experiment_id,
            "history_run_id": history.run_id,
            "graph_hash": history.graph_hash,
            "factor_inputs": _factor_inputs(graph),
            "history_quality": history.quality.model_dump(mode="json"),
            "cost_scenarios": cost_results,
        }

    pairwise = {
        "C_minus_B_intangible_adjustment": _paired_diagnostic(
            primary_returns["C_clean_intangible"],
            primary_returns["B_clean_raw"],
            primary_holdings["C_clean_intangible"],
            primary_holdings["B_clean_raw"],
        ),
        "D_minus_C_level_simplicity": _paired_diagnostic(
            primary_returns["D_adjusted_level_only"],
            primary_returns["C_clean_intangible"],
            primary_holdings["D_adjusted_level_only"],
            primary_holdings["C_clean_intangible"],
        ),
    }
    matched_pairwise = {
        "C_minus_B_intangible_adjustment": _paired_diagnostic(
            primary_returns["C_clean_intangible_matched"],
            primary_returns["B_clean_raw_matched"],
            primary_holdings["C_clean_intangible_matched"],
            primary_holdings["B_clean_raw_matched"],
        ),
        "D_minus_C_level_simplicity": _paired_diagnostic(
            primary_returns["D_adjusted_level_only_matched"],
            primary_returns["C_clean_intangible_matched"],
            primary_holdings["D_adjusted_level_only_matched"],
            primary_holdings["C_clean_intangible_matched"],
        ),
    }
    result = {
        "design": {
            "labels": MODEL_LABELS,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "signal_lag_days": 1,
            "cost_scenarios_bps": COST_SCENARIOS_BPS,
            "primary_contrasts": ["C-B", "D-C"],
            "clean_graph_invariant": (
                "B and C share topology, PIT policy, universe, transforms, weights, "
                "dates and portfolio rules; only accounting-basis factor IDs and "
                "positive-earnings measurement differ"
            ),
            "matched_sample_invariant": (
                "B/C/D matched graphs contain the same six standardized factors, "
                "the same two positive-earnings tests and the same market-cap gate; "
                "zero weights change the score but still require every input row"
            ),
        },
        "models": models,
        "pairwise_20bps": pairwise,
        "matched_sample_models": matched_models,
        "matched_sample_pairwise_20bps": matched_pairwise,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
