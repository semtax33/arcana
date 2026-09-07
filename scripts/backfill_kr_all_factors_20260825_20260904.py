from __future__ import annotations

"""Fill Korean market data, all daily factors, and PIT snapshots for 2026-08-25..09-04.

The job is intentionally narrow and resumable.  It never deletes rows: ClickHouse
ReplacingMergeTree versions make repeated inserts idempotent under ``FINAL``.

Run from the project root::

    python -m scripts.backfill_kr_all_factors_20260825_20260904 prices
    python -m scripts.backfill_kr_all_factors_20260825_20260904 compute --workers 4
    python -m scripts.backfill_kr_all_factors_20260825_20260904 snapshots
    python -m scripts.backfill_kr_all_factors_20260825_20260904 verify
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import time
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_dataframe, write_source_text
from engine.extractors._internal.marcap_market_prices import (
    ensure_marcap_year_file,
    normalize_marcap_price_frame,
    normalize_marcap_shares_frame,
)
from engine.loaders._internal.clickhouse_factors import (
    insert_factor_catalog,
    prepare_daily_factor_rows,
)
from engine.loaders.factor_snapshots import (
    build_incremental_factor_snapshot_insert_query,
    ensure_factor_snapshot_table,
)
from engine.transformers._internal.factor_metrics import (
    FINANCIAL_DIR,
    REPORT_METADATA_PATH,
    FactorMarketDataCache,
    create_stock_factor_dataframe,
    dividend_path_for_market,
    market_applicable_factor_columns,
    price_path_for_market,
    shares_path_for_market,
)


ROOT = Path(__file__).resolve().parents[1]
START_DATE = "2026-08-25"
END_DATE = "2026-09-04"
BASELINE_DATE = "2026-08-24"
TARGET_DATES = (
    "2026-08-25",
    "2026-08-26",
    "2026-08-27",
    "2026-08-28",
    "2026-08-31",
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
)
MARCAP_DATES = TARGET_DATES[:-1]
FDR_DATE = TARGET_DATES[-1]
FINANCIAL_BASES = ("annual", "quarterly", "ttm")
WARMUP_START_DATE = "2015-08-25"
SHARD_COUNT = max(1, int(os.getenv("ARCANA_KR_FACTOR_SHARDS", "8")))

DATE_TAG = f"{START_DATE.replace('-', '')}-{END_DATE.replace('-', '')}"
WORK = ROOT / ".codex-tmp" / f"kr-all-factors-{DATE_TAG}"
INPUT_DIR = WORK / "inputs"
PRICE_INCREMENT_PATH = WORK / "price_increment.csv"
SHARES_INCREMENT_PATH = WORK / "shares_increment.csv"
COMPUTE_STATUS_PATH = WORK / "compute_status.json"
REPORT_PATH = DATA_LAKE.meta(f"kr_all_factors_{DATE_TAG}_backfill_status.json")
FDR_SOURCE_PATH = DATA_LAKE.bronze(
    "finance-datareader", "krx-listing", f"krx_listing_{FDR_DATE}.csv"
)
SILVER_PRICE_PATH = price_path_for_market("kr")
SILVER_SHARES_PATH = shares_path_for_market("kr")
SILVER_DIVIDEND_PATH = dividend_path_for_market("kr")
FACTOR_IDS = tuple(market_applicable_factor_columns("kr"))
FDR_VALIDATION_CODES = ("005930", "000660", "035420")


def _client():
    return get_clickhouse_client()


def _security_id(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return f"SEC_KR_{text.zfill(6) if text.isdigit() else text.upper()}"


def _stock_code(security_id: str) -> str:
    return str(security_id).removeprefix("SEC_KR_")


def _frame_digest(frame: pd.DataFrame, columns: list[str]) -> str:
    canonical = frame.loc[:, columns].copy()
    canonical["trade_date"] = canonical["trade_date"].astype(str)
    canonical = canonical.sort_values(["security_id", "trade_date"], kind="stable")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_target_frame(frame: pd.DataFrame, *, name: str) -> None:
    if frame.duplicated(["security_id", "trade_date"]).any():
        raise RuntimeError(f"{name} contains duplicate security/date rows")
    actual_dates = tuple(sorted(frame["trade_date"].astype(str).unique()))
    if actual_dates != TARGET_DATES:
        raise RuntimeError(f"{name} dates mismatch: {actual_dates}")
    counts = frame.groupby(frame["trade_date"].astype(str))["security_id"].nunique()
    if int(counts.min()) < 2_700:
        raise RuntimeError(f"{name} cross-section is unexpectedly small: {counts.to_dict()}")


def _load_marcap_source() -> pd.DataFrame:
    path = ensure_marcap_year_file(2026, refresh=False)
    source = pd.read_parquet(path)
    dates = pd.to_datetime(source["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    available = set(dates.dropna())
    if not set(MARCAP_DATES).issubset(available):
        path = ensure_marcap_year_file(2026, refresh=True)
        source = pd.read_parquet(path)
        dates = pd.to_datetime(source["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        available = set(dates.dropna())
    missing = sorted(set(MARCAP_DATES) - available)
    if missing:
        raise RuntimeError(f"marcap source is missing target sessions: {missing}")
    return source


def _validate_fdr_asof(listing: pd.DataFrame) -> None:
    import FinanceDataReader as fdr

    by_code = listing.assign(_code=listing["Code"].map(lambda value: _stock_code(_security_id(value))))
    for code in FDR_VALIDATION_CODES:
        rows = by_code.loc[by_code["_code"] == code]
        if len(rows) != 1:
            raise RuntimeError(f"FDR listing is missing validation code={code}")
        daily = fdr.DataReader(code, FDR_DATE, date.fromisoformat(FDR_DATE).replace(day=5).isoformat())
        if daily.empty:
            raise RuntimeError(f"FDR daily validation returned no row for {code} on {FDR_DATE}")
        listing_row = rows.iloc[0]
        daily_row = daily.iloc[-1]
        for column in ("Open", "High", "Low", "Close"):
            if float(listing_row[column]) != float(daily_row[column]):
                raise RuntimeError(
                    f"FDR listing is not the {FDR_DATE} close: code={code}, column={column}"
                )
        listed_volume = float(listing_row["Volume"])
        daily_volume = float(daily_row["Volume"])
        denominator = max(1.0, abs(daily_volume))
        if abs(listed_volume - daily_volume) / denominator > 0.001:
            raise RuntimeError(f"FDR listing volume validation failed for code={code}")


def _load_fdr_listing() -> pd.DataFrame:
    required = {
        "Code", "Open", "High", "Low", "Close", "Volume", "Marcap", "Stocks"
    }
    if FDR_SOURCE_PATH.exists():
        listing = pd.read_csv(FDR_SOURCE_PATH, dtype={"Code": "string"})
    else:
        import FinanceDataReader as fdr

        listing = fdr.StockListing("KRX")
        missing = sorted(required - set(listing.columns))
        if missing:
            raise RuntimeError(f"FDR KRX listing is missing columns: {missing}")
        _validate_fdr_asof(listing)
        write_source_dataframe(
            FDR_SOURCE_PATH,
            listing,
            source=f"FinanceDataReader StockListing(KRX), validated as of {FDR_DATE}",
        )
    missing = sorted(required - set(listing.columns))
    if missing:
        raise RuntimeError(f"cached FDR KRX listing is missing columns: {missing}")
    return listing


def build_market_increments() -> tuple[pd.DataFrame, pd.DataFrame]:
    WORK.mkdir(parents=True, exist_ok=True)
    if PRICE_INCREMENT_PATH.exists() and SHARES_INCREMENT_PATH.exists():
        prices = pd.read_csv(PRICE_INCREMENT_PATH)
        shares = pd.read_csv(SHARES_INCREMENT_PATH)
        _validate_target_frame(prices, name="cached prices")
        _validate_target_frame(shares, name="cached shares")
        return prices, shares

    marcap = _load_marcap_source()
    raw_prices = normalize_marcap_price_frame(
        marcap, start_date=MARCAP_DATES[0], end_date=MARCAP_DATES[-1]
    )
    prices = pd.DataFrame(
        {
            "security_id": raw_prices["stock_code"].map(_security_id),
            "trade_date": raw_prices["날짜"],
            "open": pd.to_numeric(raw_prices["시가"], errors="coerce"),
            "high": pd.to_numeric(raw_prices["고가"], errors="coerce"),
            "low": pd.to_numeric(raw_prices["저가"], errors="coerce"),
            "close": pd.to_numeric(raw_prices["종가"], errors="coerce"),
            "volume": pd.to_numeric(raw_prices["거래량"], errors="coerce"),
        }
    )
    shares = normalize_marcap_shares_frame(
        marcap, start_date=MARCAP_DATES[0], end_date=MARCAP_DATES[-1]
    )

    listing = _load_fdr_listing().copy()
    listing_ids = listing["Code"].map(_security_id)
    fdr_prices = pd.DataFrame(
        {
            "security_id": listing_ids,
            "trade_date": FDR_DATE,
            "open": pd.to_numeric(listing["Open"], errors="coerce"),
            "high": pd.to_numeric(listing["High"], errors="coerce"),
            "low": pd.to_numeric(listing["Low"], errors="coerce"),
            "close": pd.to_numeric(listing["Close"], errors="coerce"),
            "volume": pd.to_numeric(listing["Volume"], errors="coerce"),
        }
    )
    fdr_shares = pd.DataFrame(
        {
            "security_id": listing_ids,
            "trade_date": FDR_DATE,
            "shares": pd.to_numeric(listing["Stocks"], errors="coerce"),
            "market_cap": pd.to_numeric(listing["Marcap"], errors="coerce"),
        }
    )
    prices = pd.concat([prices, fdr_prices], ignore_index=True)
    prices["adj_close"] = prices["close"]
    prices["currency"] = "KRW"
    prices = prices.loc[prices["close"].notna() & prices["close"].gt(0)].copy()
    prices["volume"] = prices["volume"].round().astype("Int64")
    prices = (
        prices.drop_duplicates(["security_id", "trade_date"], keep="last")
        .sort_values(["security_id", "trade_date"])
        .reset_index(drop=True)
    )

    shares = pd.concat([shares, fdr_shares], ignore_index=True)
    shares = shares.loc[shares["shares"].notna() & shares["shares"].gt(0)].copy()
    shares["shares"] = shares["shares"].round().astype("Int64")
    shares = (
        shares.drop_duplicates(["security_id", "trade_date"], keep="last")
        .sort_values(["security_id", "trade_date"])
        .reset_index(drop=True)
    )
    _validate_target_frame(prices, name="prices")
    _validate_target_frame(shares, name="shares")
    prices.to_csv(PRICE_INCREMENT_PATH, index=False)
    shares.to_csv(SHARES_INCREMENT_PATH, index=False)
    return prices, shares


def _existing_target_pairs(path: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for chunk in pd.read_csv(
        path,
        usecols=["security_id", "trade_date"],
        dtype={"security_id": "string", "trade_date": "string"},
        chunksize=500_000,
    ):
        rows = chunk.loc[chunk["trade_date"].isin(TARGET_DATES)]
        pairs.update(zip(rows["security_id"].astype(str), rows["trade_date"].astype(str)))
    return pairs


def _append_missing_silver(path: Path, frame: pd.DataFrame, columns: list[str]) -> int:
    existing = _existing_target_pairs(path)
    pairs = pd.Series(
        list(zip(frame["security_id"].astype(str), frame["trade_date"].astype(str))),
        index=frame.index,
    )
    missing = frame.loc[~pairs.isin(existing), columns].copy()
    if missing.empty:
        return 0
    missing["trade_date"] = missing["trade_date"].astype(str)
    missing.to_csv(path, mode="a", header=False, index=False, encoding="utf-8")
    return len(missing)


def _load_missing_db_rows(client, table: str, frame: pd.DataFrame) -> pd.DataFrame:
    existing = {
        (str(security_id), str(trade_date))
        for security_id, trade_date in client.query(
            f"""
