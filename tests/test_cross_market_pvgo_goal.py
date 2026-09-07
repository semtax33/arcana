from __future__ import annotations

import json
from pathlib import Path


RESULT_PATH = Path(
    "deliverables/cross_market_pvgo_benchmark_strategy_20260907.json"
)
PVGO_CORE = {
    "pvgo_gap_pct",
    "roiic_wacc_spread",
    "pvgo_compression_pct",
}


def test_market_adaptive_pvgo_strategy_meets_the_full_period_goal() -> None:
    assert RESULT_PATH.exists(), "cross-market FactorLab result is missing"
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    assert payload["goal_passed"] is True
    assert payload["objective"] == (
        "beat each local benchmark by cumulative return with Sharpe above 1; "
        "then maximize Sharpe and CAGR while minimizing absolute MDD"
    )
    assert payload["transaction_cost_bps"] == 50.0
    assert payload["us_blend_policy"] == "when_primary_has_no_positions"

    expected_periods = {
        "KR": ["2002-04-01", "2026-09-04"],
        "US": ["2016-01-04", "2026-09-04"],
    }
    expected_benchmarks = {
        "KR": {"KOSPI200", "KOSDAQ"},
        "US": {"US_QQQ", "US_SP500"},
    }
    for market in ("KR", "US"):
        result = payload["markets"][market]
        assert result["experiment_id"]
        assert PVGO_CORE.issubset(set(result["factor_ids"]))
        assert result["observed_period"] == expected_periods[market]
        assert result["full_trading_day_coverage"] is True
        assert result["strategy_metrics"]["sharpe"] > 1.0
        assert set(result["benchmark_metrics"]) == expected_benchmarks[market]
        for benchmark in result["benchmark_metrics"].values():
            assert (
                result["strategy_metrics"]["cumulative_return"]
                > benchmark["cumulative_return"]
            )
        assert result["goal_passed"] is True

    us = payload["markets"]["US"]
    assert us["model_name"] == (
        "Arcana_US_PVGO_ConfirmationFallback_Quarterly_2016_2026_20260907"
    )
    assert us["composite_matches_service_blend"] is True
    assert us["experiment_id"] not in {
        us["primary_experiment_id"],
        us["fallback_experiment_id"],
    }
    assert us["fallback_experiment_id"]
    assert PVGO_CORE.issubset(set(us["fallback_factor_ids"]))
    assert us["cash_day_count"] > 0
