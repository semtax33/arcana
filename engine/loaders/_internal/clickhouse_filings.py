from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, first_existing_path, market_csv_name
from engine.transformers.filing_periods import LEGACY_REPORT_METADATA_PATH, REPORT_METADATA_PATH


US_REPORT_METADATA_PATH = DATA_LAKE.silver("sec", market_csv_name("report_metadata", market="us"))

DART_REPORT_METADATA_COLUMNS = [
    "security_id",
    "stock_code",
    "country",
    "market_mic",
    "filing_system",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "report_date",
    "rcept_no",
    "report_name",
    "source_type",
    "source_url",
    "updated_at",
]
DART_REPORT_METADATA_TABLE = "dart_report_metadata"


def report_metadata_path_for_market(market: str = "kr") -> Path:
    market = _normalize_market(market)
    if market == "kr":
        return first_existing_path(REPORT_METADATA_PATH, LEGACY_REPORT_METADATA_PATH)
    if market == "us":
        return US_REPORT_METADATA_PATH
    raise ValueError(f"unsupported market: {market}")


def read_report_metadata(
    path: str | Path | None = None,
    *,
    market: str = "kr",
) -> pd.DataFrame:
    resolved_path = report_metadata_path_for_market(market) if path is None else Path(path)
    path = (
        first_existing_path(REPORT_METADATA_PATH, LEGACY_REPORT_METADATA_PATH)
        if resolved_path == REPORT_METADATA_PATH
        else resolved_path
    )
    if not path.exists():
        return pd.DataFrame(columns=DART_REPORT_METADATA_COLUMNS)

    df = pd.read_csv(path, dtype={"stock_code": str, "rcept_no": str})
    market = _normalize_market(market)
    df["country"] = df["country"] if "country" in df.columns else market.upper()
    df["market_mic"] = df["market_mic"] if "market_mic" in df.columns else ("US" if market == "us" else "")
    df["filing_system"] = df["filing_system"] if "filing_system" in df.columns else ("SEC" if market == "us" else "DART")
    for column in DART_REPORT_METADATA_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[DART_REPORT_METADATA_COLUMNS].copy()
    df["period_end_date"] = pd.to_datetime(df["period_end_date"], errors="coerce").dt.date
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").dt.date
    df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    df["updated_at"] = df["updated_at"].fillna(pd.Timestamp.now())
    return df


def insert_report_metadata(
    path: str | Path | None = None,
    client=None,
    *,
    market: str = "kr",
    dry_run: bool = False,
) -> int:
    df = read_report_metadata(path, market=market)
    if df.empty:
        return 0

    if dry_run:
        return len(df)

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        for partition, chunk in _iter_report_metadata_partitions(df):
            client.insert_df(
                DART_REPORT_METADATA_TABLE,
                chunk,
                column_names=DART_REPORT_METADATA_COLUMNS,
            )
            print(f"inserted dart_report_metadata partition={partition}, rows={len(chunk):,}")
    finally:
        if owns_client:
            client.close()
    return len(df)


def _iter_report_metadata_partitions(df: pd.DataFrame):
    if df.empty:
        return
    insert_df = df.copy()
    insert_df["_partition"] = pd.to_datetime(insert_df["report_date"], errors="coerce").dt.strftime("%Y%m")
    for partition, chunk in insert_df.groupby("_partition", sort=True, dropna=False):
        chunk = chunk.drop(columns=["_partition"]).copy()
        yield str(partition), chunk[DART_REPORT_METADATA_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Insert filing report metadata into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    inserted = insert_report_metadata(path=args.path, market=args.market, dry_run=args.dry_run)
    action = "prepared" if args.dry_run else "inserted"
    print(f"{action} dart_report_metadata market={args.market} rows={inserted:,}")


def _normalize_market(market: str) -> str:
    return str(market or "kr").strip().lower()


if __name__ == "__main__":
    main()
