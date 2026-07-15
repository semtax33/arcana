from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal import krx_market_prices


class KrxMarketPricesTest(unittest.TestCase):
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
