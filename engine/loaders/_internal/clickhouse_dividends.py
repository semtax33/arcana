from __future__ import annotations

import argparse
from pathlib import Path

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name
from engine.transformers.dividends import (
    create_all_stock_dividend_dataframe,
    silver_dividend_dir,
    write_silver_dividend_summary_files,
)


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

    normalized_stock_dividends_df = create_all_stock_dividend_dataframe(market=market, path=path)
    output_path = dividend_output_path(market)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_stock_dividends_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"refreshed silver dividend daily file: market={market} rows={len(normalized_stock_dividends_df):,}")
    return normalized_stock_dividends_df


def insert_dividends(*, market: str = "kr", path: str | Path | None = None, dry_run: bool = False, client=None) -> int:
    normalized_stock_dividends_df = refresh_silver_dividend_files(market=market, path=path)
    if normalized_stock_dividends_df.empty or dry_run:
        return len(normalized_stock_dividends_df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        normalized_stock_dividends_df["_partition"] = normalized_stock_dividends_df["trade_date"].dt.strftime("%Y%m")
        for partition, chunk in normalized_stock_dividends_df.groupby("_partition", sort=True):
            chunk = chunk.drop(columns=["_partition"]).copy()
            chunk["trade_date"] = chunk["trade_date"].dt.date
            client.insert_df("stock_dividend", chunk, column_names=list(chunk.columns))
            print(f"inserted partition={partition}, rows={len(chunk):,}")
    finally:
        if owns_client:
            client.close()
    return len(normalized_stock_dividends_df)


def main():
    parser = argparse.ArgumentParser(description="Insert stock dividend data into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--path", default=None, help="Optional source CSV path. For US, this is a yfinance price CSV.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    inserted = insert_dividends(market=args.market, path=args.path, dry_run=args.dry_run)
    action = "prepared" if args.dry_run else "inserted"
    print(f"{action} stock_dividend market={args.market} rows={inserted:,}")


def _normalize_market(market: str) -> str:
    return str(market or "kr").strip().lower()


if __name__ == "__main__":
    main()
