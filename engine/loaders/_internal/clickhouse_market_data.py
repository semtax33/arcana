from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name
from engine.loaders._internal.clickhouse_securities import insert_securities
from engine.transformers.market_data import (
    normalize_price,
    normalize_shares,
    normalize_us_price,
    read_normalized_us_price,
)


PRICE_TABLE = "price_daily"
SHARES_TABLE = "stock_shares"
CLICKHOUSE_DATE_MIN = pd.Timestamp("1970-01-01")
CLICKHOUSE_DATE_MAX = pd.Timestamp("2149-06-06")


def _insert_partitioned(client, table_name, frame):
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    missing_dates = frame["trade_date"].isna()
    if missing_dates.any():
        print(f"dropped rows with invalid trade_date rows={int(missing_dates.sum()):,}", flush=True)
        frame = frame.loc[~missing_dates].copy()

    valid_dates = frame["trade_date"].between(CLICKHOUSE_DATE_MIN, CLICKHOUSE_DATE_MAX)
    if not valid_dates.all():
        invalid = frame.loc[~valid_dates, "trade_date"]
        print(
            "dropped rows with ClickHouse Date out of range "
            f"rows={len(invalid):,}, min={invalid.min().date()}, max={invalid.max().date()}",
            flush=True,
        )
        frame = frame.loc[valid_dates].copy()

    frame["_partition"] = frame["trade_date"].dt.strftime("%Y%m")
    inserted_rows = 0
    for partition, chunk in frame.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date
        client.insert_df(table_name, chunk, column_names=list(chunk.columns))
        inserted_rows += len(chunk)
        print(f"inserted partition={partition}, rows={len(chunk):,}", flush=True)
    return inserted_rows


def insert_price(
    *,
    market: str = "kr",
    source: str = "bronze",
    dry_run: bool = False,
    client: Any = None,
    progress_interval: int = 100,
):
    market = _normalize_market(market)
    source = _normalize_source(source)
    print(
        f"loading price data market={market}, source={source}, dry_run={dry_run}",
        flush=True,
    )
    normalized_price_df = create_price_dataframe(
        market=market,
        source=source,
        progress_interval=progress_interval,
    )
    normalized_price_df.attrs["inserted_rows"] = 0
    if normalized_price_df.empty or dry_run:
        normalized_price_df.attrs["inserted_rows"] = len(normalized_price_df)
        action = "dry-run prepared" if dry_run else "skipped empty"
        print(f"{action} price rows={len(normalized_price_df):,}", flush=True)
        return normalized_price_df

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        print(f"inserting price rows={len(normalized_price_df):,}", flush=True)
        normalized_price_df.attrs["inserted_rows"] = _insert_partitioned(client, PRICE_TABLE, normalized_price_df)
    finally:
        if owns_client:
            client.close()
    print(f"loaded price rows={normalized_price_df.attrs['inserted_rows']:,}", flush=True)
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
        normalized_shares_df.attrs["inserted_rows"] = _insert_partitioned(client, SHARES_TABLE, normalized_shares_df)
    finally:
        if owns_client:
            client.close()
    return normalized_shares_df


def create_price_dataframe(
    *,
    market: str = "kr",
    source: str = "bronze",
    progress_interval: int = 100,
) -> pd.DataFrame:
    market = _normalize_market(market)
    source = _normalize_source(source)
    if market == "kr":
        if source == "silver":
            return _read_silver_market_csv(DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price")))
        return normalize_price(str(DATA_LAKE.bronze("krx", "price", "*")))
    if market == "us":
        if source == "silver":
            return read_normalized_us_price()
        return normalize_us_price(
            str(DATA_LAKE.bronze("yfinance", "price", "*.csv")),
            progress_interval=progress_interval,
        )
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
    parser.add_argument(
        "--skip-securities",
        action="store_true",
        help="For market=us, skip loading issuers/security_master/identifiers before prices.",
    )
    parser.add_argument("--progress-interval", type=int, default=100)
    args = parser.parse_args()

    should_load_securities = (
        args.market == "us"
        and not args.skip_securities
        and args.target in {"all", "prices"}
    )
    if should_load_securities:
        action = "prepared" if args.dry_run else "inserted"
        rows_by_target = insert_securities(market=args.market, target="all", dry_run=args.dry_run)
        for target, rows in rows_by_target.items():
            print(f"{action} {target} market={args.market} rows={rows:,}", flush=True)

    if args.target in {"all", "prices"}:
        result = insert_price(
            market=args.market,
            source=args.source,
            dry_run=args.dry_run,
            progress_interval=args.progress_interval,
        )
        print(f"prepared price rows={result.attrs.get('inserted_rows', len(result)):,}", flush=True)

    if args.target in {"all", "shares"}:
        if args.market != "kr":
            if args.target == "shares":
                raise SystemExit("shares target is currently supported only for market=kr")
            return
        result = insert_shares(market=args.market, source=args.source, dry_run=args.dry_run)
        print(f"prepared share rows={result.attrs.get('inserted_rows', len(result)):,}", flush=True)


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
