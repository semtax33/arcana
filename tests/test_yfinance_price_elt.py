from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from urllib.error import URLError
from unittest.mock import patch

import pandas as pd

from engine.extractors.market_prices import filter_us_equity_universe
from engine.extractors._internal import yfinance_market_prices
from engine.loaders import market_data
from engine.loaders import securities as securities_loader
from engine.loaders.market_data import PRICE_TABLE, insert_price
from engine.transformers.market_data import (
    normalize_us_price,
    normalize_us_shares,
    normalize_yfinance_price_frame,
    normalize_yfinance_shares_frame,
)
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
    def test_us_price_download_appends_new_dates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            output_path = output_dir / "AAPL.csv"
            pd.DataFrame(
                {
                    "Date": ["2026-07-22", "2026-07-23"],
                    "Open": [100, 101],
                    "High": [101, 102],
                    "Low": [99, 100],
                    "Close": [100.5, 101.5],
                    "Volume": [1000, 1100],
                }
            ).to_csv(output_path, index=False)
            incremental = pd.DataFrame(
                {
                    "Date": ["2026-07-24"],
                    "Open": [102],
                    "High": [103],
                    "Low": [101],
                    "Close": [102.5],
                    "Volume": [1200],
                }
            )

            with patch.object(
                yfinance_market_prices,
                "fetch_yfinance_price",
                return_value=incremental,
            ) as fetch:
                written = yfinance_market_prices.download_us_price_histories(
                    symbols=["AAPL"],
                    output_dir=output_dir,
                    end_date="2026-07-24",
                )

            result = pd.read_csv(output_path)

        self.assertEqual(written, [output_path])
        self.assertEqual(
            result["Date"].tolist(),
            ["2026-07-22", "2026-07-23", "2026-07-24"],
        )
        self.assertEqual(fetch.call_args.kwargs["start_date"], "2026-07-24")

    def test_us_price_download_retries_then_continues_after_a_ticker_failure(self):
        frame = pd.DataFrame(
            {
                "Date": ["2026-07-24"],
                "Open": [102],
                "High": [103],
                "Low": [101],
                "Close": [102.5],
                "Volume": [1200],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            stdout = io.StringIO()
            with (
                patch.object(
                    yfinance_market_prices,
                    "fetch_yfinance_price",
                    side_effect=[TimeoutError("timed out"), pd.DataFrame(), frame],
                ) as fetch,
                patch.object(yfinance_market_prices, "sleep") as sleep_mock,
                redirect_stdout(stdout),
            ):
                written = yfinance_market_prices.download_us_price_histories(
                    symbols=["COMP", "CON"],
                    output_dir=output_dir,
                    retries=1,
                    retry_backoff_seconds=0.5,
                )

            self.assertEqual([path.name for path in written], ["__ARCANA_WIN_RESERVED__CON.csv"])

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list[0].args, (0.5,))
        output = stdout.getvalue()
        self.assertIn("ticker=COMP attempts=2 reason=empty result", output)
        self.assertIn("downloaded CON rows=1", output)

    def test_fetch_yfinance_price_uses_bounded_single_threaded_requests(self):
        class FakeYFinance:
            def __init__(self):
                self.kwargs = None

            def download(self, ticker, **kwargs):
                self.kwargs = kwargs
                return pd.DataFrame(
                    {"Close": [100]},
                    index=pd.DatetimeIndex(["2026-07-24"], name="Date"),
                )

        fake_yf = FakeYFinance()
        with patch.object(yfinance_market_prices, "_import_yfinance", return_value=fake_yf):
            result = yfinance_market_prices.fetch_yfinance_price("COMP", timeout=12.5)

        self.assertEqual(len(result), 1)
        self.assertEqual(fake_yf.kwargs["timeout"], 12.5)
        self.assertFalse(fake_yf.kwargs["threads"])
        self.assertFalse(fake_yf.kwargs["repair"])

    def test_flatten_yfinance_columns_preserves_repaired_marker(self):
        raw = pd.DataFrame(
            [[100, False]],
            columns=pd.MultiIndex.from_tuples(
                [("Close", "COMP"), ("Repaired?", "COMP")]
            ),
        )

        result = yfinance_market_prices._flatten_yfinance_columns(raw)

        self.assertEqual(result.columns.tolist(), ["Close", "Repaired?"])

    def test_yfinance_price_storage_stem_avoids_windows_reserved_ticker_names(self):
        stored = yfinance_market_prices.yfinance_price_storage_stem("CON")

        self.assertEqual(stored, "__ARCANA_WIN_RESERVED__CON")
        self.assertEqual(
            yfinance_market_prices.yfinance_price_ticker_from_storage_stem(stored),
            "CON",
        )
        self.assertEqual(yfinance_market_prices.yfinance_price_storage_stem("COMP"), "COMP")

    def test_price_normalizer_restores_ticker_from_windows_safe_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            safe_path = Path(temp_dir) / "__ARCANA_WIN_RESERVED__CON.csv"
            pd.DataFrame(
                {
                    "Date": ["2026-07-24"],
                    "Open": [100],
                    "High": [101],
                    "Low": [99],
                    "Close": [100.5],
                    "Adj Close": [100],
                    "Volume": [1000],
                }
            ).to_csv(safe_path, index=False)

            result = normalize_us_price(
                Path(temp_dir) / "*.csv",
                output_path=None,
                log_progress=False,
            )

        self.assertEqual(result["security_id"].tolist(), ["SEC_US_CON"])

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

    def test_universe_filter_keeps_common_adr_and_class_stock_but_excludes_preferred(self):
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
                    "Symbol": "GOOGL",
                    "Security Name": "Alphabet Inc. Class A Common Stock",
                    "ETF": "N",
                    "Test Issue": "N",
                },
                {
                    "Symbol": "GOOG",
                    "Security Name": "Alphabet Inc. Class C Capital Stock",
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

        self.assertEqual(result["ticker"].tolist(), ["AAPL", "GOOG", "TSM", "XYZ"])

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

    def test_normalizer_drops_dates_outside_clickhouse_date_range(self):
        raw = pd.DataFrame(
            {
                "Date": ["1969-12-31", "1970-01-01", "2149-06-06", "2149-06-07"],
                "Open": [100, 101, 102, 103],
                "High": [110, 111, 112, 113],
                "Low": [90, 91, 92, 93],
                "Close": [105, 106, 107, 108],
                "Adj Close": [104.5, 105.5, 106.5, 107.5],
                "Volume": [1000, 1001, 1002, 1003],
            }
        )

        result = normalize_yfinance_price_frame(raw, ticker="AAPL")

        self.assertEqual(
            result["trade_date"].dt.strftime("%Y-%m-%d").tolist(),
            ["1970-01-01", "2149-06-06"],
        )

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

    def test_insert_us_prices_skips_dates_outside_clickhouse_date_range(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL", "SEC_US_AAPL", "SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["1969-12-31", "2026-01-31", "2149-06-07"]),
                "open": [90, 100, 110],
                "high": [91, 101, 111],
                "low": [89, 99, 109],
                "close": [90.5, 100.5, 110.5],
                "volume": [900, 1000, 1100],
                "adj_close": [90.0, 100.0, 110.0],
                "currency": ["USD", "USD", "USD"],
            }
        )
        client = FakeClickHouseClient()

        with patch("engine.loaders.market_data.create_price_dataframe", return_value=price_df):
            result = insert_price(market="us", client=client)

        self.assertEqual(result.attrs["inserted_rows"], 1)
        self.assertEqual(len(client.inserted), 1)
        self.assertEqual(
            client.inserted[0][1]["trade_date"].tolist(),
            [pd.Timestamp("2026-01-31").date()],
        )

    def test_us_market_data_cli_loads_securities_before_prices(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2026-01-31"]),
                "open": [100],
                "high": [101],
                "low": [99],
                "close": [100.5],
                "volume": [1000],
                "adj_close": [100.0],
                "currency": ["USD"],
            }
        )

        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--target", "prices", "--dry-run"]),
            patch.object(
                market_data,
                "insert_securities",
                return_value={"issuers": 1, "security-master": 1, "identifiers": 2},
            ) as insert_securities_mock,
            patch.object(market_data, "insert_price", return_value=price_df) as insert_price_mock,
        ):
            market_data.main()

        insert_securities_mock.assert_called_once_with(market="us", target="all", dry_run=True)
        insert_price_mock.assert_called_once()

    def test_us_securities_cli_uses_same_loader_as_kr_entrypoint(self):
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--dry-run"]),
            patch.object(
                securities_loader,
                "insert_securities",
                return_value={"issuers": 1, "security-master": 1, "identifiers": 2},
            ) as insert_securities_mock,
            redirect_stdout(stdout),
        ):
            securities_loader.main()

        insert_securities_mock.assert_called_once_with(market="us", target="all", dry_run=True)
        self.assertIn("prepared issuers market=us rows=1", stdout.getvalue())

    def test_us_market_data_securities_target_is_removed(self):
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--target", "securities", "--dry-run"]),
            patch.object(market_data, "insert_securities") as insert_securities_mock,
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                market_data.main()

        insert_securities_mock.assert_not_called()
        self.assertIn("invalid choice: 'securities'", stderr.getvalue())

    def test_us_market_data_cli_all_prepares_shares(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2026-01-31"]),
                "open": [100],
                "high": [101],
                "low": [99],
                "close": [100.5],
                "volume": [1000],
                "adj_close": [100.0],
                "currency": ["USD"],
            }
        )

        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--target", "all", "--dry-run"]),
            patch.object(
                market_data,
                "insert_securities",
                return_value={"issuers": 1, "security-master": 1, "identifiers": 2},
            ),
            patch.object(market_data, "insert_price", return_value=price_df) as insert_price_mock,
            patch.object(market_data, "insert_shares", return_value=pd.DataFrame()) as insert_shares_mock,
        ):
            market_data.main()

        insert_price_mock.assert_called_once()
        insert_shares_mock.assert_called_once()

    def test_normalize_yfinance_shares_frame_uses_explicit_shares_and_market_cap(self):
        frame = pd.DataFrame(
            {
                "Date": ["2026-01-02"],
                "Close": [100],
                "Shares": [10],
                "Market Cap": [1_000],
            }
        )

        result = normalize_yfinance_shares_frame(frame, ticker="AAPL")

        self.assertEqual(result["security_id"].iat[0], "SEC_US_AAPL")
        self.assertEqual(result["shares"].iat[0], 10)
        self.assertEqual(result["market_cap"].iat[0], 1_000)

    def test_normalize_us_shares_falls_back_to_sec_weighted_average_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            financial_dir = root / "financials"
            financial_dir.mkdir()
            (financial_dir / "us_normalized_AAPL.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,normalized_amount,fiscal_year,fiscal_month",
                        "BASIC_SHARES,90,2025,12",
                        "DILUTED_SHARES,100,2025,12",
                        "COMMON_SHARES_OUTSTANDING,110,2025,12",
                    ]
                ),
                encoding="utf-8",
            )

            result = normalize_us_shares(
                root / "missing-price-*.csv",
                financial_dir=financial_dir,
                output_path=None,
                log_progress=False,
            )

        self.assertEqual(result["security_id"].iat[0], "SEC_US_AAPL")
        self.assertEqual(result["trade_date"].dt.strftime("%Y-%m-%d").iat[0], "2025-12-31")
        self.assertEqual(result["shares"].iat[0], 110)
        self.assertTrue(pd.isna(result["market_cap"].iat[0]))

    def test_normalize_us_price_logs_file_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for ticker in ["AAPL", "MSFT"]:
                pd.DataFrame(
                    {
                        "Date": ["2026-01-02"],
                        "Open": [100],
                        "High": [101],
                        "Low": [99],
                        "Close": [100.5],
                        "Adj Close": [100.0],
                        "Volume": [1000],
                    }
                ).to_csv(tmp_path / f"{ticker}.csv", index=False)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = normalize_us_price(
                    tmp_path / "*.csv",
                    output_path=None,
                    progress_interval=1,
                )

        output = stdout.getvalue()
        self.assertEqual(len(result), 2)
        self.assertIn("normalizing US price files count=2", output)
        self.assertIn("normalizing price file ticker=AAPL (1/2)", output)
        self.assertIn("normalizing price file ticker=MSFT (2/2)", output)
        self.assertIn("normalized US price rows=2", output)

    def test_download_workflow_routes_us_prices_and_preserves_kr_prices(self):
        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "--symbols", "AAPL,MSFT", "--limit", "2", "prices"]),
            patch.object(download_workflow, "download_us_price_histories") as download_us,
        ):
            download_workflow.main()

        download_us.assert_called_once()
        self.assertEqual(download_us.call_args.kwargs["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(download_us.call_args.kwargs["limit"], 2)
        self.assertIsNone(download_us.call_args.kwargs["start_date"])
        self.assertIsNone(download_us.call_args.kwargs["end_date"])
        self.assertEqual(download_us.call_args.kwargs["request_timeout"], 15.0)
        self.assertEqual(download_us.call_args.kwargs["retries"], 2)
        self.assertEqual(download_us.call_args.kwargs["retry_backoff_seconds"], 2.0)
        self.assertFalse(download_us.call_args.kwargs["repair"])

        with (
            patch.object(sys, "argv", ["prog", "--start-date", "2024-01-01", "--end-date", "20240131", "prices"]),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch.object(download_workflow, "fetch_all_prices") as fetch_kr,
        ):
            download_workflow.main()

        fetch_kr.assert_called_once()
        self.assertEqual(fetch_kr.call_args.args[0], ["005930"])
        self.assertEqual(fetch_kr.call_args.args[2], "20240101")
        self.assertEqual(fetch_kr.call_args.args[3], "20240131")

    def test_download_workflow_routes_date_range_to_dart_downloads(self):
        with (
            patch.object(sys, "argv", ["prog", "--start-date", "2024-01-01", "--end-date", "2024-03-31", "statements"]),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch("engine.extractors.filings.download_statements") as download_statements,
        ):
            download_workflow.main()

        download_statements.assert_called_once_with(
            ["005930"],
            0,
            start_date="20240101",
            end_date="20240331",
        )

    def test_download_workflow_expands_year_range_to_dart_dates(self):
        with (
            patch.object(sys, "argv", ["prog", "--start-year", "2000", "--end-year", "2025", "statements"]),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch("engine.extractors.filings.download_statements") as download_statements,
        ):
            download_workflow.main()

        download_statements.assert_called_once_with(
            ["005930"],
            0,
            start_date="20000101",
            end_date="20251231",
        )

    def test_download_workflow_routes_date_range_to_dart_business_info(self):
        with (
            patch.object(
                sys,
                "argv",
                ["prog", "--start-date", "2024-01-01", "--end-date", "2024-12-31", "business-info"],
            ),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch("engine.extractors.filings.download_business_infos") as download_business_infos,
        ):
            download_workflow.main()

        download_business_infos.assert_called_once_with(
            ["005930"],
            0,
            start_date="20240101",
            end_date="20241231",
            max_workers=1,
            force=False,
            sleep_seconds=5.0,
            stock_retries=3,
            stock_retry_backoff=30.0,
        )

    def test_download_workflow_routes_workers_and_force_to_dart_business_info(self):
        with (
            patch.object(
                sys,
                "argv",
                ["prog", "--workers", "3", "--force", "--sleep-seconds", "1.25", "business-info"],
            ),
            patch.object(download_workflow, "_stock_codes", return_value=["005930"]),
            patch("engine.extractors.filings.download_business_infos") as download_business_infos,
        ):
            download_workflow.main()

        download_business_infos.assert_called_once_with(
            ["005930"],
            0,
            start_date=None,
            end_date=None,
            max_workers=3,
            force=True,
            sleep_seconds=1.25,
            stock_retries=3,
            stock_retry_backoff=30.0,
        )


if __name__ == "__main__":
    unittest.main()
