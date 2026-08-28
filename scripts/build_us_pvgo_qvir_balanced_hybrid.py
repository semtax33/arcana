from __future__ import annotations

"""Build the economically balanced PVGO + QVIR hybrid FactorLab strategy.

The design deliberately avoids performance-fitted sleeve weights.  It preserves
the PVGO score and the QVIR economic composite, standardizes their economically
distinct scores, and combines them 50/50.  The QVIR hard gate is not carried into
the hybrid because intersecting it with PVGO creates an undiversified universe;
trend and reflexivity remain present as soft inputs inside the QVIR composite.
"""

from dataclasses import asdict
from datetime import date
import json
from statistics import median
from typing import Any

import pandas as pd

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.factor_lab_research_diagnostics import (
    load_and_run_factor_model,
    newey_west_mean_test,
)


PVGO_SOURCE_NAME = "Arcana_US_PVGO_ExpectationsAlpha_Quarterly_20260829"
QVIR_SOURCE_NAME = (
    "Ungdroo_US_QualityValueInnovation_Reflexivity_RPR_TTM_Quarterly_"
    "Robust_v3_20160104_20260821_FactorLab"
)
MODEL_NAME = "Arcana_US_PVGO_QVIR_BalancedHybrid_Quarterly_20260829"
QVIR_GATED_HISTORY_RUN_ID = "76bbdef7-1ff0-4e25-955b-47dfd3735959"

START_DATE = date(2017, 1, 3)
END_DATE = date(2026, 8, 27)
REBALANCE_FREQUENCY = "quarterly"
TRANSACTION_COST_BPS = 20.0
TOP_PERCENT = 20.0
MAX_POSITIONS = 30
SLEEVE_WEIGHTS = {"pvgo": 0.50, "qvir": 0.50}
NON_FINANCIAL_GICS_SECTORS = [
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
]


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def build_qvir_core_graph(
    service: FactorLabService | None = None,
    *,
    qvir_source_name: str = QVIR_SOURCE_NAME,
    model_name: str = MODEL_NAME,
) -> FactorLabGraphDto:
    service = service or FactorLabService()
    source = service.get_experiment_by_name(qvir_source_name)
    payload = source.graph.model_dump(mode="json")
    payload["experiment"]["name"] = f"{model_name}__QVIRCoreSource"
    payload["experiment"]["start_date"] = START_DATE
    payload["experiment"]["end_date"] = END_DATE
    payload["outputs"] = {"final_node_id": "composite_score"}
    return FactorLabGraphDto(**payload)


