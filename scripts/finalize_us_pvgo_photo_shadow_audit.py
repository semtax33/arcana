from __future__ import annotations

"""Persist a non-destructive PIT shadow-audit version of the latest US PVGO model."""

from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabGraphDto,
)
from api.service.factor_lab_service import FactorLabService, _lab_factor_id
from engine.core.clickhouse import get_clickhouse_client
from scripts.finalize_cross_market_pvgo_benchmark_strategy import (
    _benchmark_metrics,
    _cash_day_count,
)
from scripts.improve_us_pvgo_from_comprehensive_score import (
    SOURCE_EXPERIMENT_ID,
    SOURCE_GRAPH_HASH,
    SOURCE_RUN_ID,
    build_checklist_module_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import PortfolioSpec, _jsonable
from scripts.search_cross_market_pvgo_strategy import (
    US_FULL,
    US_HOLDOUT,
    US_TRAIN,
    US_VALIDATION,
    _backtest,
)


MODEL_NAME = (
    "Arcana_US_PVGO_ConfirmationFallback_ShadowQualityAudit_"
    "Quarterly_2016_2026_20260907"
)
MODULE_NAME = (
    "Arcana_US_PVGO_PhotoFeedbackShadowAudit_Module_"
    "Quarterly_2016_2026_20260907"
)
OUTPUT = Path("deliverables/us_pvgo_photo_shadow_audit_strategy_20260907.json")
PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)
PRIMARY_COST_BPS = 50.0
COSTS_BPS = (20.0, 50.0, 100.0)
AUDIT_EXPERIMENT_ID = "a9b1653a-350b-4ea1-913f-b645a488d68a"
AUDIT_RUN_ID = "d1a69579-5c6f-4197-b522-c5b1745262dd"
AUDIT_GRAPH_HASH = "5948ceef04a353d99ddd9f21a5ef1f143cc417347d7e42f92ae467ff3bc9c207"
FINAL_EXPERIMENT_ID = "190d17c2-2e05-4634-850a-09e541915676"
FINAL_RUN_ID = "768dfebc-59cd-4810-acf3-2d9d680dcbd6"
FINAL_GRAPH_HASH = "0d1cf632171c38d9a60522f2ac461b6a95bd28ff1b42e59e880ff45302a09515"


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"{source}__to__{target}__{target_handle}",
        "source": source,
        "target": target,
        "target_handle": target_handle,
    }


def build_shadow_strategy_graph(audit_run_id: str) -> FactorLabGraphDto:
    """Connect the PIT audit at zero weight so source rankings remain exact."""
    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": MODEL_NAME,
            "market": "US",
            "start_date": US_FULL[0],
            "end_date": US_FULL[1],
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
        nodes=[
            {
                "id": "source_score",
                "type": "factor_input",
                "config": {
                    "factor_id": _lab_factor_id(SOURCE_RUN_ID),
                    "financial_basis": "lab",
                },
            },
            {
                "id": "photo_audit_score",
                "type": "factor_input",
                "config": {
                    "factor_id": _lab_factor_id(audit_run_id),
                    "financial_basis": "lab",
                },
            },
            {
                "id": "source_plus_shadow_audit",
                "type": "weighted_score",
                "config": {
                    "weights": {"source": 1.0, "shadow_audit": 0.0},
                    "missing_weight_renormalize": True,
                    "shadow_policy": (
                        "audit is measured and persisted but cannot alter holdings "
                        "until a future forward validation authorizes non-zero weight"
                    ),
                },
            },
            {
                "id": "source_floor",
                "type": "constant",
                "config": {"value": -1.0},
            },
            {"id": "source_member", "type": "greater_than", "config": {}},
            {"id": "final_score", "type": "condition_score", "config": {}},
        ],
        edges=[
            _edge("source_score", "source_plus_shadow_audit", "source"),
            _edge("photo_audit_score", "source_plus_shadow_audit", "shadow_audit"),
            _edge("source_score", "source_member", "left"),
            _edge("source_floor", "source_member", "right"),
            _edge("source_member", "final_score", "condition"),
            _edge("source_plus_shadow_audit", "final_score", "score"),
        ],
        outputs={"final_node_id": "final_score"},
    )


def _metrics(
    service: FactorLabService,
    run_id: str,
    *,
    cost_bps: float = PRIMARY_COST_BPS,
) -> dict[str, Any]:
    return {
        label: _backtest(
            service,
            run_id,
            market="US",
            period=period,
            portfolio=PORTFOLIO,
            cost_bps=cost_bps,
        )
        for label, period in (
            ("train", US_TRAIN),
            ("validation", US_VALIDATION),
            ("holdout", US_HOLDOUT),
            ("full", US_FULL),
        )
    }


