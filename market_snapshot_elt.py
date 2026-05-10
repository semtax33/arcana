import clickhouse_connect

from market_snapshot_normalizer import normalize_price, normalize_shares

def insert_price():
    client = clickhouse_connect.get_client(
        host="127.0.0.1",      # 또는 WSL IP: "172.xx.xx.xx"
        port=8123,             # ClickHouse HTTP port
        username="default",
        password="default",  # 비밀번호 없으면 "" 또는 생략
        database="arcana",
    )

    
    normalized_price_df = normalize_price("./data-lake/bronze/krx/price/*")
    
    normalized_price_df["_partition"] = normalized_price_df["trade_date"].dt.strftime("%Y%m")

    for partition, chunk in normalized_price_df.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date

        client.insert_df(
            "price_daily",
            chunk,
            column_names=list(chunk.columns),
        )

        print(f"inserted partition={partition}, rows={len(chunk):,}")

    client.close()

def insert_shares():
    client = clickhouse_connect.get_client(
        host="127.0.0.1",      # 또는 WSL IP: "172.xx.xx.xx"
        port=8123,             # ClickHouse HTTP port
        username="default",
        password="default",  # 비밀번호 없으면 "" 또는 생략
        database="arcana",
    )

    normalized_shares_df = normalize_shares("./data-lake/bronze/krx/shares/*")

    normalized_shares_df["_partition"] = normalized_shares_df["trade_date"].dt.strftime("%Y%m")

    for partition, chunk in normalized_shares_df.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date

        client.insert_df(
            "stock_shares",
            chunk,
            column_names=list(chunk.columns),
        )

        print(f"inserted partition={partition}, rows={len(chunk):,}")

    client.close()

def main():
    insert_price()
    insert_shares()

if __name__ == "__main__":
    main()