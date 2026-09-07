from __future__ import annotations

"""Save and evaluate the Korean PVGO Expectations Alpha as a new experiment."""

from dataclasses import asdict, is_dataclass
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
from scripts.build_kr_pvgo_expectations_alpha import (
    EARLY_PERIOD,
    END_DATE,
    FINAL_WEIGHTS,
    LATE_PERIOD,
    MAX_POSITIONS,
    MODEL_NAME,
    REBALANCE_FREQUENCY,
    START_DATE,
    TOP_PERCENT,
    UNAVAILABLE_PERIOD,
    US_SOURCE_MODEL_NAME,
    build_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


COST_SCENARIOS_BPS = (20.0, 50.0, 100.0)
DEFAULT_OUTPUT = Path("deliverables/kr_pvgo_expectations_alpha_2002_2026_20260906.json")


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


def _source_snapshot(service: FactorLabService) -> dict[str, str]:
    source = service.get_experiment_by_name(US_SOURCE_MODEL_NAME)
    return {
        "name": source.graph.experiment.name,
        "experiment_id": source.experiment_id,
        "graph_hash_sha256": _graph_hash(source.graph),
    }


def _history_run(
    service: FactorLabService,
    *,
    experiment_id: str,
    period: tuple[Any, Any],
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment_id,
            mode="history",
            history_start_date=period[0],
            history_end_date=period[1],
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[Any, Any],
    transaction_cost_bps: float,
) -> tuple[dict[str, Any], pd.Series]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="KR",
            benchmarks=["KOSPI200", "KOSDAQ"],
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
    benchmark_terminal_returns: dict[str, float | None] = {}
    if result.equity_curve:
        for benchmark_id, benchmark_nav in result.equity_curve[-1].benchmark_navs.items():
            benchmark_terminal_returns[benchmark_id] = (
                float(benchmark_nav - 1.0) if benchmark_nav is not None else None
            )
    payload = {
        "period": [str(period[0]), str(period[1])],
        "transaction_cost_bps": transaction_cost_bps,
        "metrics": _jsonable(result.summary),
        "benchmark_terminal_returns": benchmark_terminal_returns,
        "annual_returns": [_jsonable(item) for item in result.annual_returns],
        "newey_west_mean_test": newey_west_mean_test(returns),
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }
    return payload, nav


def _stitched_diagnostic(
    early_nav: pd.Series,
    late_nav: pd.Series,
    *,
    transaction_cost_bps: float,
) -> dict[str, Any]:
    """Link two real FactorLab NAVs while treating the missing era as cash."""

    if early_nav.empty or late_nav.empty:
        return {"status": "unavailable", "reason": "one segment has no NAV"}
    early = early_nav / float(early_nav.iloc[0])
    late = late_nav / float(late_nav.iloc[0]) * float(early.iloc[-1])
    combined = pd.concat([early, late]).sort_index()
    combined = combined.loc[~combined.index.duplicated(keep="last")]
    terminal_nav = float(combined.iloc[-1])
    elapsed_years = (END_DATE - START_DATE).days / 365.25
    drawdown = combined / combined.cummax() - 1.0
    return {
        "status": "diagnostic_only",
        "transaction_cost_bps": transaction_cost_bps,
        "start_date": str(START_DATE),
        "end_date": str(END_DATE),
        "cumulative_return": terminal_nav - 1.0,
        "elapsed_period_cagr_with_cash_gap": terminal_nav ** (1.0 / elapsed_years) - 1.0,
        "max_drawdown_across_observed_segments": float(drawdown.min()),
        "cash_gap": [str(UNAVAILABLE_PERIOD[0]), str(UNAVAILABLE_PERIOD[1])],
        "cash_gap_return_assumption": 0.0,
        "warning": (
            "This links two genuine FactorLab backtests; it is not a single "
            "continuous FactorLab run because every PVGO input is absent in 2013-2015."
        ),
    }


def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    source_before = _source_snapshot(service)
    graph = build_graph()
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in validation.errors]
        )

    # A fresh UUID is required.  Name-based upsert would risk mutating a prior
    # experiment with the same name and is intentionally not used.
    saved = service.save_experiment(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    if saved.experiment_id == source_before["experiment_id"]:
        raise RuntimeError("the Korean strategy reused the U.S. source experiment ID")

    print(f"[KR-PVGO] saved new experiment={saved.experiment_id}", flush=True)
    segment_runs: dict[str, Any] = {}
    navs_by_cost: dict[float, dict[str, pd.Series]] = {
        cost: {} for cost in COST_SCENARIOS_BPS
    }
    for label, period in (("early_2002_2012", EARLY_PERIOD), ("late_2016_2026", LATE_PERIOD)):
        print(f"[KR-PVGO] history segment={label}", flush=True)
        history = _history_run(
            service,
            experiment_id=saved.experiment_id,
            period=period,
        )
        costs: dict[str, Any] = {}
        for cost in COST_SCENARIOS_BPS:
            print(f"[KR-PVGO] backtest segment={label} cost={cost:g}bp", flush=True)
            backtest, nav = _backtest(
                service,
                history.run_id,
                period=period,
                transaction_cost_bps=cost,
            )
            costs[f"cost_{int(cost)}bps"] = backtest
            navs_by_cost[cost][label] = nav
        segment_runs[label] = {
            "period": [str(period[0]), str(period[1])],
            "history_run_id": history.run_id,
            "graph_hash": history.graph_hash,
            "history_quality": history.quality.model_dump(mode="json"),
            "cost_scenarios": costs,
        }

    print("[KR-PVGO] latest screen", flush=True)
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )
    source_after = _source_snapshot(service)
    if source_before != source_after:
        raise RuntimeError("the U.S. source strategy changed during the Korean port")

    stitched = {
        f"cost_{int(cost)}bps": _stitched_diagnostic(
            navs["early_2002_2012"],
            navs["late_2016_2026"],
            transaction_cost_bps=cost,
        )
        for cost, navs in navs_by_cost.items()
    }
    result = {
        "strategy": {
            "model_name": MODEL_NAME,
            "experiment_id": saved.experiment_id,
            "screen_run_id": screen.run_id,
            "market": "KR",
            "nominal_period": [str(START_DATE), str(END_DATE)],
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "signal_lag_days": 1,
            "weights": FINAL_WEIGHTS,
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
        },
        "source_preservation_audit": {
            "before": source_before,
            "after": source_after,
            "unchanged": source_before == source_after,
            "new_experiment_id_is_distinct": (
                saved.experiment_id != source_before["experiment_id"]
            ),
            "save_method": "save_experiment (fresh UUID), not name-based upsert",
        },
        "factor_design": {
            "inputs": [
                "pvgo_gap_pct",
                "pvgo_pct",
                "roiic_wacc_spread",
                "pvgo_compression_pct",
            ],
            "intangible_adjustment": False,
            "topology": "exact score-node and edge clone of the U.S. source model",
            "missing_sleeve_policy": "available-sleeve weight renormalization",
            "financials_excluded": True,
        },
        "data_availability": {
            "factorlab_segments": {
                "early": [str(EARLY_PERIOD[0]), str(EARLY_PERIOD[1])],
                "late": [str(LATE_PERIOD[0]), str(LATE_PERIOD[1])],
            },
            "all_pvgo_inputs_absent": [
                str(UNAVAILABLE_PERIOD[0]),
                str(UNAVAILABLE_PERIOD[1]),
            ],
            "reason_for_split": (
                "FactorLab PIT history requires at least one requested input on every "
                "signal date; the persisted KR raw and snapshot tables both have no "
                "PVGO-family input in 2013-2015."
            ),
        },
        "segments": segment_runs,
        "stitched_cash_gap_diagnostics": stitched,
        "screen": {
            "run_id": screen.run_id,
            "quality": screen.quality.model_dump(mode="json"),
            "top20": [row.model_dump(mode="json") for row in screen.rows[:20]],
        },
        "limitations": [
            "The 2013-2015 data gap prevents a single continuous FactorLab history run.",
            "Early justified-PVGO coverage is sparse, so available-sleeve renormalization is material.",
            "Delisted-security history is incomplete, so survivor bias is not fully eliminated.",
            "The stitched result assumes zero cash return during the missing era and is diagnostic only.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