def build_graph(
    service: FactorLabService | None = None,
    *,
    pvgo_history_run_id: str,
    qvir_history_run_id: str,
    pvgo_screen_run_id: str | None = None,
    qvir_screen_run_id: str | None = None,
    pvgo_source_name: str = PVGO_SOURCE_NAME,
    qvir_source_name: str = QVIR_SOURCE_NAME,
    model_name: str = MODEL_NAME,
) -> tuple[FactorLabGraphDto, dict[str, str]]:
    service = service or FactorLabService()
    pvgo_source = service.get_experiment_by_name(pvgo_source_name)
    qvir_source = service.get_experiment_by_name(qvir_source_name)
    pvgo_run = service.get_run(pvgo_history_run_id)
    qvir_run = service.get_run(qvir_history_run_id)
    if pvgo_run.status != "completed" or qvir_run.status != "completed":
        raise RuntimeError("both source FactorLab history runs must be completed")
    if pvgo_run.experiment_id != pvgo_source.experiment_id:
        raise RuntimeError("PVGO source history run does not belong to the named experiment")
    if (pvgo_screen_run_id is None) != (qvir_screen_run_id is None):
        raise ValueError("both source FactorLab screen run ids must be provided together")

    pvgo_screen = None
    qvir_screen = None
    if pvgo_screen_run_id is not None and qvir_screen_run_id is not None:
        pvgo_screen = service.get_run(pvgo_screen_run_id)
        qvir_screen = service.get_run(qvir_screen_run_id)
        if pvgo_screen.status != "completed" or qvir_screen.status != "completed":
            raise RuntimeError("both source FactorLab screen runs must be completed")
        if pvgo_screen.experiment_id != pvgo_source.experiment_id:
            raise RuntimeError("PVGO source screen run does not belong to the named experiment")

    sleeve_zscore_config = {
        "group_by": ["trade_date"],
        "stddev_method": "population",
        "min_count": 20,
        "zero_std_policy": "invalid",
        "direction": "as_is",
        "clip": 3.0,
    }
    pvgo_source_nodes = [
            {
                "id": "pvgo_source_score",
                "type": "factor_input",
                "config": {
                    "factor_id": pvgo_run.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": pvgo_history_run_id,
                    "point_in_time_lineage": True,
                },
            },
            {
                "id": "qvir_source_score",
                "type": "factor_input",
                "config": {
                    "factor_id": qvir_run.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": qvir_history_run_id,
                    "point_in_time_lineage": True,
                    "source_output_node": "composite_score",
                    "hard_gate_policy": (
                        "excluded to preserve diversification; trend remains a soft composite sleeve"
                    ),
                },
            },
        ]
    source_edges: list[dict[str, str]] = []
    if pvgo_screen is not None and qvir_screen is not None:
        pvgo_source_nodes = [
            {
                "id": "pvgo_history_score",
                "type": "factor_input",
                "config": {
                    "factor_id": pvgo_run.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": pvgo_history_run_id,
                    "point_in_time_lineage": True,
                },
            },
            {
                "id": "pvgo_current_score",
                "type": "factor_input",
                "config": {
                    "factor_id": pvgo_screen.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": pvgo_screen_run_id,
                    "point_in_time_lineage": True,
                },
            },
            {
                "id": "pvgo_source_score",
                "type": "weighted_score",
                "config": {
                    "weights": {"history": 1.0, "current": 1.0},
                    "missing_weight_renormalize": True,
                    "research_design": "datewise union of quarterly history and current screen",
                },
            },
            {
                "id": "qvir_history_score",
                "type": "factor_input",
                "config": {
                    "factor_id": qvir_run.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": qvir_history_run_id,
                    "point_in_time_lineage": True,
                    "source_output_node": "composite_score",
                },
            },
            {
                "id": "qvir_current_score",
                "type": "factor_input",
                "config": {
                    "factor_id": qvir_screen.factor_id,
                    "financial_basis": "lab",
                    "missing_policy": "drop",
                    "source_run_id": qvir_screen_run_id,
                    "point_in_time_lineage": True,
                    "source_output_node": "composite_score",
                },
            },
            {
                "id": "qvir_source_score",
                "type": "weighted_score",
                "config": {
                    "weights": {"history": 1.0, "current": 1.0},
                    "missing_weight_renormalize": True,
                    "research_design": "datewise union of quarterly history and current screen",
                },
            },
        ]
        source_edges = [
            _edge("pvgo_history_score", "pvgo_source_score", "history"),
            _edge("pvgo_current_score", "pvgo_source_score", "current"),
            _edge("qvir_history_score", "qvir_source_score", "history"),
            _edge("qvir_current_score", "qvir_source_score", "current"),
        ]

    nodes = pvgo_source_nodes + [
            {
                "id": "pvgo_sleeve_z",
                "type": "zscore",
                "config": {
                    **sleeve_zscore_config,
                    "economic_role": "market expectations versus finite-CAP reinvestment value",
                },
            },
            {
                "id": "qvir_sleeve_z",
                "type": "zscore",
                "config": {
                    **sleeve_zscore_config,
                    "economic_role": (
                        "cash quality, capital discipline, innovation, value, earnings and reflexivity"
                    ),
                },
            },
            {
                "id": "balanced_hybrid_score",
                "type": "weighted_score",
                "config": {
                    "weights": SLEEVE_WEIGHTS,
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "equal-weight independent economic sleeves; weights fixed ex ante and not fitted"
                    ),
                },
            },
            {
                "id": "final_rank_score",
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                    "portfolio_hint": {
                        "top_percent": TOP_PERCENT,
                        "max_positions": MAX_POSITIONS,
                        "rebalance_frequency": REBALANCE_FREQUENCY,
                        "transaction_cost_bps": TRANSACTION_COST_BPS,
                        "selection_note": (
                            "20%/30 positions fixed from effective intersection breadth"
                        ),
                    },
                },
            },
        ]
    edges = source_edges + [
            _edge("pvgo_source_score", "pvgo_sleeve_z", "input"),
            _edge("qvir_source_score", "qvir_sleeve_z", "input"),
            _edge("pvgo_sleeve_z", "balanced_hybrid_score", "pvgo"),
            _edge("qvir_sleeve_z", "balanced_hybrid_score", "qvir"),
            _edge("balanced_hybrid_score", "final_rank_score", "input"),
        ]

    graph = FactorLabGraphDto(
        version=1,
        experiment={
            "name": model_name,
            "market": "US",
            "start_date": START_DATE,
            "end_date": END_DATE,
            # Inputs are immutable FactorLab outputs whose source graphs used
            # point-in-time snapshots.  Derived lab_* inputs are queried from
            # factor_lab_values, so this orchestration graph itself uses raw mode.
            "factor_data_mode": "raw",
            "snapshot_coverage_policy": "allow_missing_inputs",
            "universe": {
                "type": "market",
                "sector_codes": NON_FINANCIAL_GICS_SECTORS,
            },
            "rebalance": {
                "frequency": REBALANCE_FREQUENCY,
                "signal_lag_days": 1,
                "transaction_cost_bps": TRANSACTION_COST_BPS,
            },
        },
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )
    source_runs = {
        "pvgo_experiment_id": pvgo_source.experiment_id,
        "qvir_experiment_id": qvir_source.experiment_id,
        "pvgo_history_run_id": pvgo_history_run_id,
        "qvir_history_run_id": qvir_history_run_id,
        "pvgo_graph_hash": pvgo_run.graph_hash,
        "qvir_graph_hash": qvir_run.graph_hash,
    }
    if pvgo_screen is not None and qvir_screen is not None:
        source_runs.update(
            {
                "pvgo_screen_run_id": pvgo_screen.run_id,
                "qvir_screen_run_id": qvir_screen.run_id,
                "pvgo_screen_graph_hash": pvgo_screen.graph_hash,
                "qvir_screen_graph_hash": qvir_screen.graph_hash,
            }
        )
    return graph, source_runs