SELECT security_id, trade_date
FROM {table} FINAL
WHERE startsWith(security_id, 'SEC_KR_')
  AND has({{days:Array(Date)}}, trade_date)
""".strip(),
            parameters={"days": list(TARGET_DATES)},
        ).result_rows
    }
    pairs = pd.Series(
        list(zip(frame["security_id"].astype(str), frame["trade_date"].astype(str))),
        index=frame.index,
    )
    return frame.loc[~pairs.isin(existing)].copy()


def load_prices_and_shares() -> dict[str, object]:
    prices, shares = build_market_increments()
    price_columns = [
        "security_id", "trade_date", "open", "high", "low", "close", "volume",
        "adj_close", "currency",
    ]
    share_columns = ["security_id", "trade_date", "shares", "market_cap"]
    silver_price_rows = _append_missing_silver(SILVER_PRICE_PATH, prices, price_columns)
    silver_share_rows = _append_missing_silver(SILVER_SHARES_PATH, shares, share_columns)

    client = _client()
    try:
        db_prices = _load_missing_db_rows(client, "price_daily", prices)
        if not db_prices.empty:
            db_prices["trade_date"] = pd.to_datetime(db_prices["trade_date"]).dt.date
            db_prices["updated_at"] = datetime.now()
            client.insert_df("price_daily", db_prices, column_names=list(db_prices.columns))
        db_shares = _load_missing_db_rows(client, "stock_shares", shares)
        if not db_shares.empty:
            db_shares["trade_date"] = pd.to_datetime(db_shares["trade_date"]).dt.date
            db_shares["currency"] = "KRW"
            db_shares["updated_at"] = datetime.now()
            client.insert_df("stock_shares", db_shares, column_names=list(db_shares.columns))
    finally:
        client.close()

    _build_input_shards(prices)
    result = {
        "price_rows": len(prices),
        "share_rows": len(shares),
        "db_price_rows_inserted": len(db_prices),
        "db_share_rows_inserted": len(db_shares),
        "silver_price_rows_appended": silver_price_rows,
        "silver_share_rows_appended": silver_share_rows,
        "price_sha256": _frame_digest(prices, price_columns),
        "shares_sha256": _frame_digest(shares, share_columns),
        "date_price_counts": {
            str(day): int(count)
            for day, count in prices.groupby("trade_date")["security_id"].nunique().items()
        },
    }
    print(json.dumps(result, indent=2), flush=True)
    return result


def _shard_for_ids(security_ids: list[str]) -> dict[str, int]:
    return {security_id: index % SHARD_COUNT for index, security_id in enumerate(sorted(security_ids))}


def _reset_shard_paths(prefix: str) -> tuple[list[Path], list[bool]]:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = [INPUT_DIR / f"{prefix}-{index:02d}.csv" for index in range(SHARD_COUNT)]
    for path in paths:
        if path.exists():
            path.unlink()
    return paths, [False] * SHARD_COUNT


def _write_sharded_chunks(
    source_path: Path,
    *,
    prefix: str,
    shard_map: dict[str, int],
    start_date: str | None,
) -> None:
    paths, wrote = _reset_shard_paths(prefix)
    for chunk in pd.read_csv(source_path, chunksize=250_000, low_memory=False):
        chunk = chunk.loc[chunk["security_id"].isin(shard_map)].copy()
        if start_date is not None and "trade_date" in chunk.columns:
            dates = chunk["trade_date"].astype(str)
            chunk = chunk.loc[dates.between(start_date, END_DATE)].copy()
        if chunk.empty:
            continue
        chunk["_shard"] = chunk["security_id"].map(shard_map)
        for shard_index, rows in chunk.groupby("_shard", sort=False):
            shard_index = int(shard_index)
            rows.drop(columns="_shard").to_csv(
                paths[shard_index], mode="a", header=not wrote[shard_index], index=False
            )
            wrote[shard_index] = True
    if not all(wrote):
        raise RuntimeError(f"incomplete {prefix} shards: {wrote}")


def _build_input_shards(prices: pd.DataFrame) -> None:
    security_ids = sorted(prices["security_id"].astype(str).unique())
    shard_map = _shard_for_ids(security_ids)
    _write_sharded_chunks(
        SILVER_PRICE_PATH, prefix="price", shard_map=shard_map, start_date=WARMUP_START_DATE
    )
    _write_sharded_chunks(
        SILVER_SHARES_PATH, prefix="shares", shard_map=shard_map, start_date=None
    )
    _write_sharded_chunks(
        SILVER_DIVIDEND_PATH,
        prefix="dividend",
        shard_map=shard_map,
        start_date=WARMUP_START_DATE,
    )
    manifest = {
        "shards": SHARD_COUNT,
        "securities": len(security_ids),
        "files": {
            prefix: [
                {"path": str(INPUT_DIR / f"{prefix}-{index:02d}.csv"), "bytes": (INPUT_DIR / f"{prefix}-{index:02d}.csv").stat().st_size}
                for index in range(SHARD_COUNT)
            ]
            for prefix in ("price", "shares", "dividend")
        },
    }
    (WORK / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"factor input shards complete: securities={len(security_ids):,}, shards={SHARD_COUNT}", flush=True)


def _validate_compute_inputs() -> None:
    missing = [
        INPUT_DIR / f"{prefix}-{index:02d}.csv"
        for prefix in ("price", "shares", "dividend")
        for index in range(SHARD_COUNT)
        if not (INPUT_DIR / f"{prefix}-{index:02d}.csv").exists()
    ]
    if missing:
        raise RuntimeError(f"factor input shards are missing; run prices first: {missing[:3]}")


def _fully_completed_security_ids(
    expected_date_counts: dict[str, int],
    observed_rows: list[tuple[object, object, object]],
) -> set[str]:
    observed: dict[str, dict[str, int]] = {}
    for security_id, basis, date_count in observed_rows:
        observed.setdefault(str(security_id), {})[str(basis)] = int(date_count)
    return {
        security_id
        for security_id, expected_count in expected_date_counts.items()
        if expected_count > 0
        and all(observed.get(security_id, {}).get(basis) == expected_count for basis in FINANCIAL_BASES)
    }


def _completed_security_ids() -> set[str]:
    prices = pd.read_csv(PRICE_INCREMENT_PATH, usecols=["security_id", "trade_date"])
    expected = {
        str(security_id): int(count)
        for security_id, count in prices.groupby("security_id")["trade_date"].nunique().items()
    }
    client = _client()
    try:
        observed = client.query(
            """
