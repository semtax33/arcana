import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.benchmark_elt import BENCHMARK_TABLE, insert_benchmark_prices


class FakeClickHouseClient:
    def __init__(self):
        self.inserted = []
        self.closed = False

    def insert_df(self, table, df, column_names=None):
        self.inserted.append((table, df.copy(), list(column_names or [])))

    def close(self):
        self.closed = True


class BenchmarkEltTest(unittest.TestCase):
    def test_insert_benchmark_prices_inserts_monthly_partitions(self):
        benchmark_df = pd.DataFrame(
            {
                "benchmark_id": ["KOSPI200", "KOSPI200"],
                "trade_date": pd.to_datetime(["2026-01-31", "2026-02-02"]).date,
                "open": [100, 110],
                "high": [101, 111],
                "low": [99, 109],
                "close": [100.5, 110.5],
                "volume": [1_000, 1_100],
                "currency": ["KRW", "KRW"],
            }
        )
        client = FakeClickHouseClient()

        with patch("engine.benchmark_elt.create_benchmark_price_dataframe", return_value=benchmark_df):
            result = insert_benchmark_prices(client=client)

        self.assertEqual(result.attrs["inserted_rows"], 2)
        self.assertEqual([item[0] for item in client.inserted], [BENCHMARK_TABLE, BENCHMARK_TABLE])
        self.assertFalse(client.closed)
        for _, inserted_df, columns in client.inserted:
            self.assertNotIn("_partition", inserted_df.columns)
            self.assertEqual(columns, [
                "benchmark_id",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "currency",
            ])

    def test_insert_benchmark_prices_can_dry_run_from_bronze(self):
        with TemporaryDirectory() as temp_dir:
            bronze = Path(temp_dir) / "KOSDAQ.csv"
            bronze.write_text(
                "\n".join(
                    [
                        "trade_date,open,high,low,close,volume",
                        "2026-01-02,100,110,90,105,1000",
                    ]
                ),
                encoding="utf-8",
            )

            result = insert_benchmark_prices(
                source="bronze",
                bronze_path=str(bronze),
                dry_run=True,
            )

        self.assertEqual(result.attrs["inserted_rows"], 1)
        self.assertEqual(result["benchmark_id"].tolist(), ["KOSDAQ"])


if __name__ == "__main__":
    unittest.main()
