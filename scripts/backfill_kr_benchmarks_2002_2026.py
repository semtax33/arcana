from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

import FinanceDataReader as fdr
import numpy as np
import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_dataframe, write_source_text
from engine.loaders.benchmarks import BENCHMARK_TABLE
from engine.transformers.benchmarks import normalize_benchmark_price_frame


START_DATE = "2002-01-01"
BENCHMARK_SYMBOLS = {"KOSPI200": "KS200", "KOSDAQ": "KQ11"}
BRONZE_DIR = DATA_LAKE.bronze("krx", "benchmark_2002_2026")
SILVER_PATH = DATA_LAKE.silver(
    "krx", "benchmark", "kr_normalized_benchmark_price_2002_2026.csv"
)
REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "kr_benchmark_backfill_2002_2026_20260906.json"
)
COMPARE_COLUMNS = ("open", "high", "low", "close", "volume")


def fetch_source(end_date: str) -> pd.DataFrame:
    frames = []
    for benchmark_id, symbol in BENCHMARK_SYMBOLS.items():
        raw = fdr.DataReader(symbol, START_DATE, end_date)
        frame = normalize_benchmark_price_frame(raw, benchmark_id=benchmark_id)
        frames.append(frame)
    source = pd.concat(frames, ignore_index=True).sort_values(
        ["benchmark_id", "trade_date"]
    )
    if source.duplicated(["benchmark_id", "trade_date"]).any():
        raise RuntimeError("benchmark source contains duplicate keys")
    if source["close"].isna().any() or source["close"].le(0).any():
        raise RuntimeError("benchmark source contains an invalid close")
    return source.reset_index(drop=True)


def query_existing(client: Any, end_date: str) -> pd.DataFrame:
    return client.query_df(
        f"""
SELECT
    benchmark_id,
    trade_date,
    toFloat64(open) AS open,
    toFloat64(high) AS high,
    toFloat64(low) AS low,
    toFloat64(close) AS close,
    volume,
    currency,
    country,
    market_mic,
    benchmark_family
FROM {BENCHMARK_TABLE} FINAL
WHERE benchmark_id IN ('KOSPI200', 'KOSDAQ')
  AND trade_date BETWEEN toDate('{START_DATE}') AND toDate('{end_date}')
ORDER BY benchmark_id, trade_date
"""
    )


def missing_source_rows(source: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    source = source.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.date
    keys = existing[["benchmark_id", "trade_date"]].copy()
    keys["trade_date"] = pd.to_datetime(keys["trade_date"]).dt.date
    keys = keys.drop_duplicates()
    merged = source.merge(
        keys.assign(_existing=True),
        on=["benchmark_id", "trade_date"],
        how="left",
    )
    return merged.loc[merged["_existing"].isna()].drop(columns="_existing")


def compare_overlap(source: pd.DataFrame, existing: pd.DataFrame) -> dict[str, Any]:
    source = source.copy()
    existing = existing.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.date
    existing["trade_date"] = pd.to_datetime(existing["trade_date"]).dt.date
    overlap = source.merge(
        existing,
        on=["benchmark_id", "trade_date"],
        suffixes=("_source", "_existing"),
    )
    mismatch_counts = {}
    for column in COMPARE_COLUMNS:
        left = pd.to_numeric(overlap[f"{column}_source"], errors="coerce")
        right = pd.to_numeric(overlap[f"{column}_existing"], errors="coerce")
        mismatch_counts[column] = int(
            (~np.isclose(left, right, rtol=0, atol=1e-6, equal_nan=True)).sum()
        )
    return {"rows": len(overlap), "mismatch_counts": mismatch_counts}


def coverage(frame: pd.DataFrame) -> dict[str, Any]:
    result = {}
    for benchmark_id in BENCHMARK_SYMBOLS:
        rows = frame.loc[frame["benchmark_id"] == benchmark_id]
        result[benchmark_id] = {
            "rows": len(rows),
            "unique_dates": int(rows["trade_date"].nunique()),
            "min_date": str(rows["trade_date"].min()) if not rows.empty else None,
            "max_date": str(rows["trade_date"].max()) if not rows.empty else None,
        }
    return result


def insert_missing(client: Any, missing: pd.DataFrame) -> int:
    if missing.empty:
        return 0
    prepared = missing.copy()
    prepared["trade_date"] = pd.to_datetime(prepared["trade_date"]).dt.date
    prepared["volume"] = pd.Series(
        [None if pd.isna(value) else int(value) for value in prepared["volume"]],
        dtype=object,
    )
    prepared["_partition"] = pd.to_datetime(prepared["trade_date"]).dt.strftime("%Y%m")
    inserted = 0
    for partition, chunk in prepared.groupby("_partition", sort=True):
        chunk = chunk.drop(columns="_partition")
        client.insert_df(BENCHMARK_TABLE, chunk, column_names=list(chunk.columns))
        inserted += len(chunk)
        print(f"[BENCHMARK] partition={partition}, inserted={len(chunk):,}", flush=True)
    return inserted


def run(*, end_date: str, apply: bool) -> dict[str, Any]:
    source = fetch_source(end_date)
    if not apply:
        client = get_clickhouse_client()
        try:
            existing = query_existing(client, end_date)
        finally:
            client.close()
        missing = missing_source_rows(source, existing)
        return {
            "dry_run": True,
            "source_coverage": coverage(source),
            "database_coverage_before": coverage(existing),
            "overlap": compare_overlap(source, existing),
            "missing_rows": len(missing),
        }

    BRONZE_DIR.mkdir(parents=True, exist_ok=True)
    for benchmark_id, rows in source.groupby("benchmark_id", sort=True):
        write_source_dataframe(
            BRONZE_DIR / f"{benchmark_id}.csv",
            rows,
            source="financedatareader-krx-index-cache",
            metadata={"benchmark_id": benchmark_id},
        )
    write_source_dataframe(
        SILVER_PATH,
        source,
        source="financedatareader-krx-index-cache",
        metadata={"benchmark_ids": list(BENCHMARK_SYMBOLS)},
    )

    client = get_clickhouse_client()
    try:
        before = query_existing(client, end_date)
        overlap = compare_overlap(source, before)
        missing = missing_source_rows(source, before)
        inserted = insert_missing(client, missing)
        after = query_existing(client, end_date)
        remaining = missing_source_rows(source, after)
    finally:
        client.close()
    if not remaining.empty:
        raise RuntimeError(f"benchmark backfill incomplete: {len(remaining):,} missing rows")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "FinanceDataReader KRX index cache",
        "stock_data_source": "marcap",
        "period": [START_DATE, end_date],
        "benchmark_ids": list(BENCHMARK_SYMBOLS),
        "source_coverage": coverage(source),
        "database_coverage_before": coverage(before),
        "overlap": overlap,
        "missing_rows_before": len(missing),
        "inserted_rows": inserted,
        "remaining_missing_rows": len(remaining),
        "database_coverage_after": coverage(after),
        "existing_keys_overwritten": 0,
    }
    write_source_text(
        REPORT_PATH,
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        source="arcana-kr-benchmark-backfill-audit",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill KOSPI200 and KOSDAQ benchmark history without overwriting existing keys."
    )
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(end_date=args.end_date, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
