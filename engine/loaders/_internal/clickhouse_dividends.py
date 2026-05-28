from __future__ import annotations

import argparse
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name
from engine.transformers.dividends import (
    create_all_stock_dividend_dataframe,
    silver_dividend_dir,
    write_us_sec_dividend_events_file,
    write_silver_dividend_summary_files,
)


STOCK_DIVIDEND_TABLE = "stock_dividend"
STOCK_DIVIDEND_COLUMNS = [
    "security_id",
    "trade_date",
    "dividend",
    "payout_ratio",
    "dividend_percent",
    "currency",
    "updated_at",
]
_DIVIDEND_QUANT = Decimal("0.000001")


def dividend_output_path(market: str = "kr") -> Path:
    market = _normalize_market(market)
    if market == "kr":
        return silver_dividend_dir / "dividend_normalized.csv"
    if market == "us":
        return DATA_LAKE.silver("us", "dividend", market_csv_name("dividend_normalized", market="us"))
    raise ValueError(f"unsupported market: {market}")


def refresh_silver_dividend_files(*, market: str = "kr", path: str | Path | None = None):
    market = _normalize_market(market)
    if market == "kr":
        by_kind_df, company_df, failed_df = write_silver_dividend_summary_files()
        print(
            "refreshed silver dividend summaries: "
            f"by_stock_kind={len(by_kind_df):,}, "
            f"company_summary={len(company_df):,}, "
            f"failed={len(failed_df):,}"
        )
    elif market == "us" and path is None:
        events_df = write_us_sec_dividend_events_file()
        print(f"refreshed US SEC dividend events: rows={len(events_df):,}")

    normalized_stock_dividends_df = create_all_stock_dividend_dataframe(market=market, path=path)
    output_path = dividend_output_path(market)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_stock_dividends_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"refreshed silver dividend daily file: market={market} rows={len(normalized_stock_dividends_df):,}")
    return normalized_stock_dividends_df


def prepare_stock_dividend_for_clickhouse(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=STOCK_DIVIDEND_COLUMNS)

    result = df.copy()
    for column in STOCK_DIVIDEND_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA

    result["security_id"] = result["security_id"].astype(str).str.strip()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result = result.loc[(result["security_id"] != "") & result["trade_date"].notna()].copy()
    if result.empty:
        return pd.DataFrame(columns=STOCK_DIVIDEND_COLUMNS)

    result["trade_date"] = result["trade_date"].dt.date
    result["dividend"] = result["dividend"].map(_to_clickhouse_decimal)
    for column in ["payout_ratio", "dividend_percent"]:
        result[column] = result[column].map(_to_nullable_float)
    result["currency"] = result["currency"].fillna("").astype(str)
    result["updated_at"] = _loader_updated_at()
    return result[STOCK_DIVIDEND_COLUMNS].reset_index(drop=True)


def insert_dividends(*, market: str = "kr", path: str | Path | None = None, dry_run: bool = False, client=None) -> int:
    normalized_stock_dividends_df = refresh_silver_dividend_files(market=market, path=path)
    insert_df = prepare_stock_dividend_for_clickhouse(normalized_stock_dividends_df)
    if insert_df.empty or dry_run:
        return len(insert_df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        insert_df["_partition"] = pd.to_datetime(insert_df["trade_date"]).dt.strftime("%Y%m")
        for partition, chunk in insert_df.groupby("_partition", sort=True):
            chunk = chunk.drop(columns=["_partition"]).copy()
            client.insert_df(STOCK_DIVIDEND_TABLE, chunk, column_names=STOCK_DIVIDEND_COLUMNS)
            print(f"inserted partition={partition}, rows={len(chunk):,}")
    finally:
        if owns_client:
            client.close()
    return len(insert_df)


def main():
    parser = argparse.ArgumentParser(description="Insert stock dividend data into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument(
        "--path",
        default=None,
        help="Optional source CSV path. For US, omit for SEC events or pass an event/yfinance legacy CSV.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    inserted = insert_dividends(market=args.market, path=args.path, dry_run=args.dry_run)
    action = "prepared" if args.dry_run else "inserted"
    print(f"{action} stock_dividend market={args.market} rows={inserted:,}")


def _normalize_market(market: str) -> str:
    return str(market or "kr").strip().lower()


def _to_clickhouse_decimal(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        decimal_value = Decimal(str(value)).quantize(_DIVIDEND_QUANT)
    except (InvalidOperation, ValueError):
        return None
    return decimal_value if decimal_value.is_finite() else None


def _to_nullable_float(value):
    try:
        number = float(value)
    except Exception:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _loader_updated_at():
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


if __name__ == "__main__":
    main()
