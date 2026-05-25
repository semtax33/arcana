from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name
from engine.transformers.market_data import (
    normalize_price,
    normalize_shares,
    normalize_us_price,
    read_normalized_us_price,
)


PRICE_TABLE = "price_daily"
SHARES_TABLE = "stock_shares"


def _insert_partitioned(client, table_name, frame):
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"])
    frame["_partition"] = frame["trade_date"].dt.strftime("%Y%m")
    for partition, chunk in frame.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date
        client.insert_df(table_name, chunk, column_names=list(chunk.columns))
        print(f"inserted partition={partition}, rows={len(chunk):,}")


def insert_price(
    *,
    market: str = "kr",
    source: str = "bronze",
    dry_run: bool = False,
    client: Any = None,
):
    normalized_price_df = create_price_dataframe(market=market, source=source)
    normalized_price_df.attrs["inserted_rows"] = 0
    if normalized_price_df.empty or dry_run:
        normalized_price_df.attrs["inserted_rows"] = len(normalized_price_df)
        return normalized_price_df

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        _insert_partitioned(client, PRICE_TABLE, normalized_price_df)
        normalized_price_df.attrs["inserted_rows"] = len(normalized_price_df)
    finally:
        if owns_client:
            client.close()
    return normalized_price_df


def insert_shares(
    *,
    market: str = "kr",
    source: str = "bronze",
    dry_run: bool = False,
    client: Any = None,
):
    market = _normalize_market(market)
    if market != "kr":
        raise ValueError("shares loading is currently supported only for market='kr'")

    normalized_shares_df = create_shares_dataframe(source=source)
    normalized_shares_df.attrs["inserted_rows"] = 0
    if normalized_shares_df.empty or dry_run:
        normalized_shares_df.attrs["inserted_rows"] = len(normalized_shares_df)
        return normalized_shares_df

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        _insert_partitioned(client, SHARES_TABLE, normalized_shares_df)
        normalized_shares_df.attrs["inserted_rows"] = len(normalized_shares_df)
    finally:
        if owns_client:
            client.close()
    return normalized_shares_df


def create_price_dataframe(*, market: str = "kr", source: str = "bronze") -> pd.DataFrame:
    market = _normalize_market(market)
    source = _normalize_source(source)
    if market == "kr":
        if source == "silver":
            return _read_silver_market_csv(DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price")))
        return normalize_price(str(DATA_LAKE.bronze("krx", "price", "*")))
    if market == "us":
        if source == "silver":
            return read_normalized_us_price()
        return normalize_us_price(str(DATA_LAKE.bronze("yfinance", "price", "*.csv")))
    raise ValueError(f"unsupported market: {market}")


def create_shares_dataframe(*, source: str = "bronze") -> pd.DataFrame:
    source = _normalize_source(source)
    if source == "silver":
        return _read_silver_market_csv(DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares")))
    return normalize_shares(str(DATA_LAKE.bronze("krx", "shares", "*")))


def main():
    parser = argparse.ArgumentParser(description="Insert market price/share data into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--target", default="all", choices=["all", "prices", "shares"])
    parser.add_argument("--source", default="bronze", choices=["bronze", "silver"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.target in {"all", "prices"}:
        result = insert_price(market=args.market, source=args.source, dry_run=args.dry_run)
        print(f"prepared price rows={result.attrs.get('inserted_rows', len(result)):,}")

    if args.target in {"all", "shares"}:
        if args.market != "kr":
            raise SystemExit("shares target is currently supported only for market=kr")
        result = insert_shares(market=args.market, source=args.source, dry_run=args.dry_run)
        print(f"prepared share rows={result.attrs.get('inserted_rows', len(result)):,}")


def _read_silver_market_csv(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "trade_date" in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _normalize_market(market: str) -> str:
    return str(market or "kr").strip().lower()


def _normalize_source(source: str) -> str:
    source = str(source or "bronze").strip().lower()
    if source not in {"bronze", "silver"}:
        raise ValueError("source must be 'bronze' or 'silver'")
    return source


if __name__ == "__main__":
    main()