def _backtest(
    service: FactorLabService,
    *,
    run_id: str,
    start_date: date,
    end_date: date,
    top_percent: float,
    max_positions: int,
    transaction_cost_bps: float,
) -> dict[str, Any]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=top_percent,
            start_date=start_date,
            end_date=end_date,
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="US",
            benchmarks=["US_SP500", "US_NASDAQ"],
            max_positions=max_positions,
            transaction_cost_bps=transaction_cost_bps,
        ),
    )
    position_counts = [len(item.positions) for item in result.rebalance_history]
    maximum_weights = [
        max((position.weight for position in item.positions), default=0.0)
        for item in result.rebalance_history
    ]
    nav = pd.Series(
        [point.strategy_nav for point in result.equity_curve],
        index=pd.to_datetime([point.trade_date for point in result.equity_curve]),
        dtype="float64",
    ).sort_index()
    daily_returns = nav.pct_change(fill_method=None).dropna()
    return {
        "parameters": {
            "start_date": start_date,
            "end_date": end_date,
            "top_percent": top_percent,
            "max_positions": max_positions,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "metrics": asdict(result.summary),
        "position_breadth": {
            "min_positions": min(position_counts, default=0),
            "median_positions": median(position_counts) if position_counts else 0,
            "max_positions": max(position_counts, default=0),
            "max_single_name_weight": max(maximum_weights, default=0.0),
        },
        "annual_returns": [asdict(item) for item in result.annual_returns],
        "inference": newey_west_mean_test(daily_returns),
        "ff5_momentum_regression": load_and_run_factor_model(daily_returns),
        "warnings": result.warnings,
    }


def run() -> dict[str, Any]:
    service = FactorLabService()
    pvgo_source = service.get_experiment_by_name(PVGO_SOURCE_NAME)
    pvgo_history = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=pvgo_source.experiment_id,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )
    pvgo_screen = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=pvgo_source.experiment_id,
            mode="screen",
        )
    )
    qvir_core_graph = build_qvir_core_graph(service)
    qvir_core_validation = service.validate_graph(qvir_core_graph)
    if not qvir_core_validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in qvir_core_validation.errors]
        )
    qvir_core = service.run_graph(
        FactorLabRunRequestDto(
            graph=qvir_core_graph,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )
    qvir_screen = service.run_graph(
        FactorLabRunRequestDto(
            graph=qvir_core_graph,
            mode="screen",
        )
    )
    graph, source_experiments = build_graph(
        service,
        pvgo_history_run_id=pvgo_history.run_id,
        qvir_history_run_id=qvir_core.run_id,
        pvgo_screen_run_id=pvgo_screen.run_id,
        qvir_screen_run_id=qvir_screen.run_id,
    )
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([issue.model_dump(mode="json") for issue in validation.errors])

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
    screen = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="screen",
        )
    )

    primary = _backtest(
        service,
        run_id=history.run_id,
        start_date=START_DATE,
        end_date=END_DATE,
        top_percent=TOP_PERCENT,
        max_positions=MAX_POSITIONS,
        transaction_cost_bps=TRANSACTION_COST_BPS,
    )
    source_baselines = {
        "pvgo": _backtest(
            service,
            run_id=pvgo_history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=20.0,
            max_positions=50,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "qvir_gated_common_period": _backtest(
            service,
            run_id=QVIR_GATED_HISTORY_RUN_ID,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=3.0,
            max_positions=30,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
    }
    robustness = {
        "breadth_10pct": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=10.0,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "breadth_30pct": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=30.0,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "cost_50bps": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=50.0,
        ),
        "early_2022_2023": _backtest(
            service,
            run_id=history.run_id,
            start_date=date(2022, 1, 3),
            end_date=date(2023, 12, 29),
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "late_2024_2026": _backtest(
            service,
            run_id=history.run_id,
            start_date=date(2024, 1, 2),
            end_date=END_DATE,
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
    }
    return {
        "model_name": MODEL_NAME,
        "experiment_id": experiment.experiment_id,
        "history_run_id": history.run_id,
        "screen_run_id": screen.run_id,
        "graph_hash": history.graph_hash,
        "source_experiments": source_experiments,
        "sleeve_weights": SLEEVE_WEIGHTS,
        "portfolio_rule": {
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "signal_lag_days": 1,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
        },
        "history_quality": history.quality.model_dump(mode="json"),
        "primary_backtest": primary,
        "source_baselines": source_baselines,
        "robustness": robustness,
        "latest_quarterly_signal_quality": screen.quality.model_dump(mode="json"),
        "latest_quarterly_signal_top10": [
            row.model_dump(mode="json") for row in screen.rows[:10]
        ],
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