SELECT security_id, financial_basis, uniqExact(trade_date)
FROM fact_daily_factors FINAL
WHERE startsWith(security_id, 'SEC_KR_')
  AND has({days:Array(Date)}, trade_date)
  AND has({bases:Array(String)}, financial_basis)
GROUP BY security_id, financial_basis
""".strip(),
            parameters={"days": list(TARGET_DATES), "bases": list(FINANCIAL_BASES)},
        ).result_rows
    finally:
        client.close()
    return _fully_completed_security_ids(expected, observed)


def _compute_shard(
    shard_index: int,
    completed_security_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    warnings.filterwarnings("ignore")
    cache = FactorMarketDataCache(
        market="kr",
        price_path=INPUT_DIR / f"price-{shard_index:02d}.csv",
        shares_path=INPUT_DIR / f"shares-{shard_index:02d}.csv",
        dividend_path=INPUT_DIR / f"dividend-{shard_index:02d}.csv",
        start_date=START_DATE,
        end_date=END_DATE,
    )
    price_frame, _ = cache._groups("price")
    targets = {pd.Timestamp(value) for value in TARGET_DATES}
    all_stock_codes = sorted(
        price_frame.loc[price_frame["trade_date"].isin(targets), "security_id"]
        .dropna().astype(str).map(_stock_code).unique()
    )
    completed = set(completed_security_ids)
    stock_codes = [code for code in all_stock_codes if f"SEC_KR_{code}" not in completed]
    client = _client()
    inserted = 0
    seen_factors: set[str] = set()
    buffer: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    started = time.monotonic()
    try:
        for index, stock_code in enumerate(stock_codes, start=1):
            try:
                for basis in FINANCIAL_BASES:
                    wide = create_stock_factor_dataframe(
                        stock_code,
                        financial_basis=basis,
                        start_date=START_DATE,
                        end_date=END_DATE,
                        market="kr",
                        market_data_cache=cache,
                        financial_dir=FINANCIAL_DIR,
                        report_metadata_path=REPORT_METADATA_PATH,
                        use_edgartools=False,
                    )
                    if wide.empty:
                        continue
                    target = wide.loc[wide["trade_date"].isin(targets)].copy()
                    long = prepare_daily_factor_rows(
                        target,
                        financial_basis=basis,
                        factor_ids=list(FACTOR_IDS),
                        sort_rows=False,
                    )
                    if not long.empty:
                        seen_factors.update(long["factor_id"].astype(str).unique())
                        buffer.append(long)
            except Exception as exc:
                errors.append(
                    {"stock_code": stock_code, "error": f"{type(exc).__name__}: {exc}"}
                )
            if len(buffer) >= 24 or index == len(stock_codes):
                if buffer:
                    batch = pd.concat(buffer, ignore_index=True)
                    client.insert_df("fact_daily_factors", batch, column_names=list(batch.columns))
                    inserted += len(batch)
                    buffer = []
            if index == 1 or index % 10 == 0 or index == len(stock_codes):
                print(
                    f"shard={shard_index}, stocks={index}/{len(stock_codes)}, "
                    f"rows={inserted:,}, errors={len(errors)}, "
                    f"elapsed={(time.monotonic() - started) / 60:.1f}m",
                    flush=True,
                )
    finally:
        client.close()
    return {
        "shard": shard_index,
        "stocks": len(stock_codes),
        "skipped_complete": len(all_stock_codes) - len(stock_codes),
        "rows": inserted,
        "factor_count": len(seen_factors),
        "errors": errors,
        "minutes": round((time.monotonic() - started) / 60, 2),
    }


def compute(*, workers: int) -> list[dict[str, object]]:
    _validate_compute_inputs()
    completed = _completed_security_ids()
    print(f"resume: already complete securities={len(completed):,}", flush=True)
    client = _client()
    try:
        insert_factor_catalog(client, factor_ids=list(FACTOR_IDS))
    finally:
        client.close()
    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=min(max(1, workers), SHARD_COUNT)) as executor:
        completed_tuple = tuple(sorted(completed))
        futures = {
            executor.submit(_compute_shard, index, completed_tuple): index
            for index in range(SHARD_COUNT)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    results.sort(key=lambda item: int(item["shard"]))
    COMPUTE_STATUS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    errors = [error for result in results for error in result["errors"]]
    if errors:
        raise RuntimeError(f"factor computation failed for {len(errors)} stocks; see {COMPUTE_STATUS_PATH}")
    return results


def _active_snapshot_factor_ids(client, financial_basis: str) -> list[str]:
    rows = client.query(
        """
