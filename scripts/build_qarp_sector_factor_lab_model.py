from __future__ import annotations

"""Create and verify the sector-aware QARP FactorLab model.

The score deliberately uses a small, non-overlapping value sleeve and adds
explicit quality signals.  It is intended to be run from the Arcana virtual
environment, for example:

    python -m scripts.build_qarp_sector_factor_lab_model
"""

import json
from datetime import date
from typing import Any, Mapping

from api.config.clickhouse import get_clickhouse_client
from api.repository.factor_lab_query import compile_factor_lab_graph
from api.repository.factor_screen_query import DEFAULT_FACTOR_SNAPSHOT_TABLE
from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService


MODEL_NAME = "Arcana_Required3_QARP_SectorValue_Semiannual10_20260724"
START_DATE = date(2022, 1, 1)
END_DATE = date(2026, 7, 22)

# Value 42%, explicit quality 20%, earnings catalyst 16%, momentum 18%,
# liquidity 4%.  Value factors are deliberately restricted to PCR, equity
# duration, and RIM upside to avoid stacking closely related valuation bets.
WEIGHTS = {
    "pcr": 0.17,
    "equity_duration_20y": 0.18,
    "rim_upside_potential": 0.07,
    "roic_wacc_spread": 0.08,
    "accrual_ratio": 0.04,
    "f_score": 0.04,
    "debt_ratio": 0.04,
    "real_operating_income_expected_growth": 0.16,
    "tr_12_1": 0.15,
    "k_ratio_3y": 0.03,
    "current_ratio": 0.04,
}

FACTOR_SPECS = {
    "pcr": ("annual", "lower"),
    "equity_duration_20y": ("annual", "lower"),
    "rim_upside_potential": ("annual", "higher"),
    "roic_wacc_spread": ("ttm", "higher"),
    "accrual_ratio": ("annual", "lower"),
    "f_score": ("ttm", "higher"),
    "debt_ratio": ("ttm", "lower"),
    "real_operating_income_expected_growth": ("ttm", "higher"),
    "tr_12_1": ("ttm", "higher"),
    "k_ratio_3y": ("annual", "higher"),
    "current_ratio": ("ttm", "higher"),
}

# PCR, equity duration, RIM, and current ratio are structurally different
# across sectors.  The other quality and catalyst variables retain global
# comparability, so the model does not erase genuine cross-sector differences.
SECTOR_ZSCORED = {
    "pcr",
    "equity_duration_20y",
    "rim_upside_potential",
    "current_ratio",
}


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    composite_weights: dict[str, float] = {}

    for index, (factor_id, weight) in enumerate(WEIGHTS.items()):
        input_id = f"input_{index}"
        winsor_id = f"winsor_{index}"
        zscore_id = f"zscore_{index}"
        basis, direction = FACTOR_SPECS[factor_id]
        group_by = (
            ["trade_date", "sector"]
            if factor_id in SECTOR_ZSCORED
            else ["trade_date"]
        )
        nodes.extend(
            [
                {
                    "id": input_id,
                    "type": "factor_input",
                    "config": {
                        "factor_id": factor_id,
                        "financial_basis": basis,
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
                        "min_count": 20,
                        "zero_std_policy": "invalid",
                        "direction": (
                            "lower_better"
                            if direction == "lower"
                            else "higher_better"
                        ),
                        "clip": 3.0,
                    },
                },
            ]
        )
        handle = f"factor_{index}"
        composite_weights[handle] = weight
        edges.extend(
            [
                {
                    "source": input_id,
                    "source_handle": "out",
                    "target": winsor_id,
                    "target_handle": "input",
                },
                {
                    "source": winsor_id,
                    "source_handle": "out",
                    "target": zscore_id,
                    "target_handle": "input",
                },
                {
                    "source": zscore_id,
                    "source_handle": "out",
                    "target": "raw_composite",
                    "target_handle": handle,
                },
            ]
        )

    nodes.extend(
        [
            {
                "id": "raw_composite",
                "type": "weighted_score",
                "config": {"weights": composite_weights},
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
    edges.append(
        {
            "source": "raw_composite",
            "source_handle": "out",
            "target": "final_rank_score",
            "target_handle": "input",
        }
    )
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "KR",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "universe": {"type": "market"},
            "rebalance": {
                "frequency": "semiannual",
                "signal_lag_days": 1,
                "transaction_cost_bps": 20,
            },
        },
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )


def raw_composite_top(
    graph: FactorLabGraphDto,
    trade_date: date,
    *,
    limit: int = 10,
    score_node_id: str = "raw_composite",
    weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Return the pre-rank score and weighted factor contributions.

    FactorLab persists its final rank score, but not intermediate node values.
    This reuses the compiled graph and the same point-in-time snapshot table
    that the screen uses, so the diagnostic is directly comparable to screen
    rankings rather than an economic interpretation of the 0--100 rank.
    """
    weights = weights or WEIGHTS
    graph_dict = graph.model_dump(mode="json")
    graph_dict["experiment"]["start_date"] = trade_date.isoformat()
    graph_dict["experiment"]["end_date"] = trade_date.isoformat()
    compiled = compile_factor_lab_graph(
        graph_dict,
        factor_table=DEFAULT_FACTOR_SNAPSHOT_TABLE,
    )
    cte_query = compiled.query.rsplit("\nSELECT *\nFROM ", 1)[0]
    select_columns = [
        "raw.trade_date AS trade_date",
        "raw.security_id AS security_id",
        "raw.value AS raw_composite",
    ]
    joins: list[str] = []
    for index, (factor_id, weight) in enumerate(weights.items()):
        alias = f"zscore_{index}"
        select_columns.extend(
            [
                f"{alias}.value AS {factor_id}_zscore",
                f"({weight:.12f} * {alias}.value) AS {factor_id}_contribution",
            ]
        )
        joins.append(
            f"""INNER JOIN node_zscore_{index} AS {alias}
    ON raw.trade_date = {alias}.trade_date
   AND raw.security_id = {alias}.security_id"""
        )
    selected_sql = ",\n    ".join(select_columns)
    joins_sql = "\n".join(joins)
    query = f"""{cte_query}
SELECT
    {selected_sql}
FROM node_{score_node_id} AS raw
{joins_sql}
WHERE toUInt8(raw.is_valid) = 1
ORDER BY raw.value DESC, raw.security_id ASC
LIMIT {int(limit)}"""
    client = get_clickhouse_client()
    try:
        rows = client.query_df(query, parameters=compiled.parameters).to_dict(
            orient="records"
        )
    finally:
        client.close()
    return rows


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
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency="semiannual",
        )
    )
    backtest = service.run_backtest(
        history.run_id,
        FactorLabBacktestRequestDto(
            top_percent=20,
            start_date=START_DATE,
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
    return {
        "experiment_id": experiment.experiment_id,
        "history_run_id": history.run_id,
        "screen_run_id": screen.run_id,
        "graph_hash": history.graph_hash,
        "weights": WEIGHTS,
        "sector_zscored": sorted(SECTOR_ZSCORED),
        "history_quality": history.quality.model_dump(mode="json"),
        "metrics": vars(backtest.summary),
        "annual_returns": [vars(item) for item in backtest.annual_returns],
        "backtest_warnings": backtest.warnings,
        "screen_quality": screen.quality.model_dump(mode="json"),
        "screen_top10": [item.model_dump(mode="json") for item in screen.rows[:10]],
        "raw_composite_top10": raw_composite_top(graph, screen_date),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
