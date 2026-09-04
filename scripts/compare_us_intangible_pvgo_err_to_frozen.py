from __future__ import annotations

"""Compare the new ERR model with the frozen model on an identical window.

The frozen graph is read and run inline.  It is never saved, so its experiment
row and graph remain untouched.
"""

import json
from pathlib import Path
from typing import Any

from api.service.factor_lab_service import FactorLabService
from scripts.build_us_intangible_pvgo_err import (
    END_DATE,
    MODEL_NAME,
    ORIGINAL_LEVEL_ONLY_MODEL_NAME,
    START_DATE,
)
from scripts.run_us_intangible_pvgo_err_optimization import (
    FINAL_COST_SCENARIOS_BPS,
    HOLDOUT,
    SELECTION_COST_BPS,
    _backtest,
    _graph_hash,
    _paired_diagnostic,
    _run_history,
)


DEFAULT_OPTIMIZATION = Path("deliverables/pvgo_err_optimization_20260902.json")
DEFAULT_OUTPUT = Path("deliverables/pvgo_err_vs_frozen_common_window_20260902.json")


def _snapshot(service: FactorLabService) -> tuple[Any, dict[str, str]]:
    experiment = service.get_experiment_by_name(ORIGINAL_LEVEL_ONLY_MODEL_NAME)
    return experiment, {
        "experiment_id": experiment.experiment_id,
        "name": experiment.graph.experiment.name,
        "graph_hash_sha256": _graph_hash(experiment.graph),
    }


def run(
    optimization_path: Path = DEFAULT_OPTIMIZATION,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    optimization = json.loads(optimization_path.read_text(encoding="utf-8"))
    new_history_run_id = str(optimization["strategy"]["history_run_id"])
    service = FactorLabService()
    frozen, frozen_before = _snapshot(service)

    print("[PVGO-ERR-COMPARE] frozen history inline (no experiment save)", flush=True)
    frozen_history = _run_history(service, frozen.graph)
    frozen_costs: dict[str, Any] = {}
    frozen_returns_50 = None
    frozen_holdings_50 = None
    for cost in FINAL_COST_SCENARIOS_BPS:
        result, returns, holdings = _backtest(
            service,
            frozen_history.run_id,
            period=(START_DATE, END_DATE),
            transaction_cost_bps=cost,
        )
        frozen_costs[f"cost_{int(cost)}bps"] = result
        if cost == SELECTION_COST_BPS:
            frozen_returns_50 = returns
            frozen_holdings_50 = holdings

    frozen_holdout, frozen_holdout_returns, frozen_holdout_holdings = _backtest(
        service,
        frozen_history.run_id,
        period=HOLDOUT,
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    new_common, new_returns, new_holdings = _backtest(
        service,
        new_history_run_id,
        period=(START_DATE, END_DATE),
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    new_holdout, new_holdout_returns, new_holdout_holdings = _backtest(
        service,
        new_history_run_id,
        period=HOLDOUT,
        transaction_cost_bps=SELECTION_COST_BPS,
    )
    if frozen_returns_50 is None or frozen_holdings_50 is None:
        raise RuntimeError("frozen 50bp scenario was not evaluated")

    _frozen_after_experiment, frozen_after = _snapshot(service)
    if frozen_before != frozen_after:
        raise RuntimeError("frozen experiment changed during inline comparison")

    result = {
        "models": {
            "new": {
                "name": MODEL_NAME,
                "experiment_id": optimization["strategy"]["new_experiment_id"],
                "history_run_id": new_history_run_id,
            },
            "frozen": {
                **frozen_before,
                "inline_comparison_run_id": frozen_history.run_id,
            },
        },
        "common_period": [str(START_DATE), str(END_DATE)],
        "frozen_cost_scenarios": frozen_costs,
        "new_50bps": new_common,
        "holdout_50bps": {
            "new": new_holdout,
            "frozen": frozen_holdout,
        },
        "paired_diagnostics_50bps": {
            "common_period_new_minus_frozen": _paired_diagnostic(
                new_returns,
                frozen_returns_50,
                new_holdings,
                frozen_holdings_50,
            ),
            "holdout_new_minus_frozen": _paired_diagnostic(
                new_holdout_returns,
                frozen_holdout_returns,
                new_holdout_holdings,
                frozen_holdout_holdings,
            ),
        },
        "non_overwrite_audit": {
            "frozen_before": frozen_before,
            "frozen_after": frozen_after,
            "unchanged": frozen_before == frozen_after,
            "comparison_method": "inline graph run; no experiment save/update",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
