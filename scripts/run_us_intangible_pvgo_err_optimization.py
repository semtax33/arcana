from __future__ import annotations

"""Optimize and persist a new US PVGO Expectation Revision & Rerating model.

Research candidates are run inline and are never saved as experiments.  Only the
pre-holdout winner is saved, using ``save_experiment`` so the frozen level-only
benchmark can neither be updated nor reused by name.
"""

from dataclasses import asdict, is_dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
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
from scripts.build_us_intangible_pvgo_err import (
    DEFAULT_FOUNDATION_WEIGHTS,
    END_DATE,
    MAX_POSITIONS,
    MODEL_NAME,
    ORIGINAL_LEVEL_ONLY_MODEL_NAME,
    QUALITY_HEAVY_FOUNDATION_WEIGHTS,
    START_DATE,
    TOP_PERCENT,
    VALUE_HEAVY_FOUNDATION_WEIGHTS,
    ErrGraphSpec,
    build_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test
from scripts.run_us_pvgo_clean_control_experiment import _paired_diagnostic


TRAIN = (START_DATE, date(2021, 12, 31))
VALIDATION = (date(2022, 1, 3), date(2023, 12, 29))
HOLDOUT = (date(2024, 1, 2), END_DATE)
SELECTION_COST_BPS = 50.0
FINAL_COST_SCENARIOS_BPS = (20.0, 50.0, 100.0)
DEFAULT_OUTPUT = Path("deliverables/pvgo_err_optimization_20260902.json")


CANDIDATE_SPECS: dict[str, ErrGraphSpec] = {
    "level_baseline": ErrGraphSpec(
        style="level_baseline",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
    ),
    "additive_balanced": ErrGraphSpec(
        style="additive",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
    ),
    "multiplicative_balanced_025": ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
        revision_acceleration=0.25,
        recognition_acceleration=0.25,
    ),
    "multiplicative_balanced_050": ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
        revision_acceleration=0.50,
        recognition_acceleration=0.50,
    ),
    "multiplicative_balanced_075": ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
        revision_acceleration=0.75,
        recognition_acceleration=0.75,
    ),
    "multiplicative_value_050": ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(VALUE_HEAVY_FOUNDATION_WEIGHTS),
        revision_acceleration=0.50,
        recognition_acceleration=0.50,
    ),
    "multiplicative_quality_050": ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(QUALITY_HEAVY_FOUNDATION_WEIGHTS),
        revision_acceleration=0.50,
        recognition_acceleration=0.50,
    ),
}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def _graph_hash(graph: FactorLabGraphDto) -> str:
    payload = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_frozen_benchmark(service: FactorLabService) -> dict[str, Any]:
    experiment = service.get_experiment_by_name(ORIGINAL_LEVEL_ONLY_MODEL_NAME)
    return {
        "experiment_id": experiment.experiment_id,
        "name": experiment.graph.experiment.name,
        "graph_hash_sha256": _graph_hash(experiment.graph),
    }


def _run_history(
    service: FactorLabService,
    graph: FactorLabGraphDto,
    *,
    experiment_id: str | None = None,
) -> Any:
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in validation.errors]
        )
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=None if experiment_id else graph,
            experiment_id=experiment_id,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=END_DATE,
            history_rebalance_frequency="quarterly",
        )
    )


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    transaction_cost_bps: float,
) -> tuple[dict[str, Any], pd.Series, dict[str, set[str]]]:
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
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
    returns = nav.pct_change(fill_method=None).dropna()
    holdings = {
        str(rebalance.signal_date): {
            position.security_id for position in rebalance.positions
        }
        for rebalance in result.rebalance_history
    }
    return (
        {
            "period": [str(period[0]), str(period[1])],
            "transaction_cost_bps": transaction_cost_bps,
            "metrics": _jsonable(result.summary),
            "annual_returns": [_jsonable(item) for item in result.annual_returns],
            "newey_west_mean_test": newey_west_mean_test(returns),
            "rebalance_count": len(result.rebalance_history),
            "warnings": list(result.warnings),
        },
        returns,
        holdings,
    )


