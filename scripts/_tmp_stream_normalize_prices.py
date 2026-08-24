from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import pandas as pd

from engine.core.paths import DATA_LAKE, market_csv_name
from engine.core.source_storage import replace_file_with_permission_retry
from engine.extractors._internal.yfinance_market_prices import (
    yfinance_price_ticker_from_storage_stem,
)
from engine.transformers._internal.krx_market_data import (
    CLOSE_COLUMN,
    DATE_COLUMN,
    HIGH_COLUMN,
    LOW_COLUMN,
    OPEN_COLUMN,
    VOLUME_COLUMN,
)
from engine.transformers._internal.yfinance_market_data import (
    normalize_yfinance_price_frame,
)


START_DATE = pd.Timestamp("2026-08-01")
KR_END_DATE = pd.Timestamp("2026-08-24")
US_END_DATE = pd.Timestamp("2026-08-23")


def _append_csv(frame: pd.DataFrame, path: Path, *, header: bool) -> None:
    frame.to_csv(
        path,
        mode="a",
        header=header,
        index=False,
        encoding="utf-8-sig" if header else "utf-8",
    )


def _record_coverage(
    coverage: dict[str, int],
    frame: pd.DataFrame,
    *,
    end_date: pd.Timestamp,
) -> None:
    if frame.empty:
        return
    recent = frame.loc[
        frame["trade_date"].between(START_DATE, end_date),
        ["trade_date", "security_id"],
    ]
    if recent.empty:
        return
    counts = recent.groupby(recent["trade_date"].dt.strftime("%Y-%m-%d"))["security_id"].nunique()
    for trade_date, count in counts.items():
        coverage[trade_date] = coverage.get(trade_date, 0) + int(count)


def stream_kr() -> None:
    files = sorted(DATA_LAKE.bronze("marcap", "data").glob("marcap-*.parquet"))
    output = DATA_LAKE.silver(
        "krx",
        "price",
        market_csv_name("normalized_price"),
    )
    staged = output.with_name(".kr_normalized_price.stream.csv.tmp")
    staged.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    header = True
    total_rows = 0
    coverage: dict[str, int] = {}
    for file_index, path in enumerate(files, start=1):
        source = pd.read_parquet(
            path,
            columns=["Code", "Date", "Open", "High", "Low", "Close", "Volume"],
        )
        dates = pd.to_datetime(source["Date"], errors="coerce")
        frame = pd.DataFrame(
            {
                "security_id": "SEC_KR_" + source["Code"].astype(str).str.strip().str.zfill(6),
                "trade_date": dates,
                "open": pd.to_numeric(source["Open"], errors="coerce"),
                "high": pd.to_numeric(source["High"], errors="coerce"),
                "low": pd.to_numeric(source["Low"], errors="coerce"),
                "close": pd.to_numeric(source["Close"], errors="coerce"),
                "volume": pd.to_numeric(source["Volume"], errors="coerce"),
                "adj_close": pd.to_numeric(source["Close"], errors="coerce"),
                "currency": "KRW",
            }
        )
        frame = (
            frame.dropna(subset=["trade_date", "close"])
            .drop_duplicates(["security_id", "trade_date"], keep="last")
            .sort_values("trade_date")
        )
        _append_csv(frame, staged, header=header)
        header = False
        total_rows += len(frame)
        _record_coverage(coverage, frame, end_date=US_END_DATE)
        print(
            f"KR_STREAM years={file_index:,}/{len(files):,} "
            f"year={path.stem[-4:]} rows={total_rows:,}",
            flush=True,
        )

    listing = pd.read_csv(
        DATA_LAKE.bronze(
            "finance-datareader",
            "krx-listing",
            "2026-08-24.csv",
        )
    )
    current = pd.DataFrame(
        {
            "security_id": "SEC_KR_" + listing["Code"].astype(str).str.strip().str.zfill(6),
            "trade_date": KR_END_DATE,
            "open": pd.to_numeric(listing["Open"], errors="coerce"),
            "high": pd.to_numeric(listing["High"], errors="coerce"),
            "low": pd.to_numeric(listing["Low"], errors="coerce"),
            "close": pd.to_numeric(listing["Close"], errors="coerce"),
            "volume": pd.to_numeric(listing["Volume"], errors="coerce"),
            "adj_close": pd.to_numeric(listing["Close"], errors="coerce"),
            "currency": "KRW",
        }
    ).dropna(subset=["open", "high", "low", "close", "volume"])
    _append_csv(current, staged, header=header)
    total_rows += len(current)
    coverage[KR_END_DATE.strftime("%Y-%m-%d")] = current["security_id"].nunique()

    if not staged.is_file() or staged.stat().st_size <= 0:
        raise RuntimeError(f"streamed KR normalized price file is empty: {staged}")
    replace_file_with_permission_retry(staged, output)
    print(
        f"KR_STREAM_DONE rows={total_rows:,} current_rows={len(current):,} path={output}",
        flush=True,
    )
    print(pd.Series(coverage).sort_index().to_string(), flush=True)


