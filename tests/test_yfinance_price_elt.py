from __future__ import annotations

import ssl
import sys
import unittest
from urllib.error import URLError
from unittest.mock import patch

import pandas as pd

from engine.extractors.market_prices import filter_us_equity_universe
from engine.extractors._internal import yfinance_market_prices
from engine.loaders.market_data import PRICE_TABLE, insert_price
from engine.transformers.market_data import normalize_yfinance_price_frame
from engine.workflows._internal import download_workflow


class FakeClickHouseClient:
    def __init__(self):
        self.inserted = []
        self.closed = False

    def insert_df(self, table, df, column_names=None):
        self.inserted.append((table, df.copy(), list(column_names or [])))

    def close(self):
        self.closed = True


class FakeUrlResponse:
    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.text.encode("utf-8")


class YFinancePriceEltTest(unittest.TestCase):
    def test_download_text_retries_ssl_failures_with_certifi_context(self):
        cert_error = URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
        with (
            patch.object(
                yfinance_market_prices,
                "urlopen",
                side_effect=[cert_error, FakeUrlResponse("Symbol|Security Name\nAAPL|Apple Inc.\n")],
            ) as open_mock,
            patch.object(yfinance_market_prices, "_certifi_ssl_context", return_value="certifi-context"),
        ):
            text = yfinance_market_prices._download_text("https://example.test/symbols.txt")

        self.assertIn("AAPL", text)
        self.assertEqual(open_mock.call_count, 2)
        self.assertEqual(open_mock.call_args.kwargs["context"], "certifi-context")

    def test_universe_filter_keeps_common_preferred_and_adr(self):
        nasdaq = pd.DataFrame(
            [
                {
                    "Symbol": "AAPL",
                    "Security Name": "Apple Inc. Common Stock",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "Symbol": "XYZ",
                    "Security Name": "XYZ Corp Class A Common Stock",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "Symbol": "ETF1",
                    "Security Name": "ETF One Common Stock",
                    "ETF": "Y",
                    "Test Issue": "N",
                },
                {
                    "Symbol": "TEST",
                    "Security Name": "Test Co Common Stock",
                    "ETF": "N",
                    "Test Issue": "Y",
                },
                {
                    "Symbol": "WT",
                    "Security Name": "Warrant Holdings Warrants",
                    "ETF": "N",
                    "Test Issue": "N",
                },
            ]
        )
        other = pd.DataFrame(
            [
                {
                    "ACT Symbol": "TSM",
                    "Security Name": "Taiwan Semiconductor American Depositary Shares",
                    "Exchange": "N",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "ACT Symbol": "BAC^B",
                    "Security Name": "Bank of America Depositary Shares Preferred Stock",
                    "Exchange": "N",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "ACT Symbol": "UNIT",
                    "Security Name": "Acquisition Corp Units",
                    "Exchange": "A",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "ACT Symbol": "FUND",
                    "Security Name": "Income Opportunity Fund",
                    "Exchange": "P",
                    "ETF": "N",
                    "Test Issue": "N",
                },
            ]
        )

        result = filter_us_equity_universe(nasdaq, other)

        self.assertEqual(result["ticker"].tolist(), ["AAPL", "BAC-B", "TSM"])

    def test_normalizer_maps_yfinance_csv_shape(self):
        raw = pd.DataFrame(
            {
                "Date": ["2026-01-02", "bad-date", "2026-01-03"],
                "Open": [100, 101, 102],
                "High": [110, 111, 112],
                "Low": [90, 91, 92],
                "Close": [105, 106, None],
                "Adj Close": [104.5, 105.5, 101.5],
                "Volume": [1000, 1001, 1002],
            }
        )

        result = normalize_yfinance_price_frame(raw, ticker="AAPL")

        self.assertEqual(result["security_id"].tolist(), ["SEC_US_AAPL"])
        self.assertEqual(result["currency"].tolist(), ["USD"])
        self.assertEqual(result["close"].tolist(), [105.0])
        self.assertEqual(result["adj_close"].tolist(), [104.5])
        self.assertEqual(result["volume"].tolist(), [1000])

    def test_insert_us_prices_inserts_monthly_partitions(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL", "SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2026-01-31", "2026-02-02"]),
                "open": [100, 110],
                "high": [101, 111],
                "low": [99, 109],
                "close": [100.5, 110.5],
                "volume": [1000, 1100],
                "adj_close": [100.0, 110.0],
                "currency": ["USD", "USD"],
            }
        )
        client = FakeClickHouseClient()

        with patch("engine.loaders.market_data.create_price_dataframe", return_value=price_df):
            result = insert_price(market="us", client=client)

        self.assertEqual(result.attrs["inserted_rows"], 2)
        self.assertEqual([item[0] for item in client.inserted], [PRICE_TABLE, PRICE_TABLE])
        self.assertFalse(client.closed)
        for _, inserted_df, columns in client.inserted:
            self.assertNotIn("_partition", inserted_df.columns)
            self.assertEqual(columns, list(price_df.columns))

    def test_download_workflow_routes_us_prices_and_preserves_kr_prices(self):
        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--symbols", "AAPL,MSFT", "--limit", "2", "prices"]),
            patch.object(download_workflow, "download_us_price_histories") as download_us,
        ):
            download_workflow.main()

        download_us.assert_called_once()
        self.assertEqual(download_us.call_args.kwargs["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(download_us.call_args.kwargs["limit"], 2)

        with (
            patch.object(sys, "argv", ["prog", "prices"]),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch.object(download_workflow, "fetch_all_prices") as fetch_kr,
        ):
            download_workflow.main()

        fetch_kr.assert_called_once()
        self.assertEqual(fetch_kr.call_args.args[0], ["005930"])


if __name__ == "__main__":
    unittest.main()
