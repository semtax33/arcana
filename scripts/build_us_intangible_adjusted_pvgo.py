from __future__ import annotations

"""Build and audit the U.S. intangible-adjusted PVGO strategy.

The production score deliberately keeps three economically distinct roles:

* expectation level: normalized equity PVGO (lower is better)
* quality: intangible-adjusted ROE minus cost of equity
* expectation change: adjusted steady-state value compression

The older adjusted gap mixed an equity-PVGO denominator (earnings/cost of
equity) with an EV-PVGO justified value (NOPAT/WACC), and adjusted PVGO was
also included a second time inside the old ``quality_sum``.  Both sources of
double counting are excluded from the production score.  They can be studied
as separate research factors after a unit-consistent justified equity PVGO is
available.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import date
import json
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
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


MODEL_NAME = "Arcana_US_IntangibleAdjustedPVGO_ExpectationsAlpha_Quarterly_20260829"
START_DATE = date(2017, 1, 3)
END_DATE = date(2026, 8, 27)
REBALANCE_FREQUENCY = "quarterly"
TRANSACTION_COST_BPS = 20.0
TOP_PERCENT = 20.0
MAX_POSITIONS = 50
MIN_MARKET_CAP_USD_MILLIONS = 1_000.0
OPERATING_COMPANY_GICS_SECTORS = [
    "10",
    "15",
    "20",
    "25",
    "30",
    "35",
    "45",
    "50",
    "55",
]
FINAL_WEIGHTS = {
    "expectation_level": 1.0 / 3.0,
    "quality": 1.0 / 3.0,
    "expectation_change": 1.0 / 3.0,
}
FACTOR_NODE_IDS = {
    "expectation_level": "expectation_level_sector_z",
    "quality": "quality_sector_z",
    "expectation_change": "expectation_change_sector_z",
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
    ]
    edges = [
        _edge(input_id, winsor_id, "input"),
        _edge(winsor_id, zscore_id, "input"),
    ]
    return nodes, edges, zscore_id


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    value_nodes, value_edges, value_z = _factor_pipeline(
        stem="expectation_level",
        factor_id="normalized_intangible_adjusted_pvgo_pct",
        direction="lower_better",
    )
    iroe_nodes, iroe_edges, iroe_z = _factor_pipeline(
        stem="quality",
        factor_id="intangible_adjusted_roe_spread_pct",
        direction="higher_better",
    )
    compression_nodes, compression_edges, compression_z = _factor_pipeline(
        stem="expectation_change",
        factor_id="intangible_adjusted_pvgo_compression_pct",
        direction="higher_better",
    )
    nodes.extend(value_nodes + iroe_nodes + compression_nodes)
    edges.extend(value_edges + iroe_edges + compression_edges)

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
            {
                "id": "zero_nopat_floor",
                "type": "constant",
                "config": {"value": 0.0},
            },
            {
                "id": "positive_normalized_nopat",
                "type": "greater_than",
                "config": {
                    "research_design": "exclude negative steady-state operating earnings",
                },
            },
            {
                "id": "market_cap_input",
                "type": "factor_input",
                "config": {
                    "factor_id": "mcap_mil",
                    "financial_basis": "ttm",
                    "missing_policy": "drop",
                },
            },
            {
                "id": "market_cap_floor",
                "type": "constant",
                "config": {"value": MIN_MARKET_CAP_USD_MILLIONS},
            },
            {
                "id": "market_cap_eligible",
                "type": "greater_than",
                "config": {
                    "research_design": "Mauboussin-comparable USD 1bn minimum market cap",
                },
            },
            {
                "id": "eligibility_gate",
                "type": "and",
                "config": {
                    "research_design": (
                        "positive normalized NOPAT and minimum USD 1bn market cap"
                    ),
                },
            },
            {
                "id": "intangible_expectations_alpha",
                "type": "weighted_score",
                "config": {
                    "weights": FINAL_WEIGHTS,
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "equal-weight expectation level, unit-consistent quality, "
                        "and expectation change; fixed ex ante without grid search"
                    ),
                },
            },
            {
                "id": "eligible_expectations_alpha",
                "type": "condition_score",
                "config": {
                    "research_design": (
                        "clinical-option/distress and microcap guard before ranking"
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
                    "semantic_label": "cross_sectional_rank_not_probability",
                },
            },
        ]
    )
    edges.extend(
        [
            _edge("normalized_nopat_input", "positive_normalized_nopat", "left"),
            _edge("zero_nopat_floor", "positive_normalized_nopat", "right"),
            _edge("market_cap_input", "market_cap_eligible", "left"),
            _edge("market_cap_floor", "market_cap_eligible", "right"),
            _edge("positive_normalized_nopat", "eligibility_gate", "left"),
            _edge("market_cap_eligible", "eligibility_gate", "right"),
            _edge(value_z, "intangible_expectations_alpha", "expectation_level"),
            _edge(iroe_z, "intangible_expectations_alpha", "quality"),
            _edge(
                compression_z,
                "intangible_expectations_alpha",
                "expectation_change",
            ),
            _edge("eligibility_gate", "eligible_expectations_alpha", "condition"),
            _edge(
                "intangible_expectations_alpha",
                "eligible_expectations_alpha",
                "score",
            ),
            _edge("eligible_expectations_alpha", "final_rank_score", "input"),
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
            "snapshot_coverage_policy": "strict",
            "universe": {
                "type": "market",
                "sector_codes": OPERATING_COMPANY_GICS_SECTORS,
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


def _backtest(
    service: FactorLabService,
    *,
    run_id: str,
    start_date: date = START_DATE,
    end_date: date = END_DATE,
    transaction_cost_bps: float = TRANSACTION_COST_BPS,
) -> dict[str, Any]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=start_date,
            end_date=end_date,
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
    daily_returns = nav.pct_change(fill_method=None).dropna()
    return {
        "parameters": {
            "start_date": start_date,
            "end_date": end_date,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "metrics": asdict(result.summary),
        "annual_returns": [asdict(item) for item in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "inference": newey_west_mean_test(daily_returns),
        "warnings": result.warnings,
    }


def _ablation_graph(
    graph: FactorLabGraphDto,
    *,
    sleeve: str,
    source_node_id: str,
) -> FactorLabGraphDto:
    payload = deepcopy(graph.model_dump(mode="json"))
    eligible_id = f"{sleeve}_eligible_score"
    rank_id = f"{sleeve}_rank_score"
    payload["experiment"]["name"] = f"{MODEL_NAME}__Ablation_{sleeve}"
    payload["nodes"].extend(
        [
            {"id": eligible_id, "type": "condition_score", "config": {}},
            {
                "id": rank_id,
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                    "semantic_label": "cross_sectional_rank_not_probability",
                },
            },
        ]
    )
    payload["edges"].extend(
        [
            _edge("eligibility_gate", eligible_id, "condition"),
            _edge(source_node_id, eligible_id, "score"),
            _edge(eligible_id, rank_id, "input"),
        ]
    )
    payload["outputs"] = {"final_node_id": rank_id}
    return FactorLabGraphDto(**payload)


def _latest_factor_diagnostics(
    service: FactorLabService,
    *,
    run_id: str,
) -> dict[str, Any]:
    frames: list[pd.DataFrame] = []
    for sleeve, node_id in FACTOR_NODE_IDS.items():
        preview = service.preview_node(run_id, node_id=node_id, limit=1_000)
        rows = [row.model_dump(mode="json") for row in preview.rows if row.is_valid]
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        latest_date = frame["trade_date"].max()
        frame = frame.loc[frame["trade_date"] == latest_date, ["security_id", "value"]]
        frames.append(frame.rename(columns={"value": sleeve}).set_index("security_id"))
    if len(frames) != len(FACTOR_NODE_IDS):
        return {
            "status": "insufficient_node_preview",
            "available_sleeves": [column for frame in frames for column in frame.columns],
        }

    panel = pd.concat(frames, axis=1, join="inner").dropna()
    if panel.shape[0] < len(FACTOR_NODE_IDS) + 2:
        return {"status": "insufficient_complete_rows", "complete_rows": len(panel)}

    correlation = panel.corr(method="spearman")
    eigenvalues = np.linalg.eigvalsh(correlation.to_numpy(dtype=float))[::-1]
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    explained = eigenvalues / eigenvalues.sum() if eigenvalues.sum() else eigenvalues
    inverse_correlation = np.linalg.pinv(correlation.to_numpy(dtype=float))
    vif = dict(zip(correlation.columns, np.diag(inverse_correlation), strict=True))
    return {
        "status": "ok",
        "complete_rows": len(panel),
        "spearman_correlation": correlation.to_dict(),
        "pca_explained_variance_ratio": explained.tolist(),
        "vif": {key: float(value) for key, value in vif.items()},
        "interpretation": (
            "latest complete cross-section only; use the ablation backtests for time-series evidence"
        ),
    }


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
    primary = _backtest(service, run_id=history.run_id)
    ablations: dict[str, Any] = {}
    for sleeve, node_id in FACTOR_NODE_IDS.items():
        sleeve_graph = _ablation_graph(graph, sleeve=sleeve, source_node_id=node_id)
        sleeve_validation = service.validate_graph(sleeve_graph)
        if not sleeve_validation.valid:
            raise RuntimeError(
                [
                    issue.model_dump(mode="json")
                    for issue in sleeve_validation.errors
                ]
            )
        sleeve_history = service.run_graph(
            FactorLabRunRequestDto(
                graph=sleeve_graph,
                mode="history",
                history_start_date=START_DATE,
                history_end_date=END_DATE,
                history_rebalance_frequency=REBALANCE_FREQUENCY,
            )
        )
        ablations[sleeve] = {
            "history_run_id": sleeve_history.run_id,
            "history_quality": sleeve_history.quality.model_dump(mode="json"),
            "backtest": _backtest(service, run_id=sleeve_history.run_id),
        }

    robustness = {
        "cost_50bps": _backtest(
            service,
            run_id=history.run_id,
            transaction_cost_bps=50.0,
        ),
        "cost_100bps": _backtest(
            service,
            run_id=history.run_id,
            transaction_cost_bps=100.0,
        ),
        "pre_2022": _backtest(
            service,
            run_id=history.run_id,
            end_date=date(2021, 12, 31),
        ),
        "stress_2022_2023": _backtest(
            service,
            run_id=history.run_id,
            start_date=date(2022, 1, 3),
            end_date=date(2023, 12, 29),
        ),
        "recent_2024_2026": _backtest(
            service,
            run_id=history.run_id,
            start_date=date(2024, 1, 2),
        ),
    }
    factor_diagnostics = _latest_factor_diagnostics(
        service,
        run_id=history.run_id,
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
        "research_design": {
            "accounting_basis": "equity PVGO paired with adjusted ROE-cost of equity",
            "gap_policy": (
                "excluded until justified PVGO uses the same equity accounting basis"
            ),
            "missing_policy": "all three sleeves required; no weight renormalization",
            "eligibility": {
                "minimum_market_cap_usd_millions": MIN_MARKET_CAP_USD_MILLIONS,
                "positive_normalized_nopat": True,
                "excluded_gics_sectors": ["40", "60"],
            },
            "score_semantics": "0-100 cross-sectional rank, not a success probability",
        },
        "universe_sector_codes": OPERATING_COMPANY_GICS_SECTORS,
        "history_quality": history.quality.model_dump(mode="json"),
        "primary_backtest": primary,
        "ablations": ablations,
        "latest_factor_diagnostics": factor_diagnostics,
        "robustness": robustness,
        "screen_quality": screen.quality.model_dump(mode="json"),
        "screen_top10": [row.model_dump(mode="json") for row in screen.rows[:10]],
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
