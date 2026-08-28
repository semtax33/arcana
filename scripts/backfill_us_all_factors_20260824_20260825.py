from __future__ import annotations

"""Backfill every applicable US Annual/TTM factor for 2026-08-24..25.

The job downloads the missing two US price sessions in Yahoo batches, updates
``price_daily`` and the normalized US price file, builds memory-safe price
shards, calculates all standard factors for both financial bases, and loads
matching PIT snapshots.

Run from the project root::

    python -m scripts.backfill_us_all_factors_20260824_20260825 prices
    python -m scripts.backfill_us_all_factors_20260824_20260825 compute --workers 16
    python -m scripts.backfill_us_all_factors_20260824_20260825 snapshots
    python -m scripts.backfill_us_all_factors_20260824_20260825 verify
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import time
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.extractors.market_prices import normalize_yfinance_ticker
from engine.loaders._internal.clickhouse_factors import prepare_daily_factor_rows
from engine.transformers._internal.factor_metrics import (
    FactorMarketDataCache,
    create_stock_factor_dataframe,
    preferred_factor_columns,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET_DATES = tuple(
    value.strip()
    for value in os.getenv(
        "ARCANA_US_FACTOR_BACKFILL_DATES",
        "2026-08-24,2026-08-25",
    ).split(",")
    if value.strip()
)
if len(TARGET_DATES) != 2:
    raise RuntimeError("ARCANA_US_FACTOR_BACKFILL_DATES must contain exactly two dates")
for target_date in TARGET_DATES:
    date.fromisoformat(target_date)
BASELINE_DATE = os.getenv("ARCANA_US_FACTOR_BASELINE_DATE", "2026-08-21")
WARMUP_START_DATE = os.getenv("ARCANA_US_FACTOR_WARMUP_START_DATE", "2015-08-24")
YAHOO_END_DATE = (date.fromisoformat(TARGET_DATES[-1]) + timedelta(days=1)).isoformat()
DATE_TAG = "-".join(value.replace("-", "") for value in TARGET_DATES)
WORK = ROOT / ".codex-tmp" / f"us-all-factors-{DATE_TAG}"
INPUT_DIR = WORK / "inputs"
PRICE_INCREMENT_PATH = WORK / "price_increment.csv"
SILVER_APPEND_MARKER = WORK / "silver_price_appended.done"
SILVER_PRICE_PATH = DATA_LAKE.silver("us", "price", "us_normalized_price.csv")
SILVER_SHARES_PATH = DATA_LAKE.silver("us", "shares", "us_normalized_shares.csv")
SILVER_DIVIDEND_PATH = DATA_LAKE.silver("us", "dividend", "us_dividend_normalized.csv")
FINANCIAL_DIR = DATA_LAKE.silver("sec", "normalized")
REPORT_PATH = DATA_LAKE.silver("sec", "us_report_metadata.csv")
SHARD_COUNT = 8
FINANCIAL_BASES = ("annual", "ttm")
CONSENSUS_AND_TARGET_FACTOR_IDS = (
    "us_consensus_analyst_count",
    "us_eps_consensus",
    "us_revenue_consensus",
    "us_eps_revision_7d_pct",
    "us_eps_revision_30d_pct",
    "us_eps_revision_60d_pct",
    "us_eps_revision_90d_pct",
    "us_eps_revision_breadth_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_dispersion_pct",
    "us_revenue_dispersion_pct",
    "us_eps_surprise_pct",
    "us_price_to_target_price",
)


def _client():
    return get_clickhouse_client()


def _baseline_securities() -> list[str]:
    client = _client()
    try:
        return [
            str(row[0])
            for row in client.query(
                """