def stream_us(*, part_index: int = 0, part_count: int = 1) -> None:
    all_files = sorted(DATA_LAKE.bronze("yfinance", "price").glob("*.csv"))
    if part_count < 1 or not 0 <= part_index < part_count:
        raise ValueError("US part arguments must satisfy 0 <= part_index < part_count")
    files = all_files[part_index::part_count]
    output = DATA_LAKE.silver(
        "us",
        "price",
        market_csv_name("normalized_price", market="us"),
    )
    staged = (
        output.with_name(".us_normalized_price.stream.csv.tmp")
        if part_count == 1
        else output.with_name(
            f".us_normalized_price.part-{part_index:02d}-of-{part_count:02d}.csv.tmp"
        )
    )
    staged.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    header = True
    total_rows = 0
    coverage: dict[str, int] = {}
    for file_index, path in enumerate(files, start=1):
        ticker = yfinance_price_ticker_from_storage_stem(path.stem)
        frame = normalize_yfinance_price_frame(pd.read_csv(path), ticker=ticker)
        _append_csv(frame, staged, header=header)
        header = False
        total_rows += len(frame)
        _record_coverage(coverage, frame, end_date=US_END_DATE)
        if file_index % 250 == 0 or file_index == len(files):
            print(
                f"US_STREAM part={part_index + 1}/{part_count} "
                f"files={file_index:,}/{len(files):,} rows={total_rows:,}",
                flush=True,
            )

    if not staged.is_file() or staged.stat().st_size <= 0:
        raise RuntimeError(f"streamed US normalized price file is empty: {staged}")
    if part_count == 1:
        replace_file_with_permission_retry(staged, output)
    print(
        f"US_STREAM_DONE part={part_index + 1}/{part_count} "
        f"rows={total_rows:,} path={staged if part_count > 1 else output}",
        flush=True,
    )
    print(pd.Series(coverage).sort_index().to_string(), flush=True)


def combine_us_parts(part_count: int) -> None:
    if part_count < 2:
        raise ValueError("part_count must be at least 2 when combining US parts")
    output = DATA_LAKE.silver(
        "us",
        "price",
        market_csv_name("normalized_price", market="us"),
    )
    parts = [
        output.with_name(f".us_normalized_price.part-{index:02d}-of-{part_count:02d}.csv.tmp")
        for index in range(part_count)
    ]
    missing = [str(path) for path in parts if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        raise FileNotFoundError(f"missing US normalized price parts: {missing}")

    staged = output.with_name(".us_normalized_price.combined.csv.tmp")
    staged.unlink(missing_ok=True)
    with staged.open("wb") as destination:
        for part_index, part in enumerate(parts):
            with part.open("rb") as source:
                if part_index:
                    source.readline()
                shutil.copyfileobj(source, destination, length=1024 * 1024 * 8)
    if staged.stat().st_size <= 0:
        raise RuntimeError(f"combined US normalized price file is empty: {staged}")
    replace_file_with_permission_retry(staged, output)
    for part in parts:
        part.unlink(missing_ok=True)

    coverage: dict[str, int] = {}
    total_rows = 0
    for chunk in pd.read_csv(output, usecols=["security_id", "trade_date"], chunksize=500_000):
        dates = pd.to_datetime(chunk["trade_date"], errors="coerce")
        recent = chunk.loc[dates.between(START_DATE, US_END_DATE)].copy()
        if not recent.empty:
            counts = recent.groupby(pd.to_datetime(recent["trade_date"]).dt.strftime("%Y-%m-%d")).size()
            for trade_date, count in counts.items():
                coverage[trade_date] = coverage.get(trade_date, 0) + int(count)
        total_rows += len(chunk)
    print(f"US_COMBINE_DONE rows={total_rows:,} path={output}", flush=True)
    print(pd.Series(coverage).sort_index().to_string(), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("market", choices=["kr", "us"])
    parser.add_argument("--part-index", type=int, default=0)
    parser.add_argument("--part-count", type=int, default=1)
    parser.add_argument("--combine", action="store_true")
    args = parser.parse_args()
    if args.market == "kr":
        stream_kr()
    elif args.combine:
        combine_us_parts(args.part_count)
    else:
        stream_us(part_index=args.part_index, part_count=args.part_count)


if __name__ == "__main__":
    main()
