from __future__ import annotations

"""Small, dependency-light diagnostics shared by FactorLab research scripts."""

import math
import csv
from functools import lru_cache
import io
import re
from typing import Any
from urllib.request import Request, urlopen
import zipfile

import numpy as np
import pandas as pd


FAMA_FRENCH_5_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)
FAMA_FRENCH_MOMENTUM_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Momentum_Factor_daily_CSV.zip"
)
FF6_COLUMNS = ["MKT_RF", "SMB", "HML", "RMW", "CMA", "MOM"]


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


def _parse_french_daily_text(
    text: str,
    *,
    value_columns: list[str],
) -> pd.DataFrame:
    rows: list[list[Any]] = []
    expected_columns = len(value_columns) + 1
    for raw_line in text.splitlines():
        if not re.match(r"^\s*\d{8}\s*,", raw_line):
            continue
        values = next(csv.reader([raw_line], skipinitialspace=True))
        if len(values) != expected_columns:
            continue
        trade_date = pd.to_datetime(values[0].strip(), format="%Y%m%d", errors="coerce")
        numeric_values = [pd.to_numeric(value.strip(), errors="coerce") for value in values[1:]]
        if pd.isna(trade_date) or any(pd.isna(value) for value in numeric_values):
            continue
        if any(abs(float(value)) >= 99.99 for value in numeric_values):
            continue
        rows.append([trade_date, *(float(value) / 100.0 for value in numeric_values)])
    if not rows:
        raise ValueError("Kenneth French daily factor file contained no usable rows")
    return (
        pd.DataFrame(rows, columns=["trade_date", *value_columns])
        .drop_duplicates("trade_date", keep="last")
        .set_index("trade_date")
        .sort_index()
    )


def _download_french_daily_zip(
    url: str,
    *,
    value_columns: list[str],
    timeout_seconds: float,
) -> pd.DataFrame:
    request = Request(url, headers={"User-Agent": "Arcana-FactorLab/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            raise ValueError("Kenneth French archive did not contain a CSV file")
        raw = archive.read(members[0])
    text = raw.decode("utf-8-sig", errors="replace")
    return _parse_french_daily_text(text, value_columns=value_columns)


@lru_cache(maxsize=1)
def load_fama_french_us_daily(timeout_seconds: float = 30.0) -> pd.DataFrame:
    """Load official U.S. FF5 plus momentum daily returns in decimal units."""

    ff5 = _download_french_daily_zip(
        FAMA_FRENCH_5_DAILY_URL,
        value_columns=["MKT_RF", "SMB", "HML", "RMW", "CMA", "RF"],
        timeout_seconds=timeout_seconds,
    )
    momentum = _download_french_daily_zip(
        FAMA_FRENCH_MOMENTUM_DAILY_URL,
        value_columns=["MOM"],
        timeout_seconds=timeout_seconds,
    )
    result = ff5.join(momentum, how="inner")
    if result.empty:
        raise ValueError("FF5 and momentum files had no overlapping daily observations")
    return result


def factor_model_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    *,
    max_lags: int | None = None,
) -> dict[str, Any]:
    """Estimate FF5+Momentum alpha with Newey-West coefficient covariance."""

    returns = pd.to_numeric(portfolio_returns, errors="coerce").rename("portfolio")
    returns.index = pd.to_datetime(returns.index)
    required_columns = [*FF6_COLUMNS, "RF"]
    missing_columns = [column for column in required_columns if column not in factors.columns]
    if missing_columns:
        raise ValueError(f"factor data is missing columns: {missing_columns}")
    panel = returns.to_frame().join(factors[required_columns], how="inner").dropna()
    observations = len(panel)
    parameter_count = len(FF6_COLUMNS) + 1
    if observations <= parameter_count + 2:
        return {"status": "insufficient_observations", "observations": observations}

    y = (panel["portfolio"] - panel["RF"]).to_numpy(dtype=float)
    factor_values = panel[FF6_COLUMNS].to_numpy(dtype=float)
    x = np.column_stack([np.ones(observations), factor_values])
    coefficients = np.linalg.pinv(x.T @ x) @ x.T @ y
    residuals = y - x @ coefficients
    lags = (
        max(int(max_lags), 0)
        if max_lags is not None
        else max(1, int(math.floor(4 * (observations / 100) ** (2 / 9))))
    )
    lags = min(lags, observations - 1)

    meat = np.zeros((parameter_count, parameter_count), dtype=float)
    for position in range(observations):
        xu = x[position] * residuals[position]
        meat += np.outer(xu, xu)
    for lag in range(1, lags + 1):
        weight = 1 - lag / (lags + 1)
        cross = np.zeros_like(meat)
        for position in range(lag, observations):
            current = x[position] * residuals[position]
            prior = x[position - lag] * residuals[position - lag]
            cross += np.outer(current, prior)
        meat += weight * (cross + cross.T)

    bread = np.linalg.pinv(x.T @ x)
    covariance = bread @ meat @ bread
    standard_errors = np.sqrt(np.clip(np.diag(covariance), 0.0, None))
    t_stats = np.divide(
        coefficients,
        standard_errors,
        out=np.full_like(coefficients, np.nan),
        where=standard_errors > 0,
    )
    p_values = [
        math.erfc(abs(float(value)) / math.sqrt(2))
        if math.isfinite(float(value))
        else math.nan
        for value in t_stats
    ]
    total_sum_squares = float(np.dot(y - y.mean(), y - y.mean()))
    residual_sum_squares = float(np.dot(residuals, residuals))
    r_squared = (
        1 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0
        else math.nan
    )
    names = ["alpha", *FF6_COLUMNS]
    estimates = {
        name: {
            "coefficient": float(coefficients[index]),
            "hac_standard_error": float(standard_errors[index]),
            "t_stat": float(t_stats[index]),
            "two_sided_normal_p_value": float(p_values[index]),
        }
        for index, name in enumerate(names)
    }
    estimates["alpha"]["annualized_coefficient"] = float(coefficients[0] * 252)
    return {
        "status": "ok",
        "observations": observations,
        "start_date": panel.index.min().date(),
        "end_date": panel.index.max().date(),
        "max_lags": lags,
        "r_squared": r_squared,
        "estimates": estimates,
        "source": "Kenneth R. French Data Library, U.S. daily FF5 and Momentum",
        "note": "HAC inference does not correct for multiple strategy searches",
    }


def load_and_run_factor_model(portfolio_returns: pd.Series) -> dict[str, Any]:
    try:
        factors = load_fama_french_us_daily()
        return factor_model_regression(portfolio_returns, factors)
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": str(exc),
            "source_urls": [
                FAMA_FRENCH_5_DAILY_URL,
                FAMA_FRENCH_MOMENTUM_DAILY_URL,
            ],
        }