SELECT DISTINCT security_id
FROM price_daily FINAL
PREWHERE trade_date BETWEEN {day:Date} - INTERVAL 7 DAY AND {day:Date}
WHERE startsWith(security_id, 'SEC_US_')
ORDER BY security_id
""".strip(),
                parameters={"day": BASELINE_DATE},
            ).result_rows
        ]
    finally:
        client.close()


def _download_yahoo_batch(
    yahoo_to_security: dict[str, str],
    *,
    batch_size: int,
) -> pd.DataFrame:
    import yfinance as yf

    yahoo_tickers = sorted(yahoo_to_security)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    for offset in range(0, len(yahoo_tickers), batch_size):
        batch = yahoo_tickers[offset : offset + batch_size]
        data = yf.download(
            batch,
            start=TARGET_DATES[0],
            end=YAHOO_END_DATE,
            group_by="ticker",
            auto_adjust=False,
            actions=True,
            threads=True,
            progress=False,
            timeout=30,
            repair=False,
        )
        available = (
            set(map(str, data.columns.get_level_values(0)))
            if isinstance(data.columns, pd.MultiIndex)
            else set(batch if len(batch) == 1 else [])
        )
        for ticker in batch:
            if ticker not in available:
                failed.append(ticker)
                continue
            frame = data[ticker].copy() if isinstance(data.columns, pd.MultiIndex) else data.copy()
            frame = frame.dropna(how="all").reset_index()
            if frame.empty or "Close" not in frame.columns:
                failed.append(ticker)
                continue
            date_column = "Date" if "Date" in frame.columns else frame.columns[0]
            normalized = pd.DataFrame(
                {
                    "security_id": yahoo_to_security[ticker],
                    "trade_date": pd.to_datetime(frame[date_column], errors="coerce"),
                    "open": pd.to_numeric(frame.get("Open"), errors="coerce"),
                    "high": pd.to_numeric(frame.get("High"), errors="coerce"),
                    "low": pd.to_numeric(frame.get("Low"), errors="coerce"),
                    "close": pd.to_numeric(frame.get("Close"), errors="coerce"),
                    "volume": pd.to_numeric(frame.get("Volume"), errors="coerce"),
                    "adj_close": pd.to_numeric(frame.get("Adj Close"), errors="coerce"),
                    "currency": "USD",
                }
            )
            normalized = normalized.loc[
                normalized["trade_date"].dt.strftime("%Y-%m-%d").isin(TARGET_DATES)
                & normalized["close"].notna()
            ]
            if normalized.empty:
                failed.append(ticker)
                continue
            frames.append(normalized)
        print(
            f"price batches={min(offset + batch_size, len(yahoo_tickers)):,}/"
            f"{len(yahoo_tickers):,}, rows={sum(len(frame) for frame in frames):,}, "
            f"no_rows={len(failed):,}",
            flush=True,
        )
    if not frames:
        raise RuntimeError("Yahoo returned no US prices for the target dates")
    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(["security_id", "trade_date"], keep="last")
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.date
    result["volume"] = pd.to_numeric(result["volume"], errors="coerce").round().astype("Int64")
    return result.sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def download_and_load_prices(
    *,
    batch_size: int = 100,
    retry_missing: bool = False,
) -> pd.DataFrame:
    WORK.mkdir(parents=True, exist_ok=True)
    securities = _baseline_securities()
    yahoo_to_security: dict[str, str] = {}
    collisions = []
    for security_id in securities:
        symbol = security_id.removeprefix("SEC_US_")
        yahoo_ticker = normalize_yfinance_ticker(symbol)
        if yahoo_ticker in yahoo_to_security and yahoo_to_security[yahoo_ticker] != security_id:
            collisions.append((yahoo_ticker, yahoo_to_security[yahoo_ticker], security_id))
        yahoo_to_security[yahoo_ticker] = security_id
    if collisions:
        raise RuntimeError(f"Yahoo ticker collisions: {collisions}")

    if PRICE_INCREMENT_PATH.exists():
        prices = pd.read_csv(PRICE_INCREMENT_PATH)
        prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce").dt.date
        prices["volume"] = pd.to_numeric(prices["volume"], errors="coerce").astype("Int64")
        print(f"reusing price increment rows={len(prices):,}", flush=True)
        new_prices = prices.iloc[0:0].copy()
        if retry_missing:
            existing_pairs = set(
                zip(
                    prices["security_id"].astype(str),
                    prices["trade_date"].astype(str),
                )
            )
            missing_security_ids = {
                security_id
                for security_id in securities
                if any(
                    (security_id, target_date) not in existing_pairs
                    for target_date in TARGET_DATES
                )
            }
            retry_map = {
                yahoo_ticker: security_id
                for yahoo_ticker, security_id in yahoo_to_security.items()
                if security_id in missing_security_ids
            }
            if retry_map:
                retried = _download_yahoo_batch(retry_map, batch_size=batch_size)
                retried_pairs = pd.Series(
                    list(zip(retried["security_id"].astype(str), retried["trade_date"].astype(str))),
                    index=retried.index,
                )
                new_prices = retried.loc[~retried_pairs.isin(existing_pairs)].copy()
                if not new_prices.empty:
                    prices = (
                        pd.concat([prices, new_prices], ignore_index=True)
                        .drop_duplicates(["security_id", "trade_date"], keep="last")
                        .sort_values(["security_id", "trade_date"])
                        .reset_index(drop=True)
                    )
                    prices.to_csv(PRICE_INCREMENT_PATH, index=False)
                print(
                    f"price retry securities={len(retry_map):,}, new_rows={len(new_prices):,}",
                    flush=True,
                )
    else:
        prices = _download_yahoo_batch(yahoo_to_security, batch_size=batch_size)
        prices.to_csv(PRICE_INCREMENT_PATH, index=False)
        new_prices = prices.copy()

    if not new_prices.empty:
        client = _client()
        try:
            db_rows = new_prices.copy()
            db_rows["updated_at"] = datetime.now()
            client.insert_df(
                "price_daily",
                db_rows,
                column_names=list(db_rows.columns),
            )
        finally:
            client.close()

    silver_append = prices if not SILVER_APPEND_MARKER.exists() else new_prices
    if not silver_append.empty:
        silver_append.to_csv(SILVER_PRICE_PATH, mode="a", header=False, index=False)
        SILVER_APPEND_MARKER.write_text(str(len(prices)), encoding="utf-8")
        print(f"Silver price rows appended={len(silver_append):,}", flush=True)

    _build_price_shards(prices)
    return prices


def _build_price_shards(prices: pd.DataFrame) -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    security_ids = sorted(prices["security_id"].astype(str).unique())
    shard_map = {
        security_id: index % SHARD_COUNT
        for index, security_id in enumerate(security_ids)
    }
    paths = [INPUT_DIR / f"price-{index:02d}.csv" for index in range(SHARD_COUNT)]
    for path in paths:
        if path.exists():
            path.unlink()
    wrote = [False] * SHARD_COUNT
    usecols = [
        "security_id",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
        "currency",
    ]
    for chunk in pd.read_csv(SILVER_PRICE_PATH, usecols=usecols, chunksize=250_000, low_memory=False):
        dates = chunk["trade_date"].astype(str)
        chunk = chunk.loc[
            dates.between(WARMUP_START_DATE, TARGET_DATES[-1])
            & chunk["security_id"].isin(shard_map)
        ].copy()
        if chunk.empty:
            continue
        chunk["_shard"] = chunk["security_id"].map(shard_map)
        for shard_index, rows in chunk.groupby("_shard", sort=False):
            shard_index = int(shard_index)
            rows.drop(columns="_shard").to_csv(
                paths[shard_index],
                mode="a",
                header=not wrote[shard_index],
                index=False,
            )
            wrote[shard_index] = True
    if not all(wrote):
        raise RuntimeError(f"incomplete price shards: {wrote}")
    print(
        "price shards complete "
        + ", ".join(f"{path.name}={path.stat().st_size:,}" for path in paths),
        flush=True,
    )


def _validate_compute_inputs() -> None:
    required = [SILVER_SHARES_PATH, SILVER_DIVIDEND_PATH, FINANCIAL_DIR, REPORT_PATH]
    required.extend(INPUT_DIR / f"price-{index:02d}.csv" for index in range(SHARD_COUNT))
    missing = [path for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"compute inputs are missing: {missing}; run prices first")


def _compute_partition(
    shard_index: int,
    part_index: int,
    part_count: int,
    factor_ids: tuple[str, ...],
) -> dict[str, int | float]:
    warnings.filterwarnings("ignore")
    cache = FactorMarketDataCache(
        market="us",
        price_path=INPUT_DIR / f"price-{shard_index:02d}.csv",
        shares_path=SILVER_SHARES_PATH,
        dividend_path=SILVER_DIVIDEND_PATH,
        start_date=TARGET_DATES[0],
        end_date=TARGET_DATES[-1],
    )
    price_frame, _ = cache._groups("price")
    targets = {pd.Timestamp(value) for value in TARGET_DATES}
    stock_codes = sorted(
        price_frame.loc[price_frame["trade_date"].isin(targets), "security_id"]
        .dropna()
        .astype(str)
        .str.removeprefix("SEC_US_")
        .unique()
    )[part_index::part_count]
    client = _client()
    inserted = 0
    buffer: list[pd.DataFrame] = []
    started = time.monotonic()
    try:
        for index, stock_code in enumerate(stock_codes, start=1):
            for basis in FINANCIAL_BASES:
                wide = create_stock_factor_dataframe(
                    stock_code,
                    financial_basis=basis,
                    start_date=TARGET_DATES[0],
                    end_date=TARGET_DATES[-1],
                    market="us",
                    market_data_cache=cache,
                    financial_dir=FINANCIAL_DIR,
                    report_metadata_path=REPORT_PATH,
                    use_edgartools=False,
                )
                if wide.empty:
                    continue
                target = wide.loc[wide["trade_date"].isin(targets)].copy()
                if target.empty:
                    continue
                long = prepare_daily_factor_rows(
                    target,
                    financial_basis=basis,
                    factor_ids=list(factor_ids),
                    sort_rows=False,
                )
                if not long.empty:
                    buffer.append(long)
            if len(buffer) >= 50 or index == len(stock_codes):
                if buffer:
                    batch = pd.concat(buffer, ignore_index=True)
                    client.insert_df(
                        "fact_daily_factors",
                        batch,
                        column_names=list(batch.columns),
                    )
                    inserted += len(batch)
                    buffer = []
            if index == 1 or index % 50 == 0 or index == len(stock_codes):
                print(
                    f"shard={shard_index}, part={part_index}/{part_count}, "
                    f"stocks={index}/{len(stock_codes)}, rows={inserted:,}, "
                    f"elapsed={(time.monotonic() - started) / 60:.1f}m",
                    flush=True,
                )
    finally:
        client.close()
    return {
        "shard": shard_index,
        "part": part_index,
        "stocks": len(stock_codes),
        "rows": inserted,
        "minutes": round((time.monotonic() - started) / 60, 2),
    }


def compute(*, workers: int, parts_per_shard: int) -> list[dict[str, int | float]]:
    _validate_compute_inputs()
    factor_ids = tuple(preferred_factor_columns())
    tasks = [
        (shard_index, part_index, parts_per_shard, factor_ids)
        for shard_index in range(SHARD_COUNT)
        for part_index in range(parts_per_shard)
    ]
    results = []
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
        futures = {executor.submit(_compute_partition, *task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    return sorted(results, key=lambda item: (item["shard"], item["part"]))


def load_snapshots() -> None:
    client = _client()
    try:
        client.command(
            """
