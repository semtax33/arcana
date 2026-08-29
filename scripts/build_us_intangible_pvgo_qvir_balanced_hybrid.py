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
from scripts.build_us_intangible_adjusted_pvgo import (
    MIN_MARKET_CAP_USD_MILLIONS,
    OPERATING_COMPANY_GICS_SECTORS,
)
from scripts.build_us_pvgo_qvir_balanced_hybrid import (
    END_DATE,
    MAX_POSITIONS,
    QVIR_SOURCE_NAME,
    REBALANCE_FREQUENCY,
    TOP_PERCENT,
    TRANSACTION_COST_BPS,
    _backtest,
    build_graph as build_base_hybrid_graph,
    build_qvir_core_graph,
)


ADJUSTED_PVGO_SOURCE_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_ExpectationsAlpha_Quarterly_20260829"
)
MODEL_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_QVIR_BalancedHybrid_Quarterly_20260829"
)
SLEEVE_WEIGHTS = {"intangible_adjusted_pvgo": 0.50, "qvir_core": 0.50}
# Match the adjusted-PVGO sleeve's first complete quarterly signal.  Starting in
# January would create an intentional but misleading zero-position rebalance.
START_DATE = date(2017, 4, 3)


def build_graph(
    service: FactorLabService,
    *,
    pvgo_history_run_id: str,
    qvir_history_run_id: str,
    pvgo_screen_run_id: str | None = None,
    qvir_screen_run_id: str | None = None,
    qvir_output_node: str = "composite_score",
    model_name: str = MODEL_NAME,
) -> tuple[FactorLabGraphDto, dict[str, str]]:
    """Build the target hybrid while preserving the audited PVGO universe.

    The generic hybrid builder also supports legacy models that include REITs.
    This target deliberately inherits the adjusted sleeve's operating-company,
    positive-adjusted-EPS, and USD 1bn eligibility because missing PVGO rows are not
    renormalized at the final sleeve combination.
    """

    graph, source_runs = build_base_hybrid_graph(
        service,
        pvgo_history_run_id=pvgo_history_run_id,
        qvir_history_run_id=qvir_history_run_id,
        pvgo_screen_run_id=pvgo_screen_run_id,
        qvir_screen_run_id=qvir_screen_run_id,
        pvgo_source_name=ADJUSTED_PVGO_SOURCE_NAME,
        qvir_source_name=QVIR_SOURCE_NAME,
        model_name=model_name,
    )
    payload = graph.model_dump(mode="json")
    payload["experiment"]["start_date"] = START_DATE
    payload["experiment"]["universe"]["sector_codes"] = (
        OPERATING_COMPANY_GICS_SECTORS
    )
    # This graph orchestrates immutable lab outputs: quarterly history factors
    # and same-day screen factors are deliberately date-disjoint.  Requiring all
    # four lab IDs on one date would make current screening impossible.  The
    # underlying adjusted-PVGO source remains strict PIT, while the final
    # PVGO/QVIR weighted score still requires both economic sleeves.
    payload["experiment"]["snapshot_coverage_policy"] = "allow_missing_inputs"
    for node in payload["nodes"]:
        if node["id"] == "balanced_hybrid_score":
            node["config"]["research_design"] = (
                "equal-weight stock-selection sleeves; both required; adjusted "
                "PVGO source supplies positive-adjusted-EPS and USD 1bn eligibility"
            )
        elif node["id"] == "final_rank_score":
            node["config"]["semantic_label"] = (
                "cross_sectional_rank_not_probability"
            )
        elif node["id"] in {
            "qvir_source_score",
            "qvir_history_score",
            "qvir_current_score",
        }:
            node["config"]["source_output_node"] = qvir_output_node
            node["config"]["hard_gate_policy"] = (
                "enforced" if qvir_output_node != "composite_score" else "diagnostic_only"
            )
    return FactorLabGraphDto(**payload), source_runs


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

    gated_graph, gated_source_runs = build_graph(
        service,
        pvgo_history_run_id=adjusted_history.run_id,
        qvir_history_run_id=qvir_gated_history.run_id,
        qvir_output_node=qvir_gated_graph.outputs.final_node_id,
        model_name=f"{MODEL_NAME}__MinerviniRiskGatedDiagnostic",
    )
    gated_validation = service.validate_graph(gated_graph)
    if not gated_validation.valid:
        raise RuntimeError(
            [
                issue.model_dump(mode="json")
                for issue in gated_validation.errors
            ]
        )
    gated_history = _run_source_graph_history(service, graph=gated_graph)

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
        "cost_100bps": _backtest(
            service,
            run_id=history.run_id,
            start_date=START_DATE,
            end_date=END_DATE,
            top_percent=TOP_PERCENT,
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=100.0,
        ),
        "minervini_risk_gated_intersection": {
            "source_runs": gated_source_runs,
            "history_run_id": gated_history.run_id,
            "history_quality": gated_history.quality.model_dump(mode="json"),
            "backtest": _backtest(
                service,
                run_id=gated_history.run_id,
                start_date=START_DATE,
                end_date=END_DATE,
                top_percent=TOP_PERCENT,
                max_positions=MAX_POSITIONS,
                transaction_cost_bps=TRANSACTION_COST_BPS,
            ),
        },
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
            "eligibility_inherited_from_pvgo": {
                "minimum_market_cap_usd_millions": MIN_MARKET_CAP_USD_MILLIONS,
                "positive_normalized_intangible_adjusted_eps": True,
                "excluded_gics_sectors": ["40", "60"],
            },
            "score_semantics": "0-100 cross-sectional rank, not a success probability",
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
