import clickhouse_connect

from engine.dividend_normalizer import create_all_stock_dividend_dataframe


def insert_dividends():
    client = clickhouse_connect.get_client(
        host="127.0.0.1",      # 또는 WSL IP: "172.xx.xx.xx"
        port=8123,             # ClickHouse HTTP port
        username="default",
        password="default",  # 비밀번호 없으면 "" 또는 생략
        database="arcana",
    )

    normalized_stock_dividends_df = create_all_stock_dividend_dataframe()
    normalized_stock_dividends_df.to_csv('../data-lake/silver/dart/dividend/dividend_normalized.csv')
    
    normalized_stock_dividends_df["_partition"] = normalized_stock_dividends_df["trade_date"].dt.strftime("%Y%m")

    for partition, chunk in normalized_stock_dividends_df.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date

        client.insert_df(
            "stock_dividend",
            chunk,
            column_names=list(chunk.columns),
        )

        print(f"inserted partition={partition}, rows={len(chunk):,}")

    client.close()

def main():
    insert_dividends()

if __name__ == "__main__":
    main()