INSERT INTO fact_daily_factor_snapshot
    (trade_date, security_id, factor_id, financial_basis, factor_value,
     source_trade_date, fiscal_year, financial_period, currency, updated_at)
SELECT trade_date, security_id, factor_id, financial_basis, factor_value,
       trade_date, fiscal_year, financial_period, currency, now64(3)
FROM fact_daily_factors FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND has({bases:Array(String)}, financial_basis)
  AND factor_value IS NOT NULL
SETTINGS max_threads = 12
""".strip(),
            parameters={"days": list(TARGET_DATES), "bases": list(FINANCIAL_BASES)},
        )
    finally:
        client.close()
    print("Annual/TTM PIT snapshot copy complete", flush=True)


def verify() -> dict[str, object]:
    client = _client()
    try:
        expected = {
            basis: {
                str(row[0])
                for row in client.query(
                    """
SELECT DISTINCT factor_id
FROM fact_daily_factors FINAL
PREWHERE trade_date = {day:Date}
WHERE startsWith(security_id, 'SEC_US_')
  AND financial_basis = {basis:String}
  AND factor_value IS NOT NULL
""".strip(),
                    parameters={"day": BASELINE_DATE, "basis": basis},
                ).result_rows
            }
            for basis in FINANCIAL_BASES
        }
        rows = client.query(
            """