def _metric_deltas(
    baseline: dict[str, Any],
    materialized: dict[str, Any],
) -> dict[str, dict[str, float]]:
    return {
        label: {
            metric: float(materialized[label]["metrics"][metric])
            - float(baseline[label]["metrics"][metric])
            for metric in ("cumulative_return", "cagr", "max_drawdown", "sharpe")
        }
        for label in baseline
    }


def _assert_frozen_gate(
    baseline: dict[str, Any],
    materialized: dict[str, Any],
) -> None:
    """Require no degradation out of sample and a real full-period improvement."""
    tolerance = 1e-12
    for label in ("validation", "holdout"):
        for metric in ("cumulative_return", "cagr", "max_drawdown", "sharpe"):
            left = float(baseline[label]["metrics"][metric])
            right = float(materialized[label]["metrics"][metric])
            if not math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance):
                raise RuntimeError(
                    f"materialized strategy changed frozen {label} {metric}: "
                    f"{left} != {right}"
                )
    baseline_full = baseline["full"]["metrics"]
    materialized_full = materialized["full"]["metrics"]
    if float(materialized_full["sharpe"]) <= float(baseline_full["sharpe"]):
        raise RuntimeError("materialized strategy did not improve full-period Sharpe")
    if float(materialized_full["cagr"]) <= float(baseline_full["cagr"]):
        raise RuntimeError("materialized strategy did not improve full-period CAGR")
    if abs(float(materialized_full["max_drawdown"])) > (
        abs(float(baseline_full["max_drawdown"])) + tolerance
    ):
        raise RuntimeError("materialized strategy worsened full-period drawdown")


def _factor_value_equivalence() -> dict[str, Any]:
    query = """
        WITH
        source AS (
            SELECT security_id, trade_date, argMax(factor_value, updated_at) AS value
            FROM factor_lab_values
            WHERE run_id = %(source_run_id)s
            GROUP BY security_id, trade_date
        ),
        materialized AS (
            SELECT security_id, trade_date, argMax(factor_value, updated_at) AS value
            FROM factor_lab_values
            WHERE run_id = %(final_run_id)s
            GROUP BY security_id, trade_date
        )
        SELECT
            (SELECT count() FROM source) AS source_rows,
            (SELECT count() FROM materialized) AS materialized_rows,
            (SELECT count() FROM source INNER JOIN materialized
             USING (security_id, trade_date)) AS shared_rows,
            (SELECT countIf(abs(source.value - materialized.value) > 1e-12)
             FROM source INNER JOIN materialized
             USING (security_id, trade_date)) AS mismatches,
            (SELECT max(abs(source.value - materialized.value))
             FROM source INNER JOIN materialized
             USING (security_id, trade_date)) AS max_abs_diff
    """
    row = get_clickhouse_client().query(
        query,
        parameters={
            "source_run_id": SOURCE_RUN_ID,
            "final_run_id": FINAL_RUN_ID,
        },
    ).result_rows[0]
    result = {
        "source_rows": int(row[0]),
        "materialized_rows": int(row[1]),
        "shared_rows": int(row[2]),
        "mismatches_at_1e_12": int(row[3]),
        "max_abs_diff": float(row[4]),
    }
    if not (
        result["source_rows"]
        == result["materialized_rows"]
        == result["shared_rows"]
        and result["mismatches_at_1e_12"] == 0
        and result["max_abs_diff"] == 0.0
    ):
        raise RuntimeError(f"materialized factor values differ from source: {result}")
    return result


