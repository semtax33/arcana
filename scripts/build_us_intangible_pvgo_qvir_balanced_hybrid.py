from __future__ import annotations

"""Build and evaluate the intangible-adjusted PVGO + QVIR hybrid.

The research design is intentionally pre-committed rather than optimized:

* 50% intangible-adjusted PVGO expectations sleeve
* 50% QVIR economic composite sleeve (before its hard trend/risk gate)
* cross-sectional z-scores before combining the two distinct score scales
* the existing quarterly top-20% / 30-name / 20-bps hybrid portfolio rule

No sleeve weight, cutoff, or cost assumption is selected from the resulting
backtest.  Alternative breadth, cost, and subperiod runs are diagnostics only.
"""

from datetime import date
import json
from typing import Any

from api.service.dto import (
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.build_us_pvgo_qvir_balanced_hybrid import (
    END_DATE,
    MAX_POSITIONS,
    QVIR_SOURCE_NAME,
    REBALANCE_FREQUENCY,
    START_DATE,
    TOP_PERCENT,
    TRANSACTION_COST_BPS,
    _backtest,
    build_graph,
    build_qvir_core_graph,
)


ADJUSTED_PVGO_SOURCE_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_ExpectationsAlpha_Quarterly_20260829"
)
MODEL_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_QVIR_BalancedHybrid_Quarterly_20260829"
)
SLEEVE_WEIGHTS = {"intangible_adjusted_pvgo": 0.50, "qvir_core": 0.50}


def _run_history(
    service: FactorLabService,
    *,
    experiment_id: str,
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment_id,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )


def _run_screen(
    service: FactorLabService,
    *,
    experiment_id: str,
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment_id,
            mode="screen",
        )
    )


def _run_source_graph_history(
    service: FactorLabService,
    *,
    graph: FactorLabGraphDto,
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=graph,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )


def run() -> dict[str, Any]:
    service = FactorLabService()

    adjusted_source = service.get_experiment_by_name(ADJUSTED_PVGO_SOURCE_NAME)
    adjusted_history = _run_history(
        service,
        experiment_id=adjusted_source.experiment_id,
    )
    adjusted_screen = _run_screen(
        service,
        experiment_id=adjusted_source.experiment_id,
    )

    qvir_source = service.get_experiment_by_name(QVIR_SOURCE_NAME)
    qvir_gated_payload = qvir_source.graph.model_dump(mode="json")
    qvir_gated_payload["experiment"]["start_date"] = START_DATE
    qvir_gated_payload["experiment"]["end_date"] = END_DATE
    qvir_gated_graph = FactorLabGraphDto(**qvir_gated_payload)
    qvir_gated_history = _run_source_graph_history(
        service,
        graph=qvir_gated_graph,
    )

    qvir_core_graph = build_qvir_core_graph(
        service,
        qvir_source_name=QVIR_SOURCE_NAME,
        model_name=MODEL_NAME,
    )
    qvir_core_validation = service.validate_graph(qvir_core_graph)
    if not qvir_core_validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in qvir_core_validation.errors]
        )
    qvir_core_history = _run_source_graph_history(
        service,
        graph=qvir_core_graph,
    )
    qvir_core_screen = service.run_graph(
        FactorLabRunRequestDto(graph=qvir_core_graph, mode="screen")
    )

    graph, source_runs = build_graph(
        service,
        pvgo_history_run_id=adjusted_history.run_id,
        qvir_history_run_id=qvir_core_history.run_id,
        pvgo_screen_run_id=adjusted_screen.run_id,
        qvir_screen_run_id=qvir_core_screen.run_id,
        pvgo_source_name=ADJUSTED_PVGO_SOURCE_NAME,
        qvir_source_name=QVIR_SOURCE_NAME,
        model_name=MODEL_NAME,
    )
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in validation.errors]
        )

    experiment = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    history = _run_history(service, experiment_id=experiment.experiment_id)
    screen = _run_screen(service, experiment_id=experiment.experiment_id)

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
        "intangible_adjusted_pvgo_same_rule": _backtest(
            service,
            run_id=adjusted_history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "qvir_core_same_rule": _backtest(
            service,
            run_id=qvir_core_history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "qvir_gated_native_rule": _backtest(
            service,
            run_id=qvir_gated_history.run_id,
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
        "breadth_30pct_50max": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=30.0,
            max_positions=50,
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
        "pre_2022": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=date(2021, 12, 31),
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "stress_2022_2023": _backtest(
            service,
            run_id=history.run_id,
            start_date=date(2022, 1, 3),
            end_date=date(2023, 12, 29),
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
        "recent_2024_2026": _backtest(
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
        "source_runs": {
            **source_runs,
            "qvir_gated_history_run_id": qvir_gated_history.run_id,
            "qvir_core_history_run_id": qvir_core_history.run_id,
            "qvir_core_screen_run_id": qvir_core_screen.run_id,
            "adjusted_pvgo_history_run_id": adjusted_history.run_id,
            "adjusted_pvgo_screen_run_id": adjusted_screen.run_id,
        },
        "research_design": {
            "sleeve_weights": SLEEVE_WEIGHTS,
            "weight_selection": "fixed ex ante; no grid search or backtest fitting",
            "qvir_output": "composite_score before hard trend/risk gate",
            "standardization": "cross-sectional population z-score, clipped at +/-3",
            "missing_policy": "require both sleeves; no weight renormalization",
        },
        "portfolio_rule": {
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "signal_lag_days": 1,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
        },
        "history_quality": history.quality.model_dump(mode="json"),
        "screen_quality": screen.quality.model_dump(mode="json"),
        "primary_backtest": primary,
        "source_baselines": source_baselines,
        "robustness": robustness,
        "latest_signal_date": max(
            (row.trade_date for row in screen.rows),
            default=None,
        ),
        "latest_signal_count": screen.quality.security_coverage,
        "latest_signal_rows_returned": len(screen.rows),
        "latest_signal_top10": [
            row.model_dump(mode="json") for row in screen.rows[:10]
        ],
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
