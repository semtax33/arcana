from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.markets.registry import market_config
from engine.transformers._internal.erp_inputs import (
    SILVER_COUNTRY_ERP_PATH,
    SILVER_RISK_FREE_RATE_PATH,
)


SILVER_WACC_WEEKLY_RETURNS_PATH = DATA_LAKE.silver("wacc", "weekly_returns.csv")
SILVER_WACC_BENCHMARK_WEEKLY_RETURNS_PATH = DATA_LAKE.silver(
    "wacc",
    "benchmark_weekly_returns.csv",
)
SILVER_WACC_ASSUMPTIONS_PATH = DATA_LAKE.silver("wacc", "wacc_assumptions.csv")
BRONZE_US_SP500_BENCHMARK_PATH = DATA_LAKE.bronze(
    "yfinance",
    "benchmark",
    "us_sp500.csv",
)

WEEKLY_RETURN_COLUMNS = ["security_id", "week_end_date", "weekly_close", "weekly_return"]
BENCHMARK_WEEKLY_RETURN_COLUMNS = [
    "market",
    "benchmark_id",
    "week_end_date",
    "weekly_close",
    "weekly_return",
]
WACC_ASSUMPTION_COLUMNS = [
    "market",
    "country_code",
    "risk_free_rate",
    "equity_risk_premium",
    "credit_spread",
    "default_beta",
    "source",
    "updated_at",
]

DEFAULT_WACC_ASSUMPTIONS = {
    "kr": {
        "country_code": "KR",
        "risk_free_rate": 3.0,
        "equity_risk_premium": 5.0,
        "credit_spread": 2.0,
        "default_beta": 1.0,
    },
    "us": {
        "country_code": "US",
        "risk_free_rate": 4.0,
        "equity_risk_premium": 4.5,
        "credit_spread": 2.0,
        "default_beta": 1.0,
    },
}


def normalize_weekly_returns_from_prices(
    frame: pd.DataFrame,
    *,
    security_id: str | None = None,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=WEEKLY_RETURN_COLUMNS)

    df = frame.copy()
    if "trade_date" not in df.columns:
        raise ValueError("price frame is missing trade_date")
    price_col = _price_column(df)
    if price_col is None:
        raise ValueError("price frame is missing close or adj_close")

    if "security_id" not in df.columns:
        if security_id is None:
            raise ValueError("security_id is required when price frame has no security_id column")
        df["security_id"] = security_id

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["_price"] = pd.to_numeric(df[price_col], errors="coerce")
    df = df.dropna(subset=["security_id", "trade_date", "_price"])
    if df.empty:
        return pd.DataFrame(columns=WEEKLY_RETURN_COLUMNS)

    frames = []
    for sid, rows in df.sort_values("trade_date").groupby("security_id", sort=False):
        weekly = (
            rows.set_index("trade_date")["_price"]
            .resample("W-FRI")
            .last()
            .dropna()
            .reset_index()
        )
        if weekly.empty:
            continue
        weekly["security_id"] = sid
        weekly["week_end_date"] = weekly["trade_date"].dt.date
        weekly["weekly_close"] = weekly["_price"]
        weekly["weekly_return"] = weekly["weekly_close"].pct_change()
        frames.append(weekly[WEEKLY_RETURN_COLUMNS])

    return _concat_frames(frames, WEEKLY_RETURN_COLUMNS)