def run(output_path: Path = OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source_before = service.get_experiment(SOURCE_EXPERIMENT_ID)
    source_json = source_before.graph.model_dump_json()
    if service.validate_graph(source_before.graph).graph_hash != SOURCE_GRAPH_HASH:
        raise RuntimeError("source graph hash changed before shadow finalization")

    saved_audit = service.get_experiment(AUDIT_EXPERIMENT_ID)
    audit_run = service.get_run(AUDIT_RUN_ID)
    if saved_audit.graph.experiment.name != MODULE_NAME:
        raise RuntimeError("saved shadow-audit module name mismatch")
    if audit_run.status != "completed" or audit_run.graph_hash != AUDIT_GRAPH_HASH:
        raise RuntimeError("saved shadow-audit module run mismatch")

    saved = service.get_experiment(FINAL_EXPERIMENT_ID)
    history = service.get_run(FINAL_RUN_ID)
    if saved.graph.experiment.name != MODEL_NAME:
        raise RuntimeError("saved final strategy name mismatch")
    if history.status != "completed" or history.graph_hash != FINAL_GRAPH_HASH:
        raise RuntimeError("saved final strategy run mismatch")
    expected_graph = build_shadow_strategy_graph(AUDIT_RUN_ID)
    validation = service.validate_graph(saved.graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])
    expected_validation = service.validate_graph(expected_graph)
    if validation.graph_hash != expected_validation.graph_hash:
        raise RuntimeError("saved final graph differs from the reproducible graph")

    baseline = _metrics(service, SOURCE_RUN_ID)
    materialized = _metrics(service, FINAL_RUN_ID)
    _assert_frozen_gate(baseline, materialized)
    factor_value_equivalence = _factor_value_equivalence()
    cost_sensitivity = {
        str(int(cost)): _backtest(
            service,
            FINAL_RUN_ID,
            market="US",
            period=US_FULL,
            portfolio=PORTFOLIO,
            cost_bps=cost,
        )
        for cost in COSTS_BPS
    }
    full_result = service.run_backtest(
        FINAL_RUN_ID,
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
    source_after = service.get_experiment(SOURCE_EXPERIMENT_ID)
    source_unchanged = (
        source_after.graph.model_dump_json() == source_json
        and service.validate_graph(source_after.graph).graph_hash
        == SOURCE_GRAPH_HASH
    )
    if not source_unchanged:
        raise RuntimeError("source experiment changed during shadow finalization")

    benchmark_metrics = _benchmark_metrics(full_result)
    strategy_cumulative_return = float(full_result.summary.cumulative_return)
    strategy_sharpe = float(full_result.summary.sharpe)
    benchmark_goal_passed = all(
        strategy_cumulative_return > float(metrics["cumulative_return"])
        and strategy_sharpe > float(metrics["sharpe"])
        for metrics in benchmark_metrics.values()
    )

    payload = {
        "status": "verified_materialized_shadow_audit",
        "model_name": MODEL_NAME,
        "experiment_id": FINAL_EXPERIMENT_ID,
        "history_run_id": FINAL_RUN_ID,
        "graph_hash": FINAL_GRAPH_HASH,
        "source": {
            "experiment_id": SOURCE_EXPERIMENT_ID,
            "run_id": SOURCE_RUN_ID,
            "graph_hash": SOURCE_GRAPH_HASH,
            "unchanged": source_unchanged,
        },
        "shadow_audit_module": {
            "model_name": MODULE_NAME,
            "experiment_id": AUDIT_EXPERIMENT_ID,
            "history_run_id": AUDIT_RUN_ID,
            "graph_hash": AUDIT_GRAPH_HASH,
            "quality": _jsonable(audit_run.quality),
            "live_weight": 0.0,
            "checklist": [
                "sales_growth_3y > 0",
                "gross_profitability_pct > 0",
                "fcf_to_ev_yield > 0",
                "current_ratio > 1",
                "tr_6_1 > 0",
            ],
            "qualified_rnd_rule": "R&D rank is scored only when >=3 checks pass",
        },
        "portfolio": asdict(PORTFOLIO),
        "transaction_cost_bps": PRIMARY_COST_BPS,
        "baseline_50bps": baseline,
        "materialized_shadow_strategy_50bps": materialized,
        "metric_deltas": _metric_deltas(baseline, materialized),
        "factor_value_equivalence": factor_value_equivalence,
        "factor_scores_exactly_preserved": True,
        "backtest_metrics_exactly_preserved": False,
        "execution_path_note": (
            "The source experiment ends in date_fallback and is backtested through its "
            "special primary/fallback dispatch.  The new experiment materializes the "
            "already-computed composite score as a normal lab factor.  Factor values are "
            "bit-for-bit identical, while the materialized path removes the early-period "
            "dispatch difference.  This is an execution-path correction, not photo-factor alpha."
        ),
        "cost_sensitivity": cost_sensitivity,
        "observed_period": [
            full_result.equity_curve[0].trade_date.isoformat(),
            full_result.equity_curve[-1].trade_date.isoformat(),
        ],
        "cash_day_count": _cash_day_count(full_result),
        "benchmark_metrics": benchmark_metrics,
        "benchmark_goal_passed": benchmark_goal_passed,
        "rejected_research_reports": [
            "deliverables/us_pvgo_balanced_checklist_confirmation_research_20260907.json",
            "deliverables/us_pvgo_innovation_efficiency_value_confirmation_research_20260907.json",
            "deliverables/us_pvgo_photo_feedback_research_20260907.json",
        ],
        "interpretation": (
            "The photo-derived factors failed the frozen performance gate.  They are "
            "therefore retained as a forward shadow audit at zero live weight.  The small "
            "verified improvement comes only from materializing the existing fallback score."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    payload = run()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
