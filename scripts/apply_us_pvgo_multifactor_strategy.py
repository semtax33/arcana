from __future__ import annotations

"""Apply the frozen Korean PVGO multifactor strategy to the U.S. unchanged.

This is a transfer test, not a second optimization.  Nodes, factor directions,
weights, eligibility gate, portfolio construction, lag, and rebalance cadence
are inherited from the Korean winner.  Only market, name, and evaluation dates
change.
"""

from dataclasses import asdict
from datetime import date
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.discover_kr_pvgo_multifactor_strategy import (
    FROZEN_FINAL_PORTFOLIO,
    _frozen_final_spec,
    _reachable_factor_ids,
    build_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test
from scripts.optimize_kr_pvgo_expectations_alpha import (
    FINAL_COSTS_BPS,
    SELECTION_COST_BPS,
    _graph_hash,
    _jsonable,
    _run_history,
    _write_checkpoint,
)


MODEL_NAME = (
    "Arcana_US_PVGO_AssetDiscipline_LowVol_Quarterly_"
    "2017_2026_20260906"
)
OUTPUT_PATH = Path(
    "deliverables/us_pvgo_multifactor_transfer_20260906.json"
)
MISMATCH_PATH = Path(
    "deliverables/us_pvgo_multifactor_transfer_mismatch_20260906.json"
)
US_PERIOD = (date(2017, 1, 3), date(2026, 9, 4))
STABILITY_PERIODS = {
    "2017_2020": (date(2017, 1, 3), date(2020, 12, 31)),
    "2021_2023": (date(2021, 1, 4), date(2023, 12, 29)),
    "2024_2026": (date(2024, 1, 2), date(2026, 9, 4)),
}
US_BENCHMARKS = ("US_NASDAQ", "US_SP500")


def build_us_graph(
    *,
    start_date: date = US_PERIOD[0],
    end_date: date = US_PERIOD[1],
):
    graph = build_graph(
        MODEL_NAME,
        _frozen_final_spec(),
        start_date=start_date,
        end_date=end_date,
    )
    graph.experiment.market = "US"
    return graph


def _backtest_us(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    transaction_cost_bps: float,
) -> dict[str, Any]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=FROZEN_FINAL_PORTFOLIO.top_percent,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
            market="US",
            benchmarks=list(US_BENCHMARKS),
            max_positions=FROZEN_FINAL_PORTFOLIO.max_positions,
            transaction_cost_bps=transaction_cost_bps,
        ),
    )
    nav = pd.Series(
        [point.strategy_nav for point in result.equity_curve],
        index=pd.to_datetime(
            [point.trade_date for point in result.equity_curve]
        ),
        dtype="float64",
    ).sort_index()
    returns = nav.pct_change(fill_method=None).dropna()
    return {
        "period": [str(period[0]), str(period[1])],
        "portfolio": asdict(FROZEN_FINAL_PORTFOLIO),
        "transaction_cost_bps": transaction_cost_bps,
        "metrics": _jsonable(result.summary),
        "newey_west_mean_test": newey_west_mean_test(returns),
        "annual_returns": [_jsonable(row) for row in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def _same_strategy_audit(us_graph: Any) -> dict[str, Any]:
    kr_reference = build_graph(
        "same_strategy_audit_reference",
        _frozen_final_spec(),
        start_date=US_PERIOD[0],
        end_date=US_PERIOD[1],
    )
    kr_payload = kr_reference.model_dump(mode="json")
    us_payload = us_graph.model_dump(mode="json")
    checks = {
        "nodes_identical": kr_payload["nodes"] == us_payload["nodes"],
        "edges_identical": kr_payload["edges"] == us_payload["edges"],
        "outputs_identical": kr_payload["outputs"] == us_payload["outputs"],
        "universe_identical": (
            kr_payload["experiment"]["universe"]
            == us_payload["experiment"]["universe"]
        ),
        "rebalance_identical": (
            kr_payload["experiment"]["rebalance"]
            == us_payload["experiment"]["rebalance"]
        ),
        "market_is_us": us_payload["experiment"]["market"] == "US",
    }
    return {"passed": all(checks.values()), "checks": checks}


def _metrics_equivalent(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance: float = 1e-12,
) -> bool:
    if left.keys() != right.keys():
        return False
    for key in left:
        a, b = left[key], right[key]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if not math.isclose(float(a), float(b), rel_tol=tolerance, abs_tol=tolerance):
                return False
        elif a != b:
            return False
    return True


def run(output_path: Path = OUTPUT_PATH) -> dict[str, Any]:
    service = FactorLabService()
    graph = build_us_graph()
    audit = _same_strategy_audit(graph)
    if not audit["passed"]:
        raise RuntimeError(f"US graph changed the frozen strategy: {audit}")

    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(issue) for issue in validation.errors])

    # Execute inline first so a save never precedes proof that the current U.S.
    # dataset can evaluate every reachable node in the frozen graph.
    inline = _run_history(service, graph, period=US_PERIOD)
    inline_full = _backtest_us(
        service,
        inline.run_id,
        period=US_PERIOD,
        transaction_cost_bps=SELECTION_COST_BPS,
    )

    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    history = _run_history(
        service,
        graph,
        period=US_PERIOD,
        experiment_id=saved.experiment_id,
    )
    full = _backtest_us(
        service,
        history.run_id,
        period=US_PERIOD,
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    if (
        inline.graph_hash != history.graph_hash
        or not _metrics_equivalent(inline_full["metrics"], full["metrics"])
    ):
        mismatch = {
            "inline_run_id": inline.run_id,
            "saved_run_id": history.run_id,
            "inline_graph_hash": inline.graph_hash,
            "saved_graph_hash": history.graph_hash,
            "inline": inline_full,
            "saved": full,
        }
        _write_checkpoint(MISMATCH_PATH, mismatch)
        raise RuntimeError(
            "inline and saved U.S. experiment metrics differ; "
            f"diagnostic written to {MISMATCH_PATH}"
        )

    costs = {
        f"cost_{int(cost)}bps": _backtest_us(
            service,
            history.run_id,
            period=US_PERIOD,
            transaction_cost_bps=cost,
        )
        for cost in FINAL_COSTS_BPS
    }
    stability = {
        label: _backtest_us(
            service,
            history.run_id,
            period=period,
            transaction_cost_bps=SELECTION_COST_BPS,
        )
        for label, period in STABILITY_PERIODS.items()
    }
    screen = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=saved.experiment_id,
            mode="screen",
        )
    )
    factor_ids = _reachable_factor_ids(graph, validation.execution_order)

    payload = {
        "design": {
            "test_type": "locked out-of-market transfer; no U.S. tuning",
            "only_changes_from_kr": [
                "market: KR -> US",
                "name",
                "evaluation dates: 2017-01-03 through 2026-09-04",
            ],
            "primary_cost_bps": SELECTION_COST_BPS,
            "benchmarks": list(US_BENCHMARKS),
        },
        "us_transfer": {
            "model_name": MODEL_NAME,
            "experiment_id": saved.experiment_id,
            "market": "US",
            "graph_hash_sha256": _graph_hash(graph),
            "same_strategy_audit": audit,
            "spec": asdict(_frozen_final_spec()),
            "portfolio": asdict(FROZEN_FINAL_PORTFOLIO),
            "factor_ids": factor_ids,
            "performance_50bps": {
                "full_period": full,
                "stability_periods": stability,
            },
            "cost_scenarios": costs,
            "history_run_id": history.run_id,
            "history_quality": _jsonable(history.quality),
            "screen_run_id": screen.run_id,
            "screen_quality": _jsonable(screen.quality),
            "screen_top20": [_jsonable(row) for row in screen.rows[:20]],
            "limitations": [
                "The U.S. result is a transfer diagnostic, not an independently optimized model.",
                "Delisted-security history is incomplete, so survivor bias may remain.",
                "Flat bps costs do not model spread, market impact, borrow, ADV, taxes, or capacity.",
                "The backtest is in USD and does not include KRW/USD currency effects for a Korean investor.",
            ],
        },
    }
    _write_checkpoint(output_path, payload)
    return payload


if __name__ == "__main__":
    print(
        json.dumps(
            run()["us_transfer"],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
