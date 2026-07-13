import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, patch

import pandas as pd

from engine.extractors.benchmarks import fetch_yfinance_benchmark_prices
from engine.workflows._internal import download_workflow


class BenchmarkDownloadTest(unittest.TestCase):
    def test_fetch_yfinance_benchmark_prices_downloads_us_defaults_with_aliases(self):
        raw = pd.DataFrame(
            {
                "Date": ["2026-01-02"],
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Volume": [1_000],
            }
        )

        with TemporaryDirectory() as temp_dir:
            with patch(
                "engine.extractors._internal.yfinance_market_prices.fetch_yfinance_price",
                return_value=raw,
            ) as fetch:
                result = fetch_yfinance_benchmark_prices(
                    "20260101",
                    "20260105",
                    benchmark_ids=["nasdaq", "S&P500"],
                    output_dir=temp_dir,
                )

            output_dir = Path(temp_dir)
            self.assertTrue((output_dir / "us_nasdaq.csv").exists())
            self.assertTrue((output_dir / "us_sp500.csv").exists())

        self.assertEqual(result["benchmark_id"].tolist(), ["US_NASDAQ", "US_SP500"])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["^IXIC", "^GSPC"])
        for call in fetch.call_args_list:
            self.assertFalse(call.kwargs["normalize_ticker"])

    def test_download_workflow_routes_us_benchmarks(self):
        frame = pd.DataFrame({"benchmark_id": ["US_SP500"], "trade_date": ["2026-01-02"]})

        with (
            patch.object(
                sys,
                "argv",
                ["prog", "--market", "us", "--benchmark-ids", "S&P500", "benchmarks"],
            ),
            patch.object(download_workflow, "fetch_yfinance_benchmark_prices", return_value=frame) as fetch,
        ):
            download_workflow.main()

        fetch.assert_called_once_with(
            "20100101",
            ANY,
            benchmark_ids=["S&P500"],
        )


if __name__ == "__main__":
    unittest.main()