SELECT factor_id
FROM fact_daily_factor_snapshot FINAL
PREWHERE trade_date = {baseline:Date}
WHERE startsWith(security_id, 'SEC_KR_')
  AND financial_basis = {basis:String}
  AND isFinite(factor_value)
GROUP BY factor_id
ORDER BY factor_id
""".strip(),
        parameters={
            "baseline": BASELINE_DATE,
            "basis": financial_basis,
        },
    ).result_rows
    return [str(row[0]) for row in rows]


def load_snapshots() -> dict[str, int]:
    result: dict[str, int] = {}
    client = _client()
    try:
        ensure_factor_snapshot_table(client)
        for basis in FINANCIAL_BASES:
            active_factor_ids = _active_snapshot_factor_ids(client, basis)
            chunks = [active_factor_ids[index : index + 64] for index in range(0, len(active_factor_ids), 64)]
            previous_date = BASELINE_DATE
            batch_index = 0
            total_batches = len(TARGET_DATES) * len(chunks)
            for target_date in TARGET_DATES:
                for factor_chunk in chunks:
                    batch_index += 1
                    query, parameters = build_incremental_factor_snapshot_insert_query(
                        market="kr",
                        snapshot_date=target_date,
                        previous_snapshot_date=previous_date,
                        financial_basis=basis,
                        factor_ids=factor_chunk,
                    )
                    client.command(query, parameters=parameters, settings={"max_threads": 4})
                    print(
                        f"snapshot basis={basis}, batch={batch_index}/{total_batches}, "
                        f"date={target_date}, factors={len(factor_chunk)}",
                        flush=True,
                    )
                previous_date = target_date
            result[basis] = int(
                client.query(
                    """
