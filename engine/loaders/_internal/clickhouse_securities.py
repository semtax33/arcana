from __future__ import annotations

import argparse

from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.securities import (
    get_normalized_identifier,
    get_normalized_sector_and_issuer,
    get_normalized_security_master,
)


def insert_issuer(*, market: str = "kr", dry_run: bool = False, client=None) -> int:
    normalized_issuer_df = get_normalized_sector_and_issuer(market=market)
    if normalized_issuer_df.empty or dry_run:
        return len(normalized_issuer_df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        client.insert_df("issuers", normalized_issuer_df, column_names=list(normalized_issuer_df.columns))
    finally:
        if owns_client:
            client.close()
    return len(normalized_issuer_df)


def insert_security_master(*, market: str = "kr", dry_run: bool = False, client=None) -> int:
    normalized_security_master_df = get_normalized_security_master(market=market)
    if normalized_security_master_df.empty or dry_run:
        return len(normalized_security_master_df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        client.insert_df(
            "security_master",
            normalized_security_master_df,
            column_names=list(normalized_security_master_df.columns),
        )
    finally:
        if owns_client:
            client.close()
    return len(normalized_security_master_df)


def insert_identifier(*, market: str = "kr", dry_run: bool = False, client=None) -> int:
    normalized_identifier_df = get_normalized_identifier(market=market)
    if normalized_identifier_df.empty or dry_run:
        return len(normalized_identifier_df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        client.insert_df("identifiers", normalized_identifier_df, column_names=list(normalized_identifier_df.columns))
    finally:
        if owns_client:
            client.close()
    return len(normalized_identifier_df)


def insert_securities(*, market: str = "kr", target: str = "all", dry_run: bool = False, client=None) -> dict[str, int]:
    target = str(target or "all").strip().lower()
    targets = {
        "issuers": insert_issuer,
        "security-master": insert_security_master,
        "identifiers": insert_identifier,
    }
    if target != "all" and target not in targets:
        choices = ", ".join(["all", *targets])
        raise ValueError(f"unknown target: {target}; choices: {choices}")

    owns_client = client is None and not dry_run
    client = client or (None if dry_run else get_clickhouse_client())
    try:
        selected = targets.items() if target == "all" else [(target, targets[target])]
        return {
            name: loader(market=market, dry_run=dry_run, client=client)
            for name, loader in selected
        }
    finally:
        if owns_client and client is not None:
            client.close()


def main():
    parser = argparse.ArgumentParser(description="Insert security reference data into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--target", default="all", choices=["all", "issuers", "security-master", "identifiers"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows_by_target = insert_securities(market=args.market, target=args.target, dry_run=args.dry_run)
    action = "prepared" if args.dry_run else "inserted"
    for target, rows in rows_by_target.items():
        print(f"{action} {target} market={args.market} rows={rows:,}")


if __name__ == "__main__":
    main()
