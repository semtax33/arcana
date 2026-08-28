from __future__ import annotations

"""Small, dependency-light diagnostics shared by FactorLab research scripts."""

import math
from typing import Any

import numpy as np
import pandas as pd


def newey_west_mean_test(
    returns: pd.Series,
    *,
    max_lags: int | None = None,
) -> dict[str, Any]:
    """HAC test of the daily mean return with a Bartlett kernel."""

    values = pd.to_numeric(returns, errors="coerce").dropna().to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    observations = int(values.size)
    if observations < 3:
        return {"status": "insufficient_observations", "observations": observations}

    lags = (
        max(int(max_lags), 0)
        if max_lags is not None
        else max(1, int(math.floor(4 * (observations / 100) ** (2 / 9))))
    )
    lags = min(lags, observations - 1)
    mean_return = float(values.mean())
    residuals = values - mean_return
    long_run_variance = float(np.dot(residuals, residuals) / observations)
    for lag in range(1, lags + 1):
        autocovariance = float(
            np.dot(residuals[lag:], residuals[:-lag]) / observations
        )
        bartlett_weight = 1 - lag / (lags + 1)
        long_run_variance += 2 * bartlett_weight * autocovariance
    standard_error = math.sqrt(max(long_run_variance, 0.0) / observations)
    t_stat = mean_return / standard_error if standard_error > 0 else math.nan
    p_value = math.erfc(abs(t_stat) / math.sqrt(2)) if math.isfinite(t_stat) else math.nan
    return {
        "status": "ok",
        "observations": observations,
        "max_lags": lags,
        "mean_daily_return": mean_return,
        "annualized_mean_return": mean_return * 252,
        "hac_standard_error_daily_mean": standard_error,
        "t_stat": t_stat,
        "two_sided_normal_p_value": p_value,
        "note": "Newey-West/Bartlett HAC; does not correct for strategy selection",
    }
