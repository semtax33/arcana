from __future__ import annotations

"""Build the strict-snapshot, ex-financials Soft-QARP alpha model.

Run from the project root with:

    python -m scripts.build_pit_soft_qarp_model
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
    SECTOR_ZSCORED,
    build_graph as build_base_graph,
    raw_composite_top,
)


MODEL_NAME = "KR_ExFinancial_PIT_SoftQARP_v20260725"
PIT_START_DATE = date(2022, 7, 1)
END_DATE = date(2026, 7, 22)
NON_FINANCIAL_GICS_SECTORS = ["10", "15", "20", "25", "30", "35", "45", "50", "55", "60"]

# The value sleeve remains 42%, but the questionable negative-PCR tail is
# excluded and the freed emphasis is placed on low equity duration and RIM.
# Explicit quality remains 20%, earnings catalyst 16%, and momentum 18%.
MODEL_WEIGHTS = {
    "pcr": 0.10,
    "equity_duration_20y": 0.24,
    "rim_upside_potential": 0.08,
    "roic_wacc_spread": 0.08,
    "accrual_ratio": 0.04,
    "f_score": 0.04,
    "debt_ratio": 0.04,
    "real_operating_income_expected_growth": 0.16,
    "tr_12_1": 0.15,
    "k_ratio_3y": 0.03,
    "current_ratio": 0.04,
}

VALUE_FACTORS = {"pcr", "equity_duration_20y", "rim_upside_potential"}
QUALITY_FACTORS = {"roic_wacc_spread", "accrual_ratio", "f_score", "debt_ratio", "current_ratio"}
GROWTH_FACTORS = {"real_operating_income_expected_growth"}
MOMENTUM_FACTORS = {"tr_12_1", "k_ratio_3y"}


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    """Use strict PIT snapshots, sector shrinkage, and a positive-PCR guard."""
    graph_dict = build_base_graph(name).model_dump(mode="json")
    experiment = graph_dict["experiment"]
    experiment.update(
        {
            "name": name,
            "start_date": PIT_START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "factor_data_mode": "point_in_time_snapshot",
            "universe": {
                "type": "market",
                "sector_codes": NON_FINANCIAL_GICS_SECTORS,
                "industry_group_codes": [],
            },
            "rebalance": {
                "frequency": "semiannual",
                "signal_lag_days": 1,
                "transaction_cost_bps": 20,
            },
        }
    )

    factor_order = list(MODEL_WEIGHTS)
    raw_composite = next(
        node for node in graph_dict["nodes"] if node["id"] == "raw_composite"
    )
    raw_composite["config"]["weights"] = {
        f"factor_{index}": MODEL_WEIGHTS[factor_id]
        for index, factor_id in enumerate(factor_order)
    }
    for index, factor_id in enumerate(factor_order):
        if factor_id not in SECTOR_ZSCORED:
            continue
        node = next(
            item for item in graph_dict["nodes"] if item["id"] == f"zscore_{index}"
        )
        node["type"] = "shrunk_zscore"
        node["config"] = {
            "group_key": "sector",
            "min_market_count": 20,
            "min_group_count": 20,
            "shrinkage_strength": 20,
            "direction": node["config"]["direction"],
            "clip": 3.0,
        }

    # PCR below zero is usually negative operating cash flow, not a cheap
    # cash-flow multiple.  Dividing sqrt(PCR) by itself preserves the score
    # for PCR > 0 but propagates invalidity for PCR <= 0 into the composite.
    graph_dict["nodes"].extend(
        [
            {"id": "pcr_positive_root", "type": "sqrt", "config": {}},
            {"id": "pcr_positive_one", "type": "div", "config": {}},
            {"id": "pcr_validated_zscore", "type": "mul", "config": {}},
        ]
    )
    graph_dict["edges"].extend(
        [
            {
                "source": "input_0",
                "source_handle": "out",
                "target": "pcr_positive_root",
                "target_handle": "input",
            },
            {
                "source": "pcr_positive_root",
                "source_handle": "out",
                "target": "pcr_positive_one",
                "target_handle": "left",
            },
            {
                "source": "pcr_positive_root",
                "source_handle": "out",
                "target": "pcr_positive_one",
                "target_handle": "right",
            },
            {
                "source": "zscore_0",
                "source_handle": "out",
                "target": "pcr_validated_zscore",
                "target_handle": "left",
            },
            {
                "source": "pcr_positive_one",
                "source_handle": "out",
                "target": "pcr_validated_zscore",
                "target_handle": "right",
            },
        ]
    )
    for edge in graph_dict["edges"]:
        if edge["source"] == "zscore_0" and edge["target"] == "raw_composite":
            edge["source"] = "pcr_validated_zscore"
    return FactorLabGraphDto(**graph_dict)


def _decorate_raw_rows(
    rows: list[dict[str, Any]],
    rankings: list[Any],
) -> list[dict[str, Any]]:
    ranking_by_security = {row.security_id: row for row in rankings}
    cutoff = rows[9]["raw_composite"] if len(rows) >= 10 else None
    result: list[dict[str, Any]] = []
    for raw_rank, row in enumerate(rows, start=1):
        item = dict(row)
        ranking = ranking_by_security.get(item["security_id"])
        item["raw_rank"] = raw_rank
        item["ticker"] = ranking.ticker if ranking is not None else None
        item["stock_name"] = ranking.stock_name if ranking is not None else None
        item["percentile_rank"] = ranking.percentile_score if ranking is not None else None
        item["score_distance_from_top10_cutoff"] = (
            None if cutoff is None else item["raw_composite"] - cutoff
        )
        item["value_contribution"] = sum(
            item[f"{factor_id}_contribution"] for factor_id in VALUE_FACTORS
        )
        item["quality_contribution"] = sum(
            item[f"{factor_id}_contribution"] for factor_id in QUALITY_FACTORS
        )
        item["growth_contribution"] = sum(
            item[f"{factor_id}_contribution"] for factor_id in GROWTH_FACTORS
        )
        item["momentum_contribution"] = sum(
            item[f"{factor_id}_contribution"] for factor_id in MOMENTUM_FACTORS
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
    raw_rows = raw_composite_top(
        graph,
        screen_date,
        limit=25,
        weights=MODEL_WEIGHTS,
    )
    return {
        "experiment_id": experiment.experiment_id,
        "history_run_id": history.run_id,
        "screen_run_id": screen.run_id,
        "graph_hash": history.graph_hash,
        "factor_data_mode": "point_in_time_snapshot",
        "pit_status": "source_trade_date is enforced; source publication timestamps are not stored and remain unverified",
        "validation_period": {
            "start_date": PIT_START_DATE,
            "end_date": END_DATE,
            "reason_for_start": "all eleven factor snapshots first have complete coverage at the 2022-06-30 signal date",
        },
        "excluded_sector_codes": ["40"],
        "weights": MODEL_WEIGHTS,
        "history_quality": history.quality.model_dump(mode="json"),
        "metrics": vars(backtest.summary),
        "annual_returns": [vars(item) for item in backtest.annual_returns],
        "backtest_warnings": backtest.warnings,
        "screen_quality": screen.quality.model_dump(mode="json"),
        "screen_top25": [item.model_dump(mode="json") for item in screen.rows[:25]],
        "raw_composite_top25": _decorate_raw_rows(raw_rows, screen.rows),
        "portfolio_policy": {
            "new_entry_rank": 10,
            "existing_holding_exit_rank": 15,
            "weighting": "equal_weight",
            "max_names_per_sector": 2,
            "status": "policy specification; stateful execution is intentionally separate from the FactorLab alpha graph",
        },
        "rejected_gate_experiment": {
            "value_rank_min": 20,
            "quality_rank_min": 30,
            "catalyst_rank_min": 50,
            "reason": "the gate reduced historical coverage from 460 to 334 and did not improve return, MDD, Sharpe, or t-stat in the strict-PIT test",
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
