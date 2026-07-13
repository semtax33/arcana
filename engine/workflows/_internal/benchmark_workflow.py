from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from engine.extractors.benchmarks import fetch_benchmark_prices, fetch_yfinance_benchmark_prices
from engine.transformers.benchmarks import normalize_benchmark_prices


DEFAULT_PROVIDER_SOURCE = "pykrx"
YFINANCE_PROVIDER_SOURCE = "yfinance"
BRONZE_PROVIDER_SOURCE = "bronze"


def create_benchmark_price_dataframe(
    start_date: str | date,
    end_date: str | date,
    *,
    benchmark_ids: list[str] | None = None,
    source: str = DEFAULT_PROVIDER_SOURCE,
    bronze_path: str | Path | None = None,
) -> pd.DataFrame:
    if source == DEFAULT_PROVIDER_SOURCE:
        return fetch_benchmark_prices(
            start_date,
            end_date,
            benchmark_ids=benchmark_ids,
            output_dir=None,
        )
    if source == YFINANCE_PROVIDER_SOURCE:
        return fetch_yfinance_benchmark_prices(
            start_date,
            end_date,
            benchmark_ids=benchmark_ids,
            output_dir=None,
        )
    if source == BRONZE_PROVIDER_SOURCE:
        return normalize_benchmark_prices(bronze_path, output_path=None)
    raise ValueError(
        f"source must be '{DEFAULT_PROVIDER_SOURCE}', "
        f"'{YFINANCE_PROVIDER_SOURCE}', or '{BRONZE_PROVIDER_SOURCE}'"
    )