SELECT trade_date, financial_basis, factor_id,
       uniqExact(security_id) AS security_count
FROM fact_daily_factors FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND has({bases:Array(String)}, financial_basis)
  AND factor_value IS NOT NULL
GROUP BY trade_date, financial_basis, factor_id
ORDER BY trade_date, financial_basis, factor_id
""".strip(),
            parameters={"days": list(TARGET_DATES), "bases": list(FINANCIAL_BASES)},
        ).result_rows
        price_rows = client.query(
            """
SELECT trade_date, uniqExact(security_id)
FROM price_daily FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
GROUP BY trade_date
ORDER BY trade_date
""".strip(),
            parameters={"days": list(TARGET_DATES)},
        ).result_rows
        snapshot_rows = client.query(
            """
SELECT trade_date, financial_basis, uniqExact(factor_id), uniqExact(security_id)
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND has({bases:Array(String)}, financial_basis)
  AND factor_value IS NOT NULL
  AND source_trade_date <= trade_date
GROUP BY trade_date, financial_basis
ORDER BY trade_date, financial_basis
""".strip(),
            parameters={"days": list(TARGET_DATES), "bases": list(FINANCIAL_BASES)},
        ).result_rows
    finally:
        client.close()

    present: dict[tuple[str, str], set[str]] = {}
    breadth: dict[tuple[str, str, str], int] = {}
    for day, basis, factor_id, security_count in rows:
        key = (str(day), str(basis))
        present.setdefault(key, set()).add(str(factor_id))
        breadth[(str(day), str(basis), str(factor_id))] = int(security_count)
    missing = {
        f"{day}|{basis}": sorted(expected[basis] - present.get((day, basis), set()))
        for day in TARGET_DATES
        for basis in FINANCIAL_BASES
        if expected[basis] - present.get((day, basis), set())
    }
    if missing:
        raise RuntimeError(f"US factor gaps remain: {missing}")
    consensus = {
        f"{day}|{basis}": {
            factor_id: breadth.get((day, basis, factor_id), 0)
            for factor_id in CONSENSUS_AND_TARGET_FACTOR_IDS
        }
        for day in TARGET_DATES
        for basis in FINANCIAL_BASES
    }
    missing_consensus = {
        key: [factor_id for factor_id, count in values.items() if count <= 0]
        for key, values in consensus.items()
        if any(count <= 0 for count in values.values())
    }
    if missing_consensus:
        raise RuntimeError(f"consensus/target-price factor gaps remain: {missing_consensus}")
    summary = {
        "price_security_counts": [(str(day), int(count)) for day, count in price_rows],
        "factor_counts": {
            f"{day}|{basis}": len(present[(day, basis)])
            for day in TARGET_DATES
            for basis in FINANCIAL_BASES
        },
        "factor_security_counts": {
            f"{day}|{basis}": max(
                breadth[(day, basis, factor_id)] for factor_id in present[(day, basis)]
            )
            for day in TARGET_DATES
            for basis in FINANCIAL_BASES
        },
        "consensus_and_target_price": consensus,
        "snapshot_counts": [
            (str(day), str(basis), int(factors), int(securities))
            for day, basis, factors, securities in snapshot_rows
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prices_parser = subparsers.add_parser("prices")
    prices_parser.add_argument("--batch-size", type=int, default=100)
    prices_parser.add_argument("--retry-missing", action="store_true")
    compute_parser = subparsers.add_parser("compute")
    compute_parser.add_argument("--workers", type=int, default=16)
    compute_parser.add_argument("--parts-per-shard", type=int, default=2)
    subparsers.add_parser("snapshots")
    subparsers.add_parser("verify")
    args = parser.parse_args()

    if args.command == "prices":
        prices = download_and_load_prices(
            batch_size=max(1, args.batch_size),
            retry_missing=args.retry_missing,
        )
        print(
            json.dumps(
                {
                    "rows": len(prices),
                    "dates": {
                        str(day): int(count)
                        for day, count in prices.groupby("trade_date")["security_id"].nunique().items()
                    },
                },
                default=str,
                indent=2,
            )
        )
    elif args.command == "compute":
        print(
            json.dumps(
                compute(
                    workers=max(1, args.workers),
                    parts_per_shard=max(1, args.parts_per_shard),
                ),
                indent=2,
            )
        )
    elif args.command == "snapshots":
        load_snapshots()
    else:
        verify()


if __name__ == "__main__":
    main()