SELECT count()
FROM fact_daily_factor_snapshot FINAL
PREWHERE trade_date BETWEEN {start:Date} AND {end:Date}
WHERE startsWith(security_id, 'SEC_KR_')
  AND financial_basis = {basis:String}
""".strip(),
                    parameters={"start": START_DATE, "end": END_DATE, "basis": basis},
                ).first_row[0]
            )
    finally:
        client.close()
    print(json.dumps(result, indent=2), flush=True)
    return result


def _factor_sets(client, table: str, day: str) -> dict[str, set[str]]:
    result = {basis: set() for basis in FINANCIAL_BASES}
    rows = client.query(
        f"""
SELECT financial_basis, factor_id
FROM {table} FINAL
WHERE startsWith(security_id, 'SEC_KR_')
  AND trade_date = {{day:Date}}
  AND has({{bases:Array(String)}}, financial_basis)
  AND isFinite(factor_value)
GROUP BY financial_basis, factor_id
""".strip(),
        parameters={"day": day, "bases": list(FINANCIAL_BASES)},
    ).result_rows
    for basis, factor_id in rows:
        result[str(basis)].add(str(factor_id))
    return result


def verify() -> dict[str, object]:
    prices, shares = build_market_increments()
    expected_price_counts = {
        str(day): int(count)
        for day, count in prices.groupby("trade_date")["security_id"].nunique().items()
    }
    expected_share_counts = {
        str(day): int(count)
        for day, count in shares.groupby("trade_date")["security_id"].nunique().items()
    }
    client = _client()
    try:
        price_counts = {
            str(day): int(count)
            for day, count in client.query(
                """
