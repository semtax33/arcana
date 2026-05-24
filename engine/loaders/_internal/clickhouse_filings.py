from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.filing_periods import REPORT_METADATA_PATH


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


def read_report_metadata(path: str | Path = REPORT_METADATA_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=DART_REPORT_METADATA_COLUMNS)

    df = pd.read_csv(path, dtype={"stock_code": str, "rcept_no": str})
    df["country"] = df["country"] if "country" in df.columns else "KR"
    df["market_mic"] = df["market_mic"] if "market_mic" in df.columns else ""
    df["filing_system"] = df["filing_system"] if "filing_system" in df.columns else "DART"
    for column in DART_REPORT_METADATA_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[DART_REPORT_METADATA_COLUMNS].copy()
    df["period_end_date"] = pd.to_datetime(df["period_end_date"], errors="coerce").dt.date
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").dt.date
    df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df


def insert_report_metadata(path: str | Path = REPORT_METADATA_PATH, client=None) -> int:
    df = read_report_metadata(path)
    if df.empty:
        return 0

    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        client.insert_df(
            "dart_report_metadata",
            df,
            column_names=list(df.columns),
        )
    finally:
        if owns_client:
            client.close()
    return len(df)


def main() -> None:
    inserted = insert_report_metadata()
    print(f"inserted dart_report_metadata rows={inserted:,}")


if __name__ == "__main__":
    main()
