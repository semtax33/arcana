from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal import (
    finance_datareader_market_prices,
    krx_market_prices,
    marcap_market_prices,
)


class KrxMarketPricesTest(unittest.TestCase):
    def test_fetch_price_prefers_marcap_over_finance_datareader(self):
        expected = pd.DataFrame({krx_market_prices.DATE_COLUMN: ["2026-01-02"], "close": [100]})
        with (
            patch.object(krx_market_prices, "fetch_marcap_price", return_value=expected) as marcap,
            patch.object(krx_market_prices, "fetch_finance_datareader_price") as fdr,
        ):
            result = krx_market_prices.fetch_price("005930", "20260101", "20260131")

        self.assertIs(result, expected)
        self.assertEqual(result.attrs["provider"], "marcap")
        marcap.assert_called_once_with(
            "005930",
            "20260101",
            "20260131",
            cache_dir=marcap_market_prices.MARCAP_CACHE_DIR,
        )
        fdr.assert_not_called()

    def test_fetch_price_falls_back_to_finance_datareader(self):
        expected = pd.DataFrame({krx_market_prices.DATE_COLUMN: ["2026-01-02"], "close": [100]})
        with (
            patch.object(krx_market_prices, "fetch_marcap_price", side_effect=OSError("offline")),
            patch.object(krx_market_prices, "fetch_finance_datareader_price", return_value=expected) as fdr,
        ):
            result = krx_market_prices.fetch_price("005930", "20260101", "20260131")

        self.assertIs(result, expected)
        self.assertEqual(result.attrs["provider"], "finance-datareader")
        fdr.assert_called_once_with("005930", "20260101", "20260131")

    def test_marcap_normalizer_preserves_ohlcv_and_zero_pads_numeric_codes(self):
        source = pd.DataFrame(
            {
                "Date": pd.to_datetime(["1996-01-03"]),
                "Code": [5930],
                "Open": [138500],
                "High": [143500],
                "Low": [138000],
                "Close": [142000],
                "Volume": [221350],
                "ChangesRatio": [3.65],
            }
        )

        result = marcap_market_prices.normalize_marcap_price_frame(source)

        self.assertEqual(result.loc[0, "stock_code"], "005930")
        self.assertEqual(result.loc[0, marcap_market_prices.DATE_COLUMN], "1996-01-03")
        self.assertEqual(result.loc[0, marcap_market_prices.CLOSE_COLUMN], 142000)
        self.assertEqual(result.loc[0, marcap_market_prices.CHANGE_RATE_COLUMN], 3.65)

    def test_finance_datareader_normalizer_converts_fractional_change_to_percent(self):
        source = pd.DataFrame(
            {
                "Open": [100],
                "High": [110],
                "Low": [90],
                "Close": [105],
                "Volume": [1234],
                "Change": [0.05],
            },
            index=pd.to_datetime(["2026-01-02"]),
        )

        result = finance_datareader_market_prices.normalize_finance_datareader_price_frame(source)

        self.assertEqual(result.loc[0, finance_datareader_market_prices.DATE_COLUMN], "2026-01-02")
        self.assertEqual(result.loc[0, finance_datareader_market_prices.CHANGE_RATE_COLUMN], 5.0)

    def test_bulk_marcap_download_writes_all_historical_symbols(self):
        yearly = [
            (
                1996,
                pd.DataFrame(
                    {
                        "stock_code": ["005930", "000660"],
                        krx_market_prices.DATE_COLUMN: ["1996-01-03", "1996-01-03"],
                        "\uc2dc\uac00": [100, 200],
                        "\uace0\uac00": [110, 210],
                        "\uc800\uac00": [90, 190],
                        "\uc885\uac00": [105, 205],
                        "\uac70\ub798\ub7c9": [1000, 2000],
                        "\ub4f1\ub77d\ub960": [5.0, 2.5],
                    }
                ),
            )
        ]
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "price"
            with patch.object(krx_market_prices, "iter_marcap_price_years", return_value=iter(yearly)):
                result = krx_market_prices.fetch_all_prices(
                    None,
                    0,
                    "19960101",
                    "19961231",
                    output_dir=output_dir,
                    refresh_marcap_current_year=False,
                )
            samsung = pd.read_csv(output_dir / "kr_005930.csv")
            hynix = pd.read_csv(output_dir / "kr_000660.csv")

        self.assertEqual(result["provider"], "marcap")
        self.assertEqual(result["files"], 2)
        self.assertEqual(samsung[krx_market_prices.DATE_COLUMN].tolist(), ["1996-01-03"])
        self.assertEqual(hynix[krx_market_prices.DATE_COLUMN].tolist(), ["1996-01-03"])

    def test_write_download_then_merge_preserves_existing_rows_and_cleans_temp_files(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "kr_005930.csv"
            pd.DataFrame(
                {
                    krx_market_prices.DATE_COLUMN: ["2024-01-01", "2024-01-02"],
                    "close": [10, 20],
                }
            ).to_csv(path, index=False, encoding="utf-8-sig")

            merged = krx_market_prices._write_download_then_merge(
                path,
                pd.DataFrame(
                    {
                        krx_market_prices.DATE_COLUMN: ["2024-01-02 00:00:00", "2024-01-03 00:00:00"],
                        "close": [22, 30],
                    }
                ),
            )
            saved = pd.read_csv(path)
            temp_files = [
                item.name
                for item in root.iterdir()
                if ".download." in item.name or ".merge." in item.name
            ]

        self.assertEqual(merged[krx_market_prices.DATE_COLUMN].tolist(), ["2024-01-01", "2024-01-02", "2024-01-03"])
        self.assertEqual(saved["close"].tolist(), [10, 22, 30])
        self.assertEqual(temp_files, [])

    def test_write_download_then_merge_keeps_original_when_merge_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "kr_005930.csv"
            original = pd.DataFrame(
                {
                    krx_market_prices.DATE_COLUMN: ["2024-01-01"],
                    "close": [10],
                }
            )
            original.to_csv(path, index=False, encoding="utf-8-sig")

            with (
                patch.object(krx_market_prices, "_atomic_write_csv", side_effect=RuntimeError("boom")),
                self.assertRaisesRegex(RuntimeError, "boom"),
            ):
                krx_market_prices._write_download_then_merge(
                    path,
                    pd.DataFrame(
                        {
                            krx_market_prices.DATE_COLUMN: ["2024-01-02"],
                            "close": [20],
                        }
                    ),
                )
            saved = pd.read_csv(path)
            temp_files = [
                item.name
                for item in root.iterdir()
                if ".download." in item.name or ".merge." in item.name
            ]

        self.assertEqual(saved[krx_market_prices.DATE_COLUMN].tolist(), ["2024-01-01"])
        self.assertEqual(saved["close"].tolist(), [10])
        self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
