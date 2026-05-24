import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from engine.transformers.benchmarks import (
    BENCHMARK_PRICE_COLUMNS,
    normalize_benchmark_price_frame,
    normalize_benchmark_prices,
)


class BenchmarkNormalizerTest(unittest.TestCase):
    def test_normalize_benchmark_price_frame_handles_pykrx_shape(self):
        raw = pd.DataFrame(
            {
                "\uc2dc\uac00": [100.0, 101.0],
                "\uace0\uac00": [110.0, 111.0],
                "\uc800\uac00": [90.0, 91.0],
                "\uc885\uac00": [105.0, 106.0],
                "\uac70\ub798\ub7c9": [1_000, 1_100],
            },
            index=pd.to_datetime(["2026-01-02", "2026-01-03"]),
        )

        result = normalize_benchmark_price_frame(raw, benchmark_id="kospi200")

        self.assertEqual(result.columns.tolist(), BENCHMARK_PRICE_COLUMNS)
        self.assertEqual(result["benchmark_id"].tolist(), ["KOSPI200", "KOSPI200"])
        self.assertEqual(result["trade_date"].astype(str).tolist(), ["2026-01-02", "2026-01-03"])
        self.assertEqual(result["close"].tolist(), [105.0, 106.0])
        self.assertEqual(result["currency"].tolist(), ["KRW", "KRW"])

    def test_normalize_benchmark_prices_reads_bronze_files_and_writes_silver(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bronze = root / "bronze"
            silver = root / "silver" / "normalized_benchmark_price.csv"
            bronze.mkdir()
            (bronze / "KOSDAQ.csv").write_text(
                "\n".join(
                    [
                        "trade_date,open,high,low,close,volume",
                        "2026-01-02,100,110,90,105,1000",
                    ]
                ),
                encoding="utf-8",
            )

            result = normalize_benchmark_prices(str(bronze / "*.csv"), output_path=silver)

            self.assertTrue(silver.exists())
            self.assertEqual(result["benchmark_id"].tolist(), ["KOSDAQ"])
            self.assertEqual(result["close"].tolist(), [105])


if __name__ == "__main__":
    unittest.main()
