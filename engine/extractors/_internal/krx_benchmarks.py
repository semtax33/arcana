from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from engine.transformers.benchmarks import (
    BRONZE_BENCHMARK_DIR,
    BRONZE_YFINANCE_BENCHMARK_DIR,
    DEFAULT_BENCHMARK_INDEX_CODES,
    DEFAULT_YFINANCE_BENCHMARK_TICKERS,
    YFINANCE_BENCHMARK_OUTPUT_NAMES,
    _concat_benchmark_frames,
    _resolve_benchmark_ids,
    normalize_benchmark_id,
    normalize_benchmark_price_frame,
    normalize_provider_date,
)


def fetch_benchmark_price(
    benchmark_id: str,
    start_date: str | date,
    end_date: str | date,
    *,
    benchmark_index_codes: dict[str, str] | None = None,
) -> pd.DataFrame:
    from pykrx import stock

    benchmark_id = normalize_benchmark_id(benchmark_id)
    index_codes = benchmark_index_codes or DEFAULT_BENCHMARK_INDEX_CODES
    index_code = index_codes.get(benchmark_id)
    if index_code is None:
        raise ValueError(f"unsupported benchmark_id: {benchmark_id}")

    raw_df = stock.get_index_ohlcv_by_date(
        normalize_provider_date(start_date),
        normalize_provider_date(end_date),
        index_code,
    )
    return normalize_benchmark_price_frame(raw_df, benchmark_id=benchmark_id)


def fetch_benchmark_prices(
    start_date: str | date,
    end_date: str | date,
    *,
    benchmark_ids: list[str] | None = None,
    output_dir: str | Path | None = BRONZE_BENCHMARK_DIR,
    benchmark_index_codes: dict[str, str] | None = None,
) -> pd.DataFrame:
    index_codes = benchmark_index_codes or DEFAULT_BENCHMARK_INDEX_CODES
    resolved_ids = _resolve_benchmark_ids(benchmark_ids, index_codes)
    frames = [
        fetch_benchmark_price(
            benchmark_id,
            start_date,
            end_date,
            benchmark_index_codes=index_codes,
        )
        for benchmark_id in resolved_ids
    ]
    result = _concat_benchmark_frames(frames)

    if output_dir is not None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        for benchmark_id, rows in result.groupby("benchmark_id", sort=True):
            rows.to_csv(output_path / f"{benchmark_id}.csv", index=False, encoding="utf-8-sig")

    return result


def fetch_yfinance_benchmark_price(
    benchmark_id: str,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    *,
    benchmark_tickers: dict[str, str] | None = None,
) -> pd.DataFrame:
    from engine.extractors._internal.yfinance_market_prices import fetch_yfinance_price

    benchmark_id = normalize_benchmark_id(benchmark_id)
    tickers = benchmark_tickers or DEFAULT_YFINANCE_BENCHMARK_TICKERS
    ticker = tickers.get(benchmark_id)
    if ticker is None:
        raise ValueError(f"unsupported yfinance benchmark_id: {benchmark_id}")

    raw_df = fetch_yfinance_price(
        ticker,
        start_date=start_date,
        end_date=end_date,
        normalize_ticker=False,
    )
    return normalize_benchmark_price_frame(raw_df, benchmark_id=benchmark_id)


def fetch_yfinance_benchmark_prices(
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    *,
    benchmark_ids: list[str] | None = None,
    output_dir: str | Path | None = BRONZE_YFINANCE_BENCHMARK_DIR,
    benchmark_tickers: dict[str, str] | None = None,
) -> pd.DataFrame:
    tickers = benchmark_tickers or DEFAULT_YFINANCE_BENCHMARK_TICKERS
    resolved_ids = _resolve_benchmark_ids(benchmark_ids, tickers)
    frames = [
        fetch_yfinance_benchmark_price(
            benchmark_id,
            start_date,
            end_date,
            benchmark_tickers=tickers,
        )
        for benchmark_id in resolved_ids
    ]
    result = _concat_benchmark_frames(frames)

    if output_dir is not None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        for benchmark_id, rows in result.groupby("benchmark_id", sort=True):
            output_name = YFINANCE_BENCHMARK_OUTPUT_NAMES.get(benchmark_id, f"{benchmark_id}.csv")
            rows.to_csv(output_path / output_name, index=False, encoding="utf-8-sig")

    return result
