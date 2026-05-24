from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.securities import (
    get_normalized_identifier,
    get_normalized_sector_and_issuer,
    get_normalized_security_master,
)


def insert_issuer():
    client = get_clickhouse_client()
    normalized_issuer_df = get_normalized_sector_and_issuer()
    client.insert_df("issuers", normalized_issuer_df, column_names=list(normalized_issuer_df.columns))
    client.close()


def insert_security_master():
    client = get_clickhouse_client()
    normalized_security_master_df = get_normalized_security_master()
    client.insert_df(
        "security_master",
        normalized_security_master_df,
        column_names=list(normalized_security_master_df.columns),
    )
    client.close()


def insert_identifier():
    client = get_clickhouse_client()
    normalized_identifier_df = get_normalized_identifier()
    client.insert_df("identifiers", normalized_identifier_df, column_names=list(normalized_identifier_df.columns))
    client.close()


def main():
    insert_issuer()
    insert_security_master()
    insert_identifier()


if __name__ == "__main__":
    main()
