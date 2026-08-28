from __future__ import annotations

"""Build and backtest the feedback-driven U.S. PVGO Expectations Alpha.

The four final sleeves are sector-neutral z-scores:

    40% PVGO Gap + 25% PVGO Quality
    + 20% PVGO Compression + 15% Raw PVGO.

Financials are excluded because the operating EV/NOPAT PVGO formula is not
comparable with bank and insurer balance sheets.  ``equity_pvgo_pct`` remains
available in the factor catalog for a separate financial-sector model.
"""

from datetime import date
import json
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService


MODEL_NAME = "Arcana_US_PVGO_ExpectationsAlpha_Quarterly_20260829"
START_DATE = date(2017, 1, 3)
END_DATE = date(2026, 8, 27)
REBALANCE_FREQUENCY = "quarterly"
TRANSACTION_COST_BPS = 20.0
TOP_PERCENT = 20.0
MAX_POSITIONS = 50
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
FINAL_WEIGHTS = {
    "gap": 0.40,
    "quality": 0.25,
    "compression": 0.20,
    "raw": 0.15,
}


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _factor_pipeline(
    *,
    stem: str,
    factor_id: str,
    direction: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    zscore_id = f"{stem}_sector_z"
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
        {
            "id": zscore_id,
            "type": "zscore",
            "config": {
                "group_by": ["trade_date", "sector"],
                "stddev_method": "population",
                "min_count": 5,
                "zero_std_policy": "invalid",
                "direction": direction,
                "clip": 3.0,
            },
        },
    ]
    edges = [
        _edge(input_id, winsor_id, "input"),
        _edge(winsor_id, zscore_id, "input"),
    ]
    return nodes, edges, zscore_id


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    gap_nodes, gap_edges, gap_z = _factor_pipeline(
        stem="gap",
        factor_id="pvgo_gap_pct",
        direction="higher_better",
    )
    raw_nodes, raw_edges, raw_z = _factor_pipeline(
        stem="raw",
        factor_id="pvgo_pct",
        direction="lower_better",
    )
    roiic_nodes, roiic_edges, roiic_z = _factor_pipeline(
        stem="roiic",
        factor_id="roiic_wacc_spread",
        direction="higher_better",
    )
    compression_nodes, compression_edges, compression_z = _factor_pipeline(
        stem="compression",
        factor_id="pvgo_compression_pct",
        direction="higher_better",
    )
    nodes.extend(gap_nodes + raw_nodes + roiic_nodes + compression_nodes)
    edges.extend(gap_edges + raw_edges + roiic_edges + compression_edges)

    nodes.extend(
        [
            {
                "id": "quality_sum",
                "type": "add",
                "config": {
                    "research_design": "z(ROIIC-WACC)-z(PVGO)",
                },
            },
            {
                "id": "quality_sector_z",
                "type": "zscore",
                "config": {
                    "group_by": ["trade_date", "sector"],
                    "stddev_method": "population",
                    "min_count": 5,
                    "zero_std_policy": "invalid",
                    "direction": "as_is",
                    "clip": 3.0,
                },
            },
            {
                "id": "expectations_alpha",
                "type": "weighted_score",
                "config": {
                    "weights": FINAL_WEIGHTS,
                    # The justified-PVGO gap needs a 5-year normalized NOPAT
                    # history plus a 3-year growth comparison.  Do not invent
                    # early values: reweight the other economic sleeves until
                    # the gap becomes observable, then automatically return to
                    # the fixed 40/25/20/15 design.
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "40% gap + 25% quality + 20% compression + 15% raw PVGO; "
                        "available-sleeve renormalization before gap history matures"
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
                },
            },
        ]
    )
    edges.extend(
        [
            _edge(roiic_z, "quality_sum", "left"),
            _edge(raw_z, "quality_sum", "right"),
            _edge("quality_sum", "quality_sector_z", "input"),
            _edge(gap_z, "expectations_alpha", "gap"),
            _edge("quality_sector_z", "expectations_alpha", "quality"),
            _edge(compression_z, "expectations_alpha", "compression"),
            _edge(raw_z, "expectations_alpha", "raw"),
            _edge("expectations_alpha", "final_rank_score", "input"),
        ]
    )

    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "factor_data_mode": "point_in_time_snapshot",
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


def run() -> dict[str, Any]:
    service = FactorLabService()
    graph = build_graph()
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
    backtest = service.run_backtest(
        history.run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=START_DATE,
            end_date=END_DATE,
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="US",
            benchmarks=["US_SP500", "US_NASDAQ"],
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
    )
    screen = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="screen",
        )
    )
    return {
        "model_name": MODEL_NAME,
        "experiment_id": experiment.experiment_id,
        "history_run_id": history.run_id,
        "screen_run_id": screen.run_id,
        "graph_hash": history.graph_hash,
        "weights": FINAL_WEIGHTS,
        "universe_sector_codes": NON_FINANCIAL_GICS_SECTORS,
        "history_quality": history.quality.model_dump(mode="json"),
        "metrics": vars(backtest.summary),
        "annual_returns": [vars(item) for item in backtest.annual_returns],
        "rebalance_count": len(backtest.rebalance_history),
        "backtest_warnings": backtest.warnings,
        "screen_quality": screen.quality.model_dump(mode="json"),
        "screen_top10": [row.model_dump(mode="json") for row in screen.rows[:10]],
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