SELECT trade_date, uniqExact(security_id)
FROM price_daily FINAL
WHERE startsWith(security_id, 'SEC_KR_') AND has({days:Array(Date)}, trade_date)
GROUP BY trade_date ORDER BY trade_date
""".strip(),
                parameters={"days": list(TARGET_DATES)},
            ).result_rows
        }
        share_counts = {
            str(day): int(count)
            for day, count in client.query(
                """
SELECT trade_date, uniqExact(security_id)
FROM stock_shares FINAL
WHERE startsWith(security_id, 'SEC_KR_') AND has({days:Array(Date)}, trade_date)
GROUP BY trade_date ORDER BY trade_date
""".strip(),
                parameters={"days": list(TARGET_DATES)},
            ).result_rows
        }
        if price_counts != expected_price_counts:
            raise RuntimeError(f"price coverage mismatch: db={price_counts}, expected={expected_price_counts}")
        if share_counts != expected_share_counts:
            raise RuntimeError(f"share coverage mismatch: db={share_counts}, expected={expected_share_counts}")

        baseline_factors = _factor_sets(client, "fact_daily_factors", BASELINE_DATE)
        baseline_snapshots = _factor_sets(client, "fact_daily_factor_snapshot", BASELINE_DATE)
        factor_summary: dict[str, object] = {}
        snapshot_summary: dict[str, object] = {}
        for day in TARGET_DATES:
            day_factors = _factor_sets(client, "fact_daily_factors", day)
            day_snapshots = _factor_sets(client, "fact_daily_factor_snapshot", day)
            for basis in FINANCIAL_BASES:
                factor_missing = sorted(baseline_factors[basis] - day_factors[basis])
                snapshot_missing = sorted(baseline_snapshots[basis] - day_snapshots[basis])
                if factor_missing:
                    raise RuntimeError(f"factor ids missing on {day}/{basis}: {factor_missing}")
                if snapshot_missing:
                    raise RuntimeError(f"snapshot ids missing on {day}/{basis}: {snapshot_missing}")
                factor_summary[f"{day}|{basis}"] = len(day_factors[basis])
                snapshot_summary[f"{day}|{basis}"] = len(day_snapshots[basis])

        invalid_snapshots = int(client.query(
            """