def normalize_benchmark_weekly_returns(
    frame: pd.DataFrame,
    *,
    market: str,
    benchmark_id: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BENCHMARK_WEEKLY_RETURN_COLUMNS)

    df = frame.copy()
    if "trade_date" not in df.columns:
        raise ValueError("benchmark frame is missing trade_date")
    price_col = _price_column(df)
    if price_col is None:
        raise ValueError("benchmark frame is missing close or adj_close")

    if "benchmark_id" not in df.columns:
        df["benchmark_id"] = benchmark_id
    df["market"] = str(market or "").strip().lower()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["_price"] = pd.to_numeric(df[price_col], errors="coerce")
    df = df.dropna(subset=["market", "benchmark_id", "trade_date", "_price"])
    if df.empty:
        return pd.DataFrame(columns=BENCHMARK_WEEKLY_RETURN_COLUMNS)

    frames = []
    for (mkt, bid), rows in df.sort_values("trade_date").groupby(["market", "benchmark_id"], sort=False):
        weekly = (
            rows.set_index("trade_date")["_price"]
            .resample("W-FRI")
            .last()
            .dropna()
            .reset_index()
        )
        if weekly.empty:
            continue
        weekly["market"] = mkt
        weekly["benchmark_id"] = bid
        weekly["week_end_date"] = weekly["trade_date"].dt.date
        weekly["weekly_close"] = weekly["_price"]
        weekly["weekly_return"] = weekly["weekly_close"].pct_change()
        frames.append(weekly[BENCHMARK_WEEKLY_RETURN_COLUMNS])

    return _concat_frames(frames, BENCHMARK_WEEKLY_RETURN_COLUMNS)


def calculate_rolling_beta(
    stock_weekly_returns: pd.DataFrame,
    benchmark_weekly_returns: pd.DataFrame,
    *,
    window: int = 104,
    min_periods: int = 52,
) -> pd.DataFrame:
    columns = ["week_end_date", "beta_raw", "beta"]
    if stock_weekly_returns is None or stock_weekly_returns.empty:
        return pd.DataFrame(columns=columns)
    if benchmark_weekly_returns is None or benchmark_weekly_returns.empty:
        return pd.DataFrame(columns=columns)

    stock = stock_weekly_returns[["week_end_date", "weekly_return"]].copy()
    bench = benchmark_weekly_returns[["week_end_date", "weekly_return"]].copy()
    stock["week_end_date"] = pd.to_datetime(stock["week_end_date"], errors="coerce")
    bench["week_end_date"] = pd.to_datetime(bench["week_end_date"], errors="coerce")
    stock["stock_return"] = pd.to_numeric(stock["weekly_return"], errors="coerce")
    bench["benchmark_return"] = pd.to_numeric(bench["weekly_return"], errors="coerce")
    merged = pd.merge(
        stock[["week_end_date", "stock_return"]],
        bench[["week_end_date", "benchmark_return"]],
        on="week_end_date",
        how="inner",
    ).dropna()
    if merged.empty:
        return pd.DataFrame(columns=columns)

    cov = merged["stock_return"].rolling(window, min_periods=min_periods).cov(
        merged["benchmark_return"]
    )
    var = merged["benchmark_return"].rolling(window, min_periods=min_periods).var()
    beta_raw = cov / var.replace(0, pd.NA)
    beta = (0.67 * beta_raw + 0.33).clip(lower=0.2, upper=2.5)
    result = pd.DataFrame(
        {
            "week_end_date": merged["week_end_date"].dt.date,
            "beta_raw": beta_raw,
            "beta": beta,
        }
    )
    return result.dropna(subset=["beta"]).reset_index(drop=True)


def default_wacc_assumptions(market: str = "kr") -> dict[str, float | str]:
    key = str(market or "kr").strip().lower()
    default = DEFAULT_WACC_ASSUMPTIONS.get(key)
    if default is None:
        config = market_config(key)
        default = {
            "country_code": config.country,
            "risk_free_rate": 4.0,
            "equity_risk_premium": 5.0,
            "credit_spread": 2.0,
            "default_beta": 1.0,
        }
    return dict(default)


