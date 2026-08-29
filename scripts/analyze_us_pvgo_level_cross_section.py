from __future__ import annotations

"""Common-sample IC and Fama-MacBeth tests for raw vs adjusted PVGO levels."""

from datetime import date
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from scripts.backfill_us_intangible_adjusted_pvgo import TARGET_DATES
from scripts.build_us_intangible_adjusted_pvgo import (
    END_DATE,
    MIN_MARKET_CAP_USD_MILLIONS,
    OPERATING_COMPANY_GICS_SECTORS,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


RAW_LEVEL_FACTOR = "equity_pvgo_pct"
ADJUSTED_LEVEL_FACTOR = "normalized_intangible_adjusted_pvgo_pct"
RAW_EARNINGS_FACTOR = "normalized_earnings_5y"
ADJUSTED_EARNINGS_FACTOR = "normalized_intangible_adjusted_earnings_5y"
DEFAULT_OUTPUT = Path("deliverables/pvgo_level_cross_section_20260829.json")


def _signal_dates() -> list[date]:
    dates = [date.fromisoformat(value) for value in TARGET_DATES]
    return [
        value
        for value in dates
        if date(2017, 3, 31) <= value <= date(2026, 6, 30)
    ]


def _factor_panel_query() -> str:
    return """
WITH
latest_security AS
(
    SELECT
        security_id,
        argMax(issuer_id, updated_at) AS issuer_id,
        argMax(country, updated_at) AS country
    FROM security_master
    GROUP BY security_id
),
latest_issuer AS
(
    SELECT
        issuer_id,
        argMax(sector_code, updated_at) AS sector_code
    FROM issuers
    GROUP BY issuer_id
),
universe AS
(
    SELECT s.security_id, i.sector_code
    FROM latest_security AS s
    INNER JOIN latest_issuer AS i ON i.issuer_id = s.issuer_id
    WHERE s.country = 'US'
      AND has({sector_codes:Array(String)}, i.sector_code)
),
factor_panel AS
(
    SELECT
        trade_date,
        security_id,
        argMaxIf(
            factor_value, updated_at,
            factor_id = {raw_factor:String} AND financial_basis = 'ttm'
        ) AS raw_pvgo,
        argMaxIf(
            factor_value, updated_at,
            factor_id = {adjusted_factor:String} AND financial_basis = 'ttm'
        ) AS adjusted_pvgo,
        argMaxIf(
            factor_value, updated_at,
            factor_id = {raw_earnings:String} AND financial_basis = 'ttm'
        ) AS raw_earnings,
        argMaxIf(
            factor_value, updated_at,
            factor_id = {adjusted_earnings:String} AND financial_basis = 'ttm'
        ) AS adjusted_earnings,
        argMaxIf(
            factor_value, updated_at,
            factor_id = 'mcap_mil' AND financial_basis = 'annual'
        ) AS market_cap_mil
    FROM fact_daily_factor_snapshot FINAL
    PREWHERE has({signal_dates:Array(Date)}, trade_date)
      AND has(
          {factor_ids:Array(String)},
          factor_id
      )
      AND startsWith(security_id, 'SEC_US_')
    WHERE source_trade_date <= trade_date
    GROUP BY trade_date, security_id
    HAVING countIf(
               factor_id = {raw_factor:String} AND financial_basis = 'ttm'
           ) > 0
       AND countIf(
               factor_id = {adjusted_factor:String} AND financial_basis = 'ttm'
           ) > 0
       AND countIf(
               factor_id = {raw_earnings:String} AND financial_basis = 'ttm'
           ) > 0
       AND countIf(
               factor_id = {adjusted_earnings:String} AND financial_basis = 'ttm'
           ) > 0
       AND countIf(factor_id = 'mcap_mil' AND financial_basis = 'annual') > 0
)
SELECT
    f.trade_date,
    f.security_id,
    u.sector_code,
    f.raw_pvgo,
    f.adjusted_pvgo,
    f.raw_earnings,
    f.adjusted_earnings,
    f.market_cap_mil
FROM factor_panel AS f
INNER JOIN universe AS u ON u.security_id = f.security_id
WHERE isFinite(f.raw_pvgo)
  AND isFinite(f.adjusted_pvgo)
  AND f.raw_earnings > 0
  AND f.adjusted_earnings > 0
  AND f.market_cap_mil > {minimum_market_cap:Float64}
ORDER BY f.trade_date, f.security_id
""".strip()


def _load_panel(client) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    signals = _signal_dates()
    trade_date_rows = client.query(
        """
SELECT DISTINCT trade_date
FROM price_daily
PREWHERE trade_date >= toDate('2017-03-31')
  AND trade_date <= {end_date:Date}
WHERE startsWith(security_id, 'SEC_US_')
ORDER BY trade_date
""".strip(),
        parameters={"end_date": END_DATE},
    ).result_rows
    trading_dates = pd.DatetimeIndex([row[0] for row in trade_date_rows]).sort_values()
    execution_dates: list[pd.Timestamp] = []
    for signal in signals:
        later = trading_dates[trading_dates > pd.Timestamp(signal)]
        if len(later):
            execution_dates.append(later[0])
        else:
            execution_dates.append(pd.NaT)
    schedule = pd.DataFrame(
        {"signal_date": pd.to_datetime(signals), "entry_date": execution_dates}
    )
    schedule["exit_date"] = schedule["entry_date"].shift(-1)
    schedule = schedule.dropna().copy()

    params = {
        "signal_dates": [value.date() for value in schedule["signal_date"]],
        "factor_ids": [
            RAW_LEVEL_FACTOR,
            ADJUSTED_LEVEL_FACTOR,
            RAW_EARNINGS_FACTOR,
            ADJUSTED_EARNINGS_FACTOR,
            "mcap_mil",
        ],
        "raw_factor": RAW_LEVEL_FACTOR,
        "adjusted_factor": ADJUSTED_LEVEL_FACTOR,
        "raw_earnings": RAW_EARNINGS_FACTOR,
        "adjusted_earnings": ADJUSTED_EARNINGS_FACTOR,
        "sector_codes": OPERATING_COMPANY_GICS_SECTORS,
        "minimum_market_cap": MIN_MARKET_CAP_USD_MILLIONS,
    }
    factor_rows = client.query(_factor_panel_query(), parameters=params)
    factors = pd.DataFrame(
        factor_rows.result_rows,
        columns=[
            "signal_date",
            "security_id",
            "sector_code",
            "raw_pvgo",
            "adjusted_pvgo",
            "raw_earnings",
            "adjusted_earnings",
            "market_cap_mil",
        ],
    )
    factors["signal_date"] = pd.to_datetime(factors["signal_date"])

    price_dates = sorted(
        set(schedule["entry_date"].dt.date) | set(schedule["exit_date"].dt.date)
    )
    price_result = client.query(
        """
SELECT security_id, trade_date, argMax(toFloat64(close), updated_at) AS close_price
FROM price_daily
PREWHERE has({price_dates:Array(Date)}, trade_date)
WHERE startsWith(security_id, 'SEC_US_')
  AND isFinite(toFloat64(close))
  AND toFloat64(close) > 0
GROUP BY security_id, trade_date
""".strip(),
        parameters={"price_dates": price_dates},
    )
    prices = pd.DataFrame(
        price_result.result_rows,
        columns=["security_id", "trade_date", "close_price"],
    )
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    wide_prices = prices.pivot(
        index="security_id", columns="trade_date", values="close_price"
    )

    panels = []
    coverage = []
    for row in schedule.itertuples(index=False):
        cross = factors.loc[factors["signal_date"] == row.signal_date].copy()
        if cross.empty or row.entry_date not in wide_prices or row.exit_date not in wide_prices:
            continue
        entry = wide_prices[row.entry_date]
        exit_ = wide_prices[row.exit_date]
        cross["entry_price"] = cross["security_id"].map(entry)
        cross["exit_price"] = cross["security_id"].map(exit_)
        cross["forward_return"] = cross["exit_price"] / cross["entry_price"] - 1.0
        eligible = len(cross)
        cross = cross.replace([np.inf, -np.inf], np.nan).dropna(
            subset=["raw_pvgo", "adjusted_pvgo", "forward_return", "market_cap_mil"]
        )
        coverage.append(
            {
                "signal_date": row.signal_date.date(),
                "entry_date": row.entry_date.date(),
                "exit_date": row.exit_date.date(),
                "common_eligible": eligible,
                "forward_return_available": len(cross),
            }
        )
        panels.append(cross)
    panel = pd.concat(panels, ignore_index=True)
    return panel, coverage


def _winsorized_z(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    lower, upper = numeric.quantile([0.01, 0.99])
    clipped = numeric.clip(lower=lower, upper=upper)
    standard_deviation = clipped.std(ddof=0)
    if not np.isfinite(standard_deviation) or standard_deviation <= 0:
        return pd.Series(np.nan, index=values.index)
    return (clipped - clipped.mean()) / standard_deviation


def _nw_coefficient_summary(values: pd.Series, max_lags: int = 4) -> dict[str, Any]:
    result = newey_west_mean_test(values, max_lags=max_lags)
    if result.get("status") != "ok":
        return result
    return {
        "status": "ok",
        "periods": result["observations"],
        "mean_coefficient": result["mean_daily_return"],
        "hac_standard_error": result["hac_standard_error_daily_mean"],
        "t_stat": result["t_stat"],
        "two_sided_normal_p_value": result["two_sided_normal_p_value"],
        "max_lags": result["max_lags"],
        "note": "Newey-West/Bartlett HAC over quarterly cross-sectional estimates",
    }


def _information_coefficients(panel: pd.DataFrame) -> dict[str, Any]:
    rows = []
    for signal_date, cross in panel.groupby("signal_date", sort=True):
        rows.append(
            {
                "signal_date": signal_date,
                "raw_low_pvgo_ic": (-cross["raw_pvgo"]).corr(
                    cross["forward_return"], method="spearman"
                ),
                "adjusted_low_pvgo_ic": (-cross["adjusted_pvgo"]).corr(
                    cross["forward_return"], method="spearman"
                ),
                "raw_adjusted_spearman": cross["raw_pvgo"].corr(
                    cross["adjusted_pvgo"], method="spearman"
                ),
            }
        )
    frame = pd.DataFrame(rows).set_index("signal_date")
    frame["adjusted_minus_raw_ic"] = (
        frame["adjusted_low_pvgo_ic"] - frame["raw_low_pvgo_ic"]
    )
    return {
        "raw_low_pvgo": _nw_coefficient_summary(frame["raw_low_pvgo_ic"]),
        "adjusted_low_pvgo": _nw_coefficient_summary(
            frame["adjusted_low_pvgo_ic"]
        ),
        "adjusted_minus_raw": _nw_coefficient_summary(
            frame["adjusted_minus_raw_ic"]
        ),
        "mean_raw_adjusted_spearman": float(frame["raw_adjusted_spearman"].mean()),
        "period_values": frame.reset_index().to_dict(orient="records"),
    }


def _fama_macbeth(
    panel: pd.DataFrame,
    predictors: list[str],
    *,
    controls: bool,
) -> dict[str, Any]:
    estimates = []
    for signal_date, source in panel.groupby("signal_date", sort=True):
        cross = source.copy()
        for predictor in predictors:
            cross[f"{predictor}_z"] = _winsorized_z(cross[predictor])
        cross["log_market_cap_z"] = _winsorized_z(np.log(cross["market_cap_mil"]))
        columns = [f"{predictor}_z" for predictor in predictors]
        design = cross[columns].copy()
        if controls:
            design["log_market_cap_z"] = cross["log_market_cap_z"]
            sectors = pd.get_dummies(
                cross["sector_code"].astype(str), prefix="sector", drop_first=True
            ).astype(float)
            design = pd.concat([design, sectors], axis=1)
        design.insert(0, "intercept", 1.0)
        valid = design.notna().all(axis=1) & cross["forward_return"].notna()
        x = design.loc[valid].to_numpy(dtype=float)
        y = cross.loc[valid, "forward_return"].to_numpy(dtype=float)
        if len(y) <= x.shape[1] + 5:
            continue
        coefficients, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
        if rank < x.shape[1]:
            continue
        row = {"signal_date": signal_date, "observations": len(y)}
        row.update(dict(zip(design.columns, coefficients, strict=True)))
        estimates.append(row)
    frame = pd.DataFrame(estimates).set_index("signal_date")
    coefficient_names = ["intercept", *[f"{value}_z" for value in predictors]]
    if controls:
        coefficient_names.append("log_market_cap_z")
    return {
        "periods": len(frame),
        "mean_cross_section_size": float(frame["observations"].mean()),
        "coefficients": {
            name: _nw_coefficient_summary(frame[name]) for name in coefficient_names
        },
        "period_values": frame.reset_index()[
            ["signal_date", "observations", *coefficient_names]
        ].to_dict(orient="records"),
        "predictor_units": "one within-date winsorized cross-sectional standard deviation",
        "controls": "log market cap and sector fixed effects" if controls else "none",
    }


def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    client = get_clickhouse_client()
    try:
        panel, coverage = _load_panel(client)
    finally:
        client.close()
    panel["raw_pvgo"] = pd.to_numeric(panel["raw_pvgo"], errors="coerce")
    panel["adjusted_pvgo"] = pd.to_numeric(
        panel["adjusted_pvgo"], errors="coerce"
    )
    panel["forward_return"] = pd.to_numeric(
        panel["forward_return"], errors="coerce"
    )
    result = {
        "design": {
            "raw_factor": RAW_LEVEL_FACTOR,
            "adjusted_factor": ADJUSTED_LEVEL_FACTOR,
            "common_sample": (
                "both PVGO levels present; raw and adjusted normalized earnings positive; "
                "market cap above USD 1bn; Financials and Real Estate excluded"
            ),
            "signal_lag": "entry is first trading close strictly after signal date",
            "horizon": "entry close to the next quarterly entry close",
            "point_in_time": (
                "fact_daily_factor_snapshot FINAL with source_trade_date <= signal_date"
            ),
        },
        "coverage": coverage,
        "panel_rows": len(panel),
        "signal_periods": int(panel["signal_date"].nunique()),
        "information_coefficients": _information_coefficients(panel),
        "fama_macbeth": {
            "raw_univariate": _fama_macbeth(panel, ["raw_pvgo"], controls=False),
            "adjusted_univariate": _fama_macbeth(
                panel, ["adjusted_pvgo"], controls=False
            ),
            "joint": _fama_macbeth(
                panel, ["raw_pvgo", "adjusted_pvgo"], controls=False
            ),
            "joint_size_sector_controlled": _fama_macbeth(
                panel, ["raw_pvgo", "adjusted_pvgo"], controls=True
            ),
        },
        "limitations": [
            "Sector classifications are the latest security-master mapping, matching current FactorLab universe compilation rather than historical sector snapshots.",
            "Securities without a price at the next rebalance close are omitted; explicit delisting returns are unavailable.",
            "The 2017-2026 quarterly sample is short, so HAC inference is reported but cannot establish structural universality.",
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
