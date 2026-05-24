from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.dividends import (
    create_all_stock_dividend_dataframe,
    silver_dividend_dir,
    write_silver_dividend_summary_files,
)


def refresh_silver_dividend_files():
    by_kind_df, company_df, failed_df = write_silver_dividend_summary_files()
    print(
        "refreshed silver dividend summaries: "
        f"by_stock_kind={len(by_kind_df):,}, "
        f"company_summary={len(company_df):,}, "
        f"failed={len(failed_df):,}"
    )

    normalized_stock_dividends_df = create_all_stock_dividend_dataframe()
    output_path = silver_dividend_dir / "dividend_normalized.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_stock_dividends_df.to_csv(output_path)
    print(f"refreshed silver dividend daily file: rows={len(normalized_stock_dividends_df):,}")
    return normalized_stock_dividends_df


def insert_dividends():
    normalized_stock_dividends_df = refresh_silver_dividend_files()
    client = get_clickhouse_client()
    try:
        normalized_stock_dividends_df["_partition"] = normalized_stock_dividends_df["trade_date"].dt.strftime("%Y%m")
        for partition, chunk in normalized_stock_dividends_df.groupby("_partition", sort=True):
            chunk = chunk.drop(columns=["_partition"]).copy()
            chunk["trade_date"] = chunk["trade_date"].dt.date
            client.insert_df("stock_dividend", chunk, column_names=list(chunk.columns))
            print(f"inserted partition={partition}, rows={len(chunk):,}")
    finally:
        client.close()


def main():
    insert_dividends()


if __name__ == "__main__":
    main()