SELECT count()
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_KR_')
  AND has({days:Array(Date)}, trade_date)
  AND (source_trade_date > trade_date OR NOT isFinite(factor_value))
""".strip(),
            parameters={"days": list(TARGET_DATES)},
        ).first_row[0])
        if invalid_snapshots:
            raise RuntimeError(f"invalid/future-dated snapshots: {invalid_snapshots}")

        missing_current = int(client.query(
            """
WITH
f AS (
    SELECT security_id, trade_date, factor_id, financial_basis,
           argMax(factor_value, updated_at) AS current_factor_value
    FROM fact_daily_factors
    WHERE startsWith(security_id, 'SEC_KR_')
      AND has({days:Array(Date)}, trade_date)
      AND has({bases:Array(String)}, financial_basis)
      AND isFinite(factor_value)
    GROUP BY security_id, trade_date, factor_id, financial_basis
),
s AS (
    SELECT security_id, trade_date, factor_id, financial_basis,
           argMax(source_trade_date, updated_at) AS snapshot_source_trade_date,
           argMax(factor_value, updated_at) AS snapshot_factor_value
    FROM fact_daily_factor_snapshot
    WHERE startsWith(security_id, 'SEC_KR_')
      AND has({days:Array(Date)}, trade_date)
      AND has({bases:Array(String)}, financial_basis)
    GROUP BY security_id, trade_date, factor_id, financial_basis
)
SELECT count()
FROM f LEFT JOIN s USING (security_id, trade_date, factor_id, financial_basis)
WHERE s.security_id = '' OR s.snapshot_source_trade_date != f.trade_date
   OR abs(s.snapshot_factor_value - f.current_factor_value) > 1e-10