def _metric(backtest: dict[str, Any], name: str) -> float | None:
    value = backtest.get("metrics", {}).get(name)
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _selection_score(
    train: dict[str, Any],
    validation: dict[str, Any],
) -> float:
    """Pre-declared robustness score; the holdout is deliberately unavailable."""

    train_sharpe = _metric(train, "sharpe")
    validation_sharpe = _metric(validation, "sharpe")
    train_mdd = _metric(train, "max_drawdown")
    validation_mdd = _metric(validation, "max_drawdown")
    if train_sharpe is None or validation_sharpe is None:
        return float("-inf")
    worst_drawdown = max(
        abs(train_mdd or 0.0),
        abs(validation_mdd or 0.0),
    )
    drawdown_penalty = 0.50 * max(0.0, worst_drawdown - 0.40)
    return (
        min(train_sharpe, validation_sharpe)
        + 0.25 * np.mean([train_sharpe, validation_sharpe])
        - drawdown_penalty
    )


def _candidate_name(label: str) -> str:
    return f"{MODEL_NAME}__research__{label}"


def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    frozen_before = _read_frozen_benchmark(service)
    candidate_results: dict[str, Any] = {}
    research_runtime: dict[str, tuple[Any, pd.Series, dict[str, set[str]]]] = {}

    for label, spec in CANDIDATE_SPECS.items():
        print(f"[PVGO-ERR] history candidate={label}", flush=True)
        graph = build_graph(name=_candidate_name(label), spec=spec)
        history = _run_history(service, graph)
        print(f"[PVGO-ERR] train candidate={label}", flush=True)
        train_result, _train_returns, _train_holdings = _backtest(
            service,
            history.run_id,
            period=TRAIN,
            transaction_cost_bps=SELECTION_COST_BPS,
        )
        print(f"[PVGO-ERR] validation candidate={label}", flush=True)
        validation_result, validation_returns, validation_holdings = _backtest(
            service,
            history.run_id,
            period=VALIDATION,
            transaction_cost_bps=SELECTION_COST_BPS,
        )
        score = _selection_score(train_result, validation_result)
        candidate_results[label] = {
            "spec": asdict(spec),
            "research_history_run_id": history.run_id,
            "graph_hash": history.graph_hash,
            "history_quality": history.quality.model_dump(mode="json"),
            "train_50bps": train_result,
            "validation_50bps": validation_result,
            "selection_score": score,
        }
        research_runtime[label] = (
            history,
            validation_returns,
            validation_holdings,
        )

    eligible_labels = [label for label in CANDIDATE_SPECS if label != "level_baseline"]
    selected_label = max(
        eligible_labels,
        key=lambda label: candidate_results[label]["selection_score"],
    )
    selected_spec = CANDIDATE_SPECS[selected_label]
    selected_score = float(candidate_results[selected_label]["selection_score"])
    baseline_score = float(
        candidate_results["level_baseline"]["selection_score"]
    )
    beats_same_sample_baseline = selected_score > baseline_score
    print(f"[PVGO-ERR] frozen winner={selected_label}", flush=True)

    final_graph = build_graph(name=MODEL_NAME, spec=selected_spec)
    if final_graph.experiment.name == ORIGINAL_LEVEL_ONLY_MODEL_NAME:
        raise RuntimeError("new strategy name unexpectedly matches frozen benchmark")
    final_validation = service.validate_graph(final_graph)
    if not final_validation.valid:
        raise RuntimeError(
            [issue.model_dump(mode="json") for issue in final_validation.errors]
        )

    # Deliberately do not use save_experiment_by_name: a fresh experiment ID is
    # part of the non-overwrite contract.
    saved = service.save_experiment(
        FactorLabExperimentSaveRequestDto(graph=final_graph)
    )
    if saved.experiment_id == frozen_before["experiment_id"]:
        raise RuntimeError("new strategy reused the frozen experiment ID")
    print(f"[PVGO-ERR] saved new experiment={saved.experiment_id}", flush=True)
    final_history = _run_history(
        service,
        final_graph,
        experiment_id=saved.experiment_id,
    )
    final_screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen")
    )

    final_costs: dict[str, Any] = {}
    final_primary_returns: pd.Series | None = None
    final_primary_holdings: dict[str, set[str]] | None = None
    for cost in FINAL_COST_SCENARIOS_BPS:
        print(f"[PVGO-ERR] full backtest cost={cost:g}bp", flush=True)
        result, returns, holdings = _backtest(
            service,
            final_history.run_id,
            period=(START_DATE, END_DATE),
            transaction_cost_bps=cost,
        )
        final_costs[f"cost_{int(cost)}bps"] = result
        if cost == SELECTION_COST_BPS:
            final_primary_returns = returns
            final_primary_holdings = holdings

    print("[PVGO-ERR] untouched holdout evaluation", flush=True)
    holdout_result, holdout_returns, holdout_holdings = _backtest(
        service,
        final_history.run_id,
        period=HOLDOUT,
        transaction_cost_bps=SELECTION_COST_BPS,
    )

    baseline_history = research_runtime["level_baseline"][0]
    baseline_full, baseline_returns, baseline_holdings = _backtest(
        service,
        baseline_history.run_id,
        period=(START_DATE, END_DATE),
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    baseline_holdout, baseline_holdout_returns, baseline_holdout_holdings = _backtest(
        service,
        baseline_history.run_id,
        period=HOLDOUT,
        transaction_cost_bps=SELECTION_COST_BPS,
    )

    if final_primary_returns is None or final_primary_holdings is None:
        raise RuntimeError("primary cost scenario was not evaluated")

    frozen_after = _read_frozen_benchmark(service)
    frozen_unchanged = frozen_before == frozen_after
    if not frozen_unchanged:
        raise RuntimeError("frozen level-only benchmark changed during optimization")

    result = {
        "strategy": {
            "new_model_name": MODEL_NAME,
            "new_experiment_id": saved.experiment_id,
            "history_run_id": final_history.run_id,
            "screen_run_id": final_screen.run_id,
            "graph_hash": final_history.graph_hash,
            "selected_candidate": selected_label,
            "selected_spec": asdict(selected_spec),
            "classification": (
                "replacement_candidate"
                if beats_same_sample_baseline
                else "research_shadow_risk_controlled"
            ),
        },
        "promotion_evaluation": {
            "selected_enhanced_score": selected_score,
            "same_sample_level_baseline_score": baseline_score,
            "beats_same_sample_level_baseline": beats_same_sample_baseline,
            "decision": (
                "eligible_for_replacement_review"
                if beats_same_sample_baseline
                else "do_not_replace_frozen_strategy"
            ),
        },
        "non_overwrite_audit": {
            "frozen_before": frozen_before,
            "frozen_after": frozen_after,
            "unchanged": frozen_unchanged,
            "new_id_is_distinct": saved.experiment_id != frozen_before["experiment_id"],
            "save_method": "save_experiment (fresh UUID), not save_experiment_by_name",
        },
        "research_design": {
            "train": [str(TRAIN[0]), str(TRAIN[1])],
            "validation": [str(VALIDATION[0]), str(VALIDATION[1])],
            "untouched_holdout": [str(HOLDOUT[0]), str(HOLDOUT[1])],
            "selection_cost_bps": SELECTION_COST_BPS,
            "selection_rule": (
                "min(train Sharpe, validation Sharpe) + 0.25 * mean Sharpe "
                "- 0.50 * drawdown excess over 40%; holdout excluded"
            ),
            "candidate_count_including_baseline": len(CANDIDATE_SPECS),
            "rebalance_frequency": "quarterly",
            "signal_lag_days": 1,
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
        },
        "candidates": candidate_results,
        "holdout_50bps": holdout_result,
        "full_period_cost_scenarios": final_costs,
        "level_baseline_50bps": {
            "full_period": baseline_full,
            "holdout": baseline_holdout,
        },
        "paired_diagnostics_50bps": {
            "full_ERR_minus_level": _paired_diagnostic(
                final_primary_returns,
                baseline_returns,
                final_primary_holdings,
                baseline_holdings,
            ),
            "holdout_ERR_minus_level": _paired_diagnostic(
                holdout_returns,
                baseline_holdout_returns,
                holdout_holdings,
                baseline_holdout_holdings,
            ),
        },
        "final_history_quality": final_history.quality.model_dump(mode="json"),
        "final_screen_quality": final_screen.quality.model_dump(mode="json"),
        "limitations": [
            "PEAD is a proxy using PIT EPS surprise and sector-relative one-month return, not an exact announcement-window return.",
            "Price/target-price is optional and lower-is-better because historical analyst coverage is sparse; it never defines eligibility.",
            "The finite candidate family reduces but does not eliminate strategy-selection bias.",
            "Backtests are research evidence, not a guarantee of future returns.",
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
