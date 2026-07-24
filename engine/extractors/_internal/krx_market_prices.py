from __future__ import annotations

from pathlib import Path
import re
from time import sleep
import tempfile

import pandas as pd

from pykrx import stock

from engine.core.paths import DATA_LAKE, market_symbol_csv_name


DATE_COLUMN = "\ub0a0\uc9dc"
CSV_ENCODING = "utf-8-sig"


def fetch_price(stock_code: str, start_date: str, end_date: str):
    df = stock.get_market_ohlcv_by_date(
        start_date,
        end_date,
        stock_code,
        adjusted=True,
    )
    return df


def fetch_share(stock_code: str, start_date: str, end_date: str):
    df = stock.get_market_cap(start_date, end_date, stock_code)
    return df


def fetch_all_prices(stock_codes: list[str], download_offset: int, start_date: str, end_date: str):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        output_dir = DATA_LAKE.bronze("krx", "price")

        prices = _with_date_column(fetch_price(ticker, start_date, end_date))
        out_path = output_dir / market_symbol_csv_name(ticker)
        _write_download_then_merge(out_path, prices)
        sleep(0.1)


def fetch_all_shares(stock_codes: list[str], download_offset: int, start_date: str, end_date: str):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        output_dir = DATA_LAKE.bronze("krx", "shares")

        shares = _with_date_column(fetch_share(ticker, start_date, end_date))
        out_path = output_dir / market_symbol_csv_name(ticker)
        _write_download_then_merge(out_path, shares)
        sleep(0.1)


def _with_date_column(df: pd.DataFrame) -> pd.DataFrame:
    if DATE_COLUMN in df.columns:
        return df.copy()

    result = df.reset_index()
    return result.rename(columns={result.columns[0]: DATE_COLUMN})


def _write_download_then_merge(path: str | Path, frame: pd.DataFrame) -> pd.DataFrame:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_download_path = _temporary_csv_path(path, "download")
    try:
        frame.to_csv(temp_download_path, index=False, encoding=CSV_ENCODING)
        return merge_symbol_csv(path, temp_download_path)
    finally:
        _unlink_if_exists(temp_download_path)


def merge_symbol_csv(path: str | Path, new_csv_path: str | Path) -> pd.DataFrame:
    path = Path(path)
    new_csv_path = Path(new_csv_path)
    frames = []
    if path.exists():
        existing = _read_csv(path)
        if not existing.empty:
            frames.append(existing)

    new_frame = _read_csv(new_csv_path)
    if not new_frame.empty:
        frames.append(new_frame)

    if not frames:
        return pd.DataFrame()

    merged = _merge_symbol_frames(frames)
    _atomic_write_csv(merged, path)
    return merged


def _merge_symbol_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    date_column = detect_date_column(frames[0])
    normalized_frames = []
    for frame in frames:
        current = frame.copy()
        current_date_column = detect_date_column(current)
        if current_date_column != date_column:
            current = current.rename(columns={current_date_column: date_column})
        normalized_frames.append(current)

    merged = pd.concat(normalized_frames, ignore_index=True, sort=False)
    merged["_parsed_date"] = pd.to_datetime(merged[date_column], errors="coerce", format="mixed")
    merged = merged.dropna(subset=["_parsed_date"]).copy()
    merged[date_column] = merged["_parsed_date"].dt.strftime("%Y-%m-%d")
    return (
        merged.sort_values("_parsed_date", kind="stable")
        .drop_duplicates(date_column, keep="last")
        .drop(columns=["_parsed_date"])
        .reset_index(drop=True)
    )


def detect_date_column(frame: pd.DataFrame) -> str:
    if frame.empty:
        if len(frame.columns) == 0:
            raise ValueError("cannot detect date column from an empty dataframe without columns")
        return str(frame.columns[0])

    for column in frame.columns:
        if str(column).strip() == DATE_COLUMN:
            return str(column)

    best_column = None
    best_count = -1
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series):
            continue
        parsed = pd.to_datetime(series, errors="coerce")
        count = int(parsed.notna().sum())
        if count > best_count:
            best_column = str(column)
            best_count = count
    if best_column is not None and best_count > 0:
        return best_column

    for column in frame.columns:
        parsed = pd.to_datetime(frame[column], errors="coerce")
        count = int(parsed.notna().sum())
        if count > best_count:
            best_column = str(column)
            best_count = count
    if best_column is None or best_count <= 0:
        raise ValueError("cannot detect date column")
    return best_column


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    temp_merge_path = _temporary_csv_path(path, "merge")
    try:
        frame.to_csv(temp_merge_path, index=False, encoding=CSV_ENCODING)
        temp_merge_path.replace(path)
    finally:
        _unlink_if_exists(temp_merge_path)


def _temporary_csv_path(path: Path, purpose: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}.{purpose}.",
        suffix=".csv",
        dir=path.parent,
        delete=False,
    )
    handle.close()
    return Path(handle.name)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "_", name).strip()
    return name or "output.csv"