""".strip(),
            parameters={"days": list(TARGET_DATES), "bases": list(FINANCIAL_BASES)},
            settings={"max_threads": 4},
        ).first_row[0])
        if missing_current:
            raise RuntimeError(f"current daily factors absent/mismatched in snapshots: {missing_current}")

        row_summary = client.query(
            """
SELECT 'factors' AS kind, trade_date, financial_basis, count(), uniqExact(security_id), uniqExact(factor_id)
FROM fact_daily_factors FINAL
WHERE startsWith(security_id, 'SEC_KR_') AND has({days:Array(Date)}, trade_date)
GROUP BY trade_date, financial_basis
UNION ALL
SELECT 'snapshots' AS kind, trade_date, financial_basis, count(), uniqExact(security_id), uniqExact(factor_id)
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_KR_') AND has({days:Array(Date)}, trade_date)
GROUP BY trade_date, financial_basis
ORDER BY kind, trade_date, financial_basis
""".strip(),
            parameters={"days": list(TARGET_DATES)},
        ).result_rows
    finally:
        client.close()

    report = {
        "status": "complete",
        "market": "kr",
        "range": [START_DATE, END_DATE],
        "target_dates": list(TARGET_DATES),
        "price_security_counts": price_counts,
        "share_security_counts": share_counts,
        "daily_factor_id_counts": factor_summary,
        "snapshot_factor_id_counts": snapshot_summary,
        "invalid_or_future_snapshots": invalid_snapshots,
        "current_factor_snapshot_mismatches": missing_current,
        "rows": [
            {
                "kind": str(kind), "trade_date": str(day), "financial_basis": str(basis),
                "rows": int(rows), "securities": int(securities), "factors": int(factors),
            }
            for kind, day, basis, rows, securities, factors in row_summary
        ],
        "verified_at": datetime.now().isoformat(),
    }
    write_source_text(
        REPORT_PATH,
        json.dumps(report, ensure_ascii=False, indent=2),
        source="Arcana KR all-factor backfill verification",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prices")
    compute_parser = subparsers.add_parser("compute")
    compute_parser.add_argument("--workers", type=int, default=4)
    subparsers.add_parser("snapshots")
    subparsers.add_parser("verify")
    args = parser.parse_args()
    if args.command == "prices":
        load_prices_and_shares()
    elif args.command == "compute":
        print(json.dumps(compute(workers=args.workers), indent=2), flush=True)
    elif args.command == "snapshots":
        load_snapshots()
    else:
        verify()


if __name__ == "__main__":
    main()
