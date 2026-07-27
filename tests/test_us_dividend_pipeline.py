from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal import us_dividends as us_dividend_extractor
from engine.extractors._internal.us_dividends import download_us_dividends
from engine.transformers._internal.us_dividends import (
    build_us_dividend_events_dataframe,
    create_us_stock_dividend_dataframe,
    normalize_us_dividends,
)
from engine.workflows._internal import download_workflow
from engine.workflows._internal import normalize_workflow


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TestUSDividendPipeline(unittest.TestCase):
    def test_alpha_collector_uses_environment_key_and_yfinance_only_for_uncovered_ticker(self):
        class FakeTicker:
            dividends = pd.Series(
                [0.40],
                index=pd.to_datetime(["2025-04-11"]),
            )

        calls = []
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "runtime-secret"}):
            root = Path(temp) / "dividend"

            def fake_get(url, *, params, timeout):
                calls.append((url, params.copy(), timeout))
                data = (
                    [{"ex_dividend_date": "2025-02-07", "amount": "0.25"}]
                    if params["symbol"] == "AAPL"
                    else []
                )
                return _Response({"symbol": params["symbol"], "data": data})

            counts = download_us_dividends(
                symbols=["AAPL", "MSFT"],
                sources=["alpha-vantage", "yfinance"],
                snapshot_date="2026-07-27",
                output_root=root,
                http_get=fake_get,
                yfinance_ticker_factory=lambda symbol: FakeTicker(),
                sleeper=lambda _: None,
            )

            alpha_path = root / "alpha-vantage" / "snapshot_date=2026-07-27" / "ticker=AAPL.json"
            yfinance_aapl_path = root / "yfinance" / "snapshot_date=2026-07-27" / "ticker=AAPL.json"
            yfinance_msft_path = root / "yfinance" / "snapshot_date=2026-07-27" / "ticker=MSFT.json"

            self.assertEqual(counts["written"], 3)
            self.assertEqual(counts["failed"], 0)
            self.assertEqual({call[1]["function"] for call in calls}, {"DIVIDENDS"})
            self.assertTrue(all(call[1]["apikey"] == "runtime-secret" for call in calls))
            self.assertTrue(alpha_path.exists())
            self.assertFalse(yfinance_aapl_path.exists())
            self.assertTrue(yfinance_msft_path.exists())

    def test_alpha_collector_requires_environment_key(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "ALPHA_VANTAGE_API_KEY"):
                download_us_dividends(
                    symbols=["AAPL"],
                    sources=["alpha-vantage"],
                    output_root=Path(temp) / "dividend",
                )

    def test_implicit_symbol_universe_is_refreshed_before_us_download(self):
        with (
            TemporaryDirectory() as temp,
            patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "runtime-secret"}),
            patch.object(us_dividend_extractor, "download_us_equity_universe") as refresh_universe,
            patch.object(us_dividend_extractor, "_resolve_symbols", return_value=["AAPL"]),
        ):
            counts = download_us_dividends(
                sources=["alpha-vantage"],
                output_root=Path(temp) / "dividend",
                http_get=lambda *args, **kwargs: _Response({"data": []}),
                sleeper=lambda _: None,
            )

        refresh_universe.assert_called_once_with()
        self.assertEqual(counts["symbols"], 1)

    def test_edgartools_stage_is_a_non_network_placeholder(self):
        with TemporaryDirectory() as temp:
            counts = download_us_dividends(
                symbols=["AAPL"],
                sources=["edgartools"],
                output_root=Path(temp) / "dividend",
            )

        self.assertEqual(counts["not_implemented"], 1)
        self.assertEqual(counts["written"], 0)

    def test_silver_events_keep_all_alpha_dates_and_preserve_yfinance_partial_event(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            bronze = root / "bronze"
            _write_json(
                bronze / "alpha-vantage" / "snapshot_date=2026-07-27" / "ticker=AAPL.json",
                {
                    "symbol": "AAPL",
                    "data": [
                        {
                            "ex_dividend_date": "2025-02-07",
                            "declaration_date": "2025-01-30",
                            "record_date": "2025-02-10",
                            "payment_date": "2025-02-13",
                            "amount": "0.25",
                        }
                    ],
                },
            )
            _write_json(
                bronze / "yfinance" / "snapshot_date=2026-07-27" / "ticker=AAPL.json",
                {"symbol": "AAPL", "data": [{"ex_dividend_date": "2025-02-07", "amount": 9.99}]},
            )
            _write_json(
                bronze / "yfinance" / "snapshot_date=2026-07-27" / "ticker=MSFT.json",
                {"symbol": "MSFT", "data": [{"ex_dividend_date": "2025-04-11", "amount": 0.40}]},
            )
            ticker_map = root / "sec_company_tickers.csv"
            ticker_map.write_text(
                "cik,ticker,title\n320193,AAPL,Apple Inc.\n789019,MSFT,Microsoft Corp.\n",
                encoding="utf-8",
            )
            financial_dir = root / "financials"
            financial_dir.mkdir()
            (financial_dir / "us_normalized_AAPL.csv").write_text(
                "canonical_account_id,normalized_amount,fiscal_year,fiscal_month\n"
                "DILUTED_EPS,2.0,2025,12\nDIV_PAID,25,2025,12\nNET_INCOME,100,2025,12\n",
                encoding="utf-8",
            )

            events = build_us_dividend_events_dataframe(
                bronze_root=bronze,
                ticker_map_path=ticker_map,
                financial_dir=financial_dir,
            )
            events_path = root / "us_dividend_events.csv"
            daily_path = root / "us_dividend_normalized.csv"
            result = normalize_us_dividends(
                bronze_root=bronze,
                events_path=events_path,
                daily_path=daily_path,
                ticker_map_path=ticker_map,
                financial_dir=financial_dir,
            )
            daily = create_us_stock_dividend_dataframe(events_path=events_path)

        self.assertEqual(events["ticker"].tolist(), ["AAPL", "MSFT"])
        aapl = events.loc[events["ticker"] == "AAPL"].iloc[0]
        msft = events.loc[events["ticker"] == "MSFT"].iloc[0]
        self.assertEqual(aapl["source"], "ALPHA_VANTAGE")
        self.assertEqual(aapl["dividend_declared_date"], "2025-01-30")
        self.assertEqual(aapl["dividend_ex_date"], "2025-02-07")
        self.assertEqual(aapl["dividend_record_date"], "2025-02-10")
        self.assertEqual(aapl["dividend_payment_date"], "2025-02-13")
        self.assertAlmostEqual(aapl["payout_ratio_dps_over_eps"], 0.125)
        self.assertEqual(msft["source"], "YFINANCE")
        self.assertEqual(msft["dividend_ex_date"], "2025-04-11")
        self.assertTrue(pd.isna(msft["dividend_payment_date"]) or msft["dividend_payment_date"] == "")
        self.assertEqual(daily["security_id"].tolist(), ["SEC_US_AAPL"])
        self.assertEqual(daily["trade_date"].dt.strftime("%Y-%m-%d").tolist(), ["2025-02-13"])
        self.assertEqual(result["events"], 2)
        self.assertEqual(result["daily_rows"], 1)

    def test_download_workflow_keeps_kr_default_and_routes_us_dividend(self):
        kr_calls = []
        us_calls = []
        with (
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch("engine.extractors.filings.download_dividend_histories", side_effect=lambda *args, **kwargs: kr_calls.append((args, kwargs))),
        ):
            with patch.object(sys, "argv", ["prog", "dividend"]):
                download_workflow.main()

        with (
            patch.object(download_workflow, "download_all_us_dividend", side_effect=lambda args: us_calls.append(args)),
            patch.dict(download_workflow.US_DOWNLOAD_ACTIONS, {"dividend": lambda args: us_calls.append(args)}),
            patch.object(sys, "argv", ["prog", "--market", "us", "--symbols", "AAPL", "dividend"]),
        ):
            download_workflow.main()

        self.assertEqual(len(kr_calls), 1)
        self.assertEqual(len(us_calls), 1)
        self.assertEqual(us_calls[0].symbols, "AAPL")

    def test_normalize_workflow_routes_us_dividend_target(self):
        with (
            patch.object(normalize_workflow, "normalize_us_dividend_history") as normalize_dividend,
            patch.object(sys, "argv", ["prog", "--market", "us", "--target", "dividend"]),
        ):
            normalize_workflow.main()

        normalize_dividend.assert_called_once_with()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
