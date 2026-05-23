from __future__ import annotations

import argparse
from datetime import date
from typing import Any

import pandas as pd

from benchmark_normalizer import (
    DEFAULT_BENCHMARK_INDEX_CODES,
    create_benchmark_price_dataframe,
    fetch_benchmark_prices,
    normalize_benchmark_prices,
)


CLICKHOUSE_CONFIG = {
    "host": "127.0.0.1",
    "port": 8123,
    "username": "default",
    "password": "default",
    "database": "arcana",
}

BENCHMARK_TABLE = "benchmark_price_daily"


def get_clickhouse_client(**overrides: Any) -> Any:
    import clickhouse_connect

    config = {**CLICKHOUSE_CONFIG, **overrides}
    return clickhouse_connect.get_client(**config)


def insert_benchmark_prices(
    *,
    benchmark_ids: list[str] | None = None,
    start_date: str | date = "2010-01-01",
    end_date: str | date | None = None,
    source: str = "pykrx",
    bronze_path: str | None = None,
    dry_run: bool = False,
    client: Any = None,
) -> pd.DataFrame:
    end_date = end_date or date.today()
    benchmark_df = create_benchmark_price_dataframe(
        start_date,
        end_date,
        benchmark_ids=benchmark_ids,
        source=source,
        bronze_path=bronze_path,
    )
    if benchmark_df.empty or dry_run:
        benchmark_df.attrs["inserted_rows"] = 0 if benchmark_df.empty else len(benchmark_df)
        return benchmark_df

    owns_client = client is None
    client = client or get_clickhouse_client()
    inserted_rows = 0
    try:
        benchmark_df = benchmark_df.copy()
        benchmark_df["_partition"] = pd.to_datetime(benchmark_df["trade_date"]).dt.strftime("%Y%m")
        for partition, chunk in benchmark_df.groupby("_partition", sort=True):
            chunk = chunk.drop(columns=["_partition"]).copy()
            chunk["trade_date"] = pd.to_datetime(chunk["trade_date"]).dt.date
            client.insert_df(
                BENCHMARK_TABLE,
                chunk,
                column_names=list(chunk.columns),
            )
            inserted_rows += len(chunk)
            print(f"inserted benchmark partition={partition}, rows={len(chunk):,}")
    finally:
        if owns_client:
            client.close()

    result = pd.DataFrame(columns=benchmark_df.drop(columns=["_partition"]).columns)
    result.attrs["inserted_rows"] = inserted_rows
    return result


def download_benchmark_prices(
    *,
    benchmark_ids: list[str] | None = None,
    start_date: str | date = "2010-01-01",
    end_date: str | date | None = None,
    output_dir: str | None = None,
) -> pd.DataFrame:
    return fetch_benchmark_prices(
        start_date,
        end_date or date.today(),
        benchmark_ids=benchmark_ids,
        output_dir=output_dir,
    )


def normalize_downloaded_benchmark_prices(path: str | None = None) -> pd.DataFrame:
    return normalize_benchmark_prices(path)


def _parse_benchmark_ids(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Insert benchmark index prices into ClickHouse.")
    parser.add_argument(
        "--benchmark-ids",
        default=",".join(sorted(DEFAULT_BENCHMARK_INDEX_CODES)),
        help="Comma-separated benchmark ids. Defaults to KOSPI200,KOSDAQ.",
    )
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--source", choices=["pykrx", "bronze"], default="pykrx")
    parser.add_argument("--bronze-path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = insert_benchmark_prices(
        benchmark_ids=_parse_benchmark_ids(args.benchmark_ids),
        start_date=args.start_date,
        end_date=args.end_date,
        source=args.source,
        bronze_path=args.bronze_path,
        dry_run=args.dry_run,
    )
    print(f"prepared benchmark rows={result.attrs.get('inserted_rows', len(result)):,}")


if __name__ == "__main__":
    main()
