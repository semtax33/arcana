from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.transformers.market_data import normalize_price, normalize_shares


def _insert_partitioned(client, table_name, frame):
    frame = frame.copy()
    frame["_partition"] = frame["trade_date"].dt.strftime("%Y%m")
    for partition, chunk in frame.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        chunk["trade_date"] = chunk["trade_date"].dt.date
        client.insert_df(table_name, chunk, column_names=list(chunk.columns))
        print(f"inserted partition={partition}, rows={len(chunk):,}")


def insert_price():
    client = get_clickhouse_client()
    try:
        normalized_price_df = normalize_price(str(DATA_LAKE.bronze("krx", "price", "*")))
        _insert_partitioned(client, "price_daily", normalized_price_df)
    finally:
        client.close()


def insert_shares():
    client = get_clickhouse_client()
    try:
        normalized_shares_df = normalize_shares(str(DATA_LAKE.bronze("krx", "shares", "*")))
        _insert_partitioned(client, "stock_shares", normalized_shares_df)
    finally:
        client.close()


def main():
    insert_price()
    insert_shares()


if __name__ == "__main__":
    main()
