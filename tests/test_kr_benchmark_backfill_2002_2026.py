from __future__ import annotations

import pandas as pd


def test_benchmark_contract_uses_only_requested_indices() -> None:
    from scripts.backfill_kr_benchmarks_2002_2026 import BENCHMARK_SYMBOLS

    assert BENCHMARK_SYMBOLS == {"KOSPI200": "KS200", "KOSDAQ": "KQ11"}


def test_missing_source_rows_is_non_destructive_anti_join() -> None:
    from scripts.backfill_kr_benchmarks_2002_2026 import missing_source_rows

    source = pd.DataFrame(
        {
            "benchmark_id": ["KOSPI200", "KOSPI200", "KOSDAQ"],
            "trade_date": pd.to_datetime(
                ["2002-01-02", "2002-01-03", "2002-01-02"]
            ).date,
            "close": [90.0, 91.0, 700.0],
        }
    )
    existing = source.iloc[[0]].copy()

    missing = missing_source_rows(source, existing)

    assert list(zip(missing["benchmark_id"], missing["trade_date"].astype(str))) == [
        ("KOSPI200", "2002-01-03"),
        ("KOSDAQ", "2002-01-02"),
    ]
