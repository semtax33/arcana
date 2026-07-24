from __future__ import annotations

"""Build the point-in-time, ex-financials, gated Soft-QARP model.

Run with ``python -m scripts.build_gated_soft_qarp_model`` from the project
root.  The FactorLab score is an alpha signal; the separate portfolio policy
defines the eventual Top-N, buffer, and capacity rules.
"""

import json
from datetime import date
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.build_qarp_sector_factor_lab_model import (
    END_DATE,
    SECTOR_ZSCORED,
    START_DATE,
    WEIGHTS,
    build_graph as build_base_graph,
    raw_composite_top,
)


MODEL_NAME = "KR_ExFinancial_SoftQARP_Gated_v20260725"
NON_FINANCIAL_GICS_SECTORS = ["10", "15", "20", "25", "30", "35", "45", "50", "55", "60"]
# The snapshot table has full coverage for these factors from the 2022-06-30
# signal onward.  Strict PIT mode intentionally does not backfill the first
# 2022 rebalance from the mutable raw factor table.
PIT_START_DATE = date(2022, 7, 1)


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _block(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    block_id: str,
    factor_ids: list[str],
    threshold: float,
) -> None:
    block_weights = {f"f{index}": WEIGHTS[factor_id] for index, factor_id in enumerate(factor_ids)}
    total_weight = sum(block_weights.values())
    block_weights = {
        handle: weight / total_weight for handle, weight in block_weights.items()
    }
    nodes.extend(
        [
            {
                "id": f"{block_id}_raw",
                "type": "weighted_score",
                "config": {"weights": block_weights},
            },
            {
                "id": f"{block_id}_rank",
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                },
            },
            {
                "id": f"{block_id}_threshold",
                "type": "constant",
                "config": {"value": threshold},
            },
            {"id": f"{block_id}_excess", "type": "sub", "config": {}},
            {"id": f"{block_id}_gate", "type": "sqrt", "config": {}},
        ]
    )
    for handle, factor_id in zip(block_weights, factor_ids, strict=True):
        factor_index = list(WEIGHTS).index(factor_id)
        edges.append(_edge(f"zscore_{factor_index}", f"{block_id}_raw", handle))
    edges.extend(
        [
            _edge(f"{block_id}_raw", f"{block_id}_rank", "input"),
            _edge(f"{block_id}_rank", f"{block_id}_excess", "left"),
            _edge(f"{block_id}_threshold", f"{block_id}_excess", "right"),
            _edge(f"{block_id}_excess", f"{block_id}_gate", "input"),
        ]
    )


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    """Create a cross-sectional signal with soft-QARP block eligibility."""
    graph_dict = build_base_graph(name).model_dump(mode="json")
    experiment = graph_dict["experiment"]
    experiment["name"] = name
    experiment["start_date"] = PIT_START_DATE.isoformat()
    experiment["factor_data_mode"] = "point_in_time_snapshot"
    experiment["universe"] = {
        "type": "market",
        "sector_codes": NON_FINANCIAL_GICS_SECTORS,
        "industry_group_codes": [],
    }
    experiment["rebalance"] = {
        "frequency": "semiannual",
        "signal_lag_days": 1,
        "transaction_cost_bps": 20,
    }

    # Small sectors use a market/sector blended Z-score instead of becoming
    # invalid or obtaining a noisy pure-sector Z-score.  At n=20 the sector
    # contribution is 50%, rising smoothly as its sample grows.
    for index, factor_id in enumerate(WEIGHTS):
        if factor_id not in SECTOR_ZSCORED:
            continue
        node = next(node for node in graph_dict["nodes"] if node["id"] == f"zscore_{index}")
        node["type"] = "shrunk_zscore"
        node["config"] = {
            "group_key": "sector",
            "min_market_count": 20,
            "min_group_count": 20,
            "shrinkage_strength": 20,
            "direction": node["config"]["direction"],
            "clip": 3.0,
        }

    nodes = graph_dict["nodes"]
    edges = graph_dict["edges"]
    # The base graph's final rank edge is replaced by a zero-weight validity
    # join.  A failed block gate invalidates the candidate, while the raw score
    # itself remains unchanged for economically meaningful ranking.
    edges[:] = [
        edge
        for edge in edges
        if not (edge["source"] == "raw_composite" and edge["target"] == "final_rank_score")
    ]
    _block(
        nodes,
        edges,
        block_id="value",
        factor_ids=["pcr", "equity_duration_20y", "rim_upside_potential"],
        threshold=20.0,
    )
    _block(
        nodes,
        edges,
        block_id="quality",
        factor_ids=["roic_wacc_spread", "accrual_ratio", "f_score", "debt_ratio"],
        threshold=30.0,
    )
    _block(
        nodes,
        edges,
        block_id="catalyst",
        factor_ids=[
            "real_operating_income_expected_growth",
            "tr_12_1",
            "k_ratio_3y",
        ],
        threshold=50.0,
    )
    nodes.append(
        {
            "id": "gated_raw_composite",
            "type": "weighted_score",
            "config": {
                "weights": {
                    "score": 1.0,
                    "value_gate": 0.0,
                    "quality_gate": 0.0,
                    "catalyst_gate": 0.0,
                }
            },
        }
    )
    edges.extend(
        [
            _edge("raw_composite", "gated_raw_composite", "score"),
            _edge("value_gate", "gated_raw_composite", "value_gate"),
            _edge("quality_gate", "gated_raw_composite", "quality_gate"),
            _edge("catalyst_gate", "gated_raw_composite", "catalyst_gate"),
            _edge("gated_raw_composite", "final_rank_score", "input"),
        ]
    )
    return FactorLabGraphDto(**graph_dict)


