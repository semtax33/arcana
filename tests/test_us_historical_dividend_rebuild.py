from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.rebuild_us_historical_dividends import (
    build_historical_dividend_rows,
    merge_exact_scope,
)


def test_build_historical_dividends_uses_yfinance_payment_events(tmp_path: Path) -> None:
    pd.DataFrame(
        {
            "Date": ["2015-12-31", "2016-02-04", "2016-05-05", "2017-02-09"],
            "Close": [25, 24, 23, 30],
            "Dividends": [0, 0.13, 0.14, 0.15],
        }
    ).to_csv(tmp_path / "AAPL.csv", index=False)

    rows = build_historical_dividend_rows(
        ["AAPL"],
        price_dir=tmp_path,
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert rows[["security_id", "trade_date", "dividend"]].to_dict("records") == [
        {
            "security_id": "SEC_US_AAPL",
            "trade_date": pd.Timestamp("2016-02-04"),
            "dividend": 0.13,
        },
        {
            "security_id": "SEC_US_AAPL",
            "trade_date": pd.Timestamp("2016-05-05"),
            "dividend": 0.14,
        },
    ]


def test_build_historical_dividends_accepts_legacy_index_date_column(
    tmp_path: Path,
) -> None:
    pd.DataFrame(
        {
            "index": ["2008-02-07", "2008-05-08"],
            "Close": [20.0, 21.0],
            "Dividends": [0.05, 0.0],
        }
    ).to_csv(tmp_path / "FDP.csv", index=False)

    rows = build_historical_dividend_rows(
        ["FDP"],
        price_dir=tmp_path,
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert rows[["security_id", "trade_date", "dividend"]].to_dict("records") == [
        {
            "security_id": "SEC_US_FDP",
            "trade_date": pd.Timestamp("2008-02-07"),
            "dividend": 0.05,
        }
    ]


def test_merge_historical_dividends_replaces_only_target_date_scope() -> None:
    existing = pd.DataFrame(
        {
            "security_id": ["SEC_US_AAPL", "SEC_US_AAPL", "SEC_US_MSFT"],
            "trade_date": pd.to_datetime(["2016-02-04", "2020-02-06", "2016-02-17"]),
            "dividend": [9.99, 0.77, 0.36],
        }
    )
    rebuilt = pd.DataFrame(
        {
            "security_id": ["SEC_US_AAPL"],
            "trade_date": pd.to_datetime(["2016-02-04"]),
            "dividend": [0.13],
        }
    )

    merged = merge_exact_scope(
        existing,
        rebuilt,
        security_ids={"SEC_US_AAPL"},
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert merged[["security_id", "trade_date", "dividend"]].to_dict("records") == [
        {
            "security_id": "SEC_US_AAPL",
            "trade_date": pd.Timestamp("2016-02-04"),
            "dividend": 0.13,
        },
        {
            "security_id": "SEC_US_MSFT",
            "trade_date": pd.Timestamp("2016-02-17"),
            "dividend": 0.36,
        },
        {
            "security_id": "SEC_US_AAPL",
            "trade_date": pd.Timestamp("2020-02-06"),
            "dividend": 0.77,
        },
    ]