def create_default_wacc_assumptions(output_path: str | Path | None = SILVER_WACC_ASSUMPTIONS_PATH) -> pd.DataFrame:
    now = datetime.now().replace(microsecond=0)
    rows = []
    for market, values in sorted(DEFAULT_WACC_ASSUMPTIONS.items()):
        rows.append(
            {
                "market": market,
                "country_code": values["country_code"],
                "risk_free_rate": values["risk_free_rate"],
                "equity_risk_premium": values["equity_risk_premium"],
                "credit_spread": values["credit_spread"],
                "default_beta": values["default_beta"],
                "source": "default",
                "updated_at": now,
            }
        )
    result = pd.DataFrame(rows, columns=WACC_ASSUMPTION_COLUMNS)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False, encoding="utf-8-sig")
    return result


def read_wacc_assumptions(path: str | Path | None = SILVER_WACC_ASSUMPTIONS_PATH) -> pd.DataFrame:
    path = Path(path) if path is not None else SILVER_WACC_ASSUMPTIONS_PATH
    if not path.exists():
        return create_default_wacc_assumptions(output_path=None)
    frame = pd.read_csv(path)
    if frame.empty:
        return create_default_wacc_assumptions(output_path=None)
    return frame


def market_assumption(frame: pd.DataFrame, market: str, key: str) -> float:
    default = default_wacc_assumptions(market)
    default_value = float(default[key])
    if frame is None or frame.empty or key not in frame.columns:
        return default_value
    if "market" not in frame.columns:
        return default_value
    market_key = str(market or "").strip().lower()
    rows = frame.loc[frame["market"].astype(str).str.lower() == market_key]
    if rows.empty:
        return default_value
    value = pd.to_numeric(rows[key], errors="coerce").dropna()
    return float(value.iloc[-1]) if not value.empty else default_value


def latest_country_erp(frame: pd.DataFrame, market: str, assumptions: pd.DataFrame | None = None) -> float:
    country_code = str(default_wacc_assumptions(market)["country_code"])
    if frame is not None and not frame.empty and "country_code" in frame.columns:
        rows = frame.loc[frame["country_code"].astype(str).str.upper() == country_code]
        if not rows.empty:
            values = pd.to_numeric(rows["equity_risk_premium"], errors="coerce").dropna()
            if not values.empty:
                return float(values.iloc[-1])
    return market_assumption(assumptions, market, "equity_risk_premium")


def risk_free_series_for_market(
    frame: pd.DataFrame,
    market: str,
    index: pd.Index,
    trade_dates: pd.Series,
    assumptions: pd.DataFrame | None = None,
) -> pd.Series:
    default_value = market_assumption(assumptions, market, "risk_free_rate")
    if frame is None or frame.empty or "market" not in frame.columns:
        return pd.Series(default_value, index=index, dtype="float64")

    rows = frame.loc[frame["market"].astype(str).str.lower() == str(market).lower()].copy()
    if rows.empty:
        return pd.Series(default_value, index=index, dtype="float64")

    rows["date"] = pd.to_datetime(rows["date"], errors="coerce")
    rows["risk_free_rate"] = pd.to_numeric(rows["risk_free_rate"], errors="coerce")
    rows = rows.dropna(subset=["date", "risk_free_rate"]).sort_values("date")
    if rows.empty:
        return pd.Series(default_value, index=index, dtype="float64")

    left = pd.DataFrame({"trade_date": pd.to_datetime(trade_dates, errors="coerce")}, index=index)
    merged = pd.merge_asof(
        left.sort_values("trade_date"),
        rows[["date", "risk_free_rate"]],
        left_on="trade_date",
        right_on="date",
        direction="backward",
    )
    merged.index = left.sort_values("trade_date").index
    result = merged["risk_free_rate"].reindex(index).fillna(default_value)
    return pd.to_numeric(result, errors="coerce")


def _price_column(df: pd.DataFrame) -> str | None:
    if "adj_close" in df.columns and pd.to_numeric(df["adj_close"], errors="coerce").notna().any():
        return "adj_close"
    if "close" in df.columns:
        return "close"
    return None


def _concat_frames(frames: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=columns)
    return pd.concat(non_empty, ignore_index=True).sort_values(columns[:2]).reset_index(drop=True)
