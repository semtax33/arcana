from __future__ import annotations

"""Save and evaluate the restored continuous KR PVGO model as a new experiment."""

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService
from scripts.build_kr_pvgo_expectations_alpha_continuous import (
    BRIDGE_MODEL_NAME,
    END_DATE,
    FINAL_WEIGHTS,
    MAX_POSITIONS,
    MODEL_NAME,
    REBALANCE_FREQUENCY,
    START_DATE,
    TOP_PERCENT,
    US_SOURCE_MODEL_NAME,
    build_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


COST_SCENARIOS_BPS = (20.0, 50.0, 100.0)
BENCHMARKS = ("KOSPI200", "KOSDAQ")
# A quarterly run beginning on the first KR trading day of 2002 asks for the
# preceding signal day (2001-12-28).  The requested evaluation window remains
# 2002-2026, while the first score is therefore generated at the next quarter.
HISTORY_START_DATE = date(2002, 4, 1)
DEFAULT_OUTPUT = Path(
    "deliverables/kr_pvgo_expectations_alpha_continuous_2002_2026_20260906.json"
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def _graph_hash(graph: Any) -> str:
    payload = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_snapshot(service: FactorLabService, name: str) -> dict[str, str]:
    source = service.get_experiment_by_name(name)
    return {
        "name": source.graph.experiment.name,
        "experiment_id": source.experiment_id,
        "graph_hash_sha256": _graph_hash(source.graph),
    }


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    transaction_cost_bps: float,
) -> dict[str, Any]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=START_DATE,
            end_date=END_DATE,
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="KR",
            benchmarks=list(BENCHMARKS),
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=transaction_cost_bps,
        ),
    )
    nav = pd.Series(
        [point.strategy_nav for point in result.equity_curve],
        index=pd.to_datetime([point.trade_date for point in result.equity_curve]),
        dtype="float64",
    ).sort_index()
    returns = nav.pct_change(fill_method=None).dropna()
    terminal_benchmarks: dict[str, float | None] = {}
    if result.equity_curve:
        for benchmark_id, benchmark_nav in result.equity_curve[-1].benchmark_navs.items():
            terminal_benchmarks[benchmark_id] = (
                float(benchmark_nav - 1.0) if benchmark_nav is not None else None
            )
    return {
        "transaction_cost_bps": transaction_cost_bps,
        "metrics": _jsonable(result.summary),
        "benchmark_terminal_returns": terminal_benchmarks,
        "annual_returns": [_jsonable(item) for item in result.annual_returns],
        "newey_west_mean_test": newey_west_mean_test(returns),
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def run(
    output_path: Path = DEFAULT_OUTPUT,
    *,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    service = FactorLabService()
    source_before = {
        "us": _source_snapshot(service, US_SOURCE_MODEL_NAME),
        "kr_bridge": _source_snapshot(service, BRIDGE_MODEL_NAME),
    }
    graph = build_graph()
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in validation.errors]
        )

    if experiment_id is None:
        # Never use the name-upsert method: every new strategy receives a fresh UUID.
        saved = service.save_experiment(FactorLabExperimentSaveRequestDto(graph=graph))
        save_method = "save_experiment (fresh UUID), not name-based upsert"
    else:
        # A history/backtest failure must be resumable without creating a second
        # experiment with the same name.
        saved = service.get_experiment(experiment_id)
        if _graph_hash(saved.graph) != _graph_hash(graph):
            raise RuntimeError("the resumed experiment graph does not match the contract")
        save_method = "resumed the already-created fresh experiment UUID"
    source_ids = {item["experiment_id"] for item in source_before.values()}
    if saved.experiment_id in source_ids:
        raise RuntimeError("the continuous strategy reused a source experiment ID")

    print(f"[KR-PVGO-CONTINUOUS] saved new experiment={saved.experiment_id}", flush=True)
    history = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=saved.experiment_id,
            mode="history",
            history_start_date=HISTORY_START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )
    costs = {
        f"cost_{int(cost)}bps": _backtest(
            service,
            history.run_id,
            transaction_cost_bps=cost,
        )
        for cost in COST_SCENARIOS_BPS
    }
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )
    source_after = {
        "us": _source_snapshot(service, US_SOURCE_MODEL_NAME),
        "kr_bridge": _source_snapshot(service, BRIDGE_MODEL_NAME),
    }
    if source_before != source_after:
        raise RuntimeError("a source strategy changed during the continuous run")

    result = {
        "strategy": {
            "model_name": MODEL_NAME,
            "experiment_id": saved.experiment_id,
            "history_run_id": history.run_id,
            "screen_run_id": screen.run_id,
            "market": "KR",
            "period": [str(START_DATE), str(END_DATE)],
            "history_signal_period": [str(HISTORY_START_DATE), str(END_DATE)],
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "signal_lag_days": 1,
            "weights": FINAL_WEIGHTS,
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
            "benchmarks": list(BENCHMARKS),
        },
        "source_preservation_audit": {
            "before": source_before,
            "after": source_after,
            "unchanged": source_before == source_after,
            "new_experiment_id_is_distinct": saved.experiment_id not in source_ids,
            "save_method": save_method,
        },
        "factor_design": {
            "pvgo_gap_pct": 0.40,
            "roiic_wacc_based_pvgo_quality": 0.25,
            "pvgo_compression_pct": 0.20,
            "low_raw_pvgo_pct": 0.15,
            "financial_basis": "ttm",
            "intangible_adjustment": False,
            "snapshot_coverage_policy": "allow_missing_inputs",
            "missing_weight_renormalize": False,
        },
        "history_quality": history.quality.model_dump(mode="json"),
        "cost_scenarios": costs,
        "screen": {
            "quality": screen.quality.model_dump(mode="json"),
            "top20": [row.model_dump(mode="json") for row in screen.rows[:20]],
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-id")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.output, experiment_id=args.experiment_id),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