def _score_distances(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = raw_rows[9]["raw_composite"] if len(raw_rows) >= 10 else None
    result = []
    for index, row in enumerate(raw_rows, start=1):
        item = dict(row)
        item["raw_rank"] = index
        item["score_distance_from_top10_cutoff"] = (
            None if cutoff is None else row["raw_composite"] - cutoff
        )
        result.append(item)
    return result


def run() -> dict[str, Any]:
    service = FactorLabService()
    graph = build_graph()
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(validation.errors)
    experiment = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    history = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="history",
            history_start_date=PIT_START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency="semiannual",
        )
    )
    backtest = service.run_backtest(
        history.run_id,
        FactorLabBacktestRequestDto(
            top_percent=20,
            start_date=PIT_START_DATE,
            end_date=END_DATE,
            rebalance_frequency="semiannual",
            market="KR",
            benchmarks=["KOSPI200", "KOSDAQ"],
            max_positions=10,
            transaction_cost_bps=20,
        ),
    )
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=experiment.experiment_id, mode="screen")
    )
    screen_date = screen.quality.date_coverage.get("max")
    if screen_date is None:
        raise RuntimeError("screening did not return an effective trade date")
    raw_rows = _score_distances(
        raw_composite_top(
            graph,
            screen_date,
            limit=25,
            score_node_id="gated_raw_composite",
        )
    )
    return {
        "experiment_id": experiment.experiment_id,
        "history_run_id": history.run_id,
        "screen_run_id": screen.run_id,
        "graph_hash": history.graph_hash,
        "factor_data_mode": "point_in_time_snapshot",
        "excluded_sector_codes": ["40"],
        "weights": WEIGHTS,
        "history_quality": history.quality.model_dump(mode="json"),
        "metrics": vars(backtest.summary),
        "annual_returns": [vars(item) for item in backtest.annual_returns],
        "backtest_warnings": backtest.warnings,
        "screen_quality": screen.quality.model_dump(mode="json"),
        "screen_top25": [item.model_dump(mode="json") for item in screen.rows[:25]],
        "raw_composite_top25": raw_rows,
        "portfolio_policy": {
            "new_entry_rank": 10,
            "existing_holding_exit_rank": 15,
            "weighting": "equal_weight",
            "max_names_per_sector": 2,
            "note": "Portfolio-policy constraints are reported separately because the FactorLab graph is an alpha score, not a stateful portfolio optimizer.",
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
