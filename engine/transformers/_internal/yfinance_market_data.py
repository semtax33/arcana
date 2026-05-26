from __future__ import annotations

from glob import glob
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.identifiers import security_id_of
from engine.core.paths import DATA_LAKE, PROJECT_ROOT, market_csv_name
from engine.extractors._internal.yfinance_market_prices import normalize_yfinance_ticker
from engine.markets.us import US_MARKET_CONFIG


ENGINE_DIR = Path(__file__).resolve().parent
BRONZE_YFINANCE_PRICE_DIR = DATA_LAKE.bronze("yfinance", "price")
SILVER_US_PRICE_PATH = DATA_LAKE.silver(
    "us",
    "price",
    market_csv_name("normalized_price", market="us"),
)
US_PRICE_COLUMNS = [
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
CLICKHOUSE_DATE_MIN = pd.Timestamp("1970-01-01")
CLICKHOUSE_DATE_MAX = pd.Timestamp("2149-06-06")


def normalize_us_price(
    path: str | Path | None = None,
    *,
    output_path: str | Path | None = SILVER_US_PRICE_PATH,
    log_progress: bool = True,
    progress_interval: int = 100,
) -> pd.DataFrame:
    files = sorted(_glob_files(str(path or (BRONZE_YFINANCE_PRICE_DIR / "*.csv"))))
    if not files:
        raise FileNotFoundError("yfinance price CSV files were not found")

    total_files = len(files)
    if log_progress:
        print(
            f"normalizing US price files count={total_files:,}, "
            f"output_path={output_path or '-'}",
            flush=True,
        )

    frames = []
    for file_index, file in enumerate(files, start=1):
        file_path = Path(file)
        ticker = normalize_yfinance_ticker(file_path.stem)
        if log_progress and _should_log_progress(file_index, total_files, progress_interval):
            print(
                f"normalizing price file ticker={ticker} ({file_index:,}/{total_files:,})",
                flush=True,
            )
        frame = pd.read_csv(file_path)
        frames.append(normalize_yfinance_price_frame(frame, ticker=ticker))

    result = _concat_price_frames(frames)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False, encoding="utf-8-sig")
        if log_progress:
            print(f"saved normalized US price rows={len(result):,}, path={output}", flush=True)
    elif log_progress:
        print(f"normalized US price rows={len(result):,}", flush=True)
    return result


def normalize_yfinance_price_frame(frame: pd.DataFrame, *, ticker: str) -> pd.DataFrame:
    ticker = normalize_yfinance_ticker(ticker)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=US_PRICE_COLUMNS)

    df = frame.copy()
    if df.index.name is not None or not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()

    column_map = {
        "trade_date": _pick_column(df, ["Date", "Datetime", "trade_date", "date", "index"]),
        "open": _pick_column(df, ["Open", "open"]),
        "high": _pick_column(df, ["High", "high"]),
        "low": _pick_column(df, ["Low", "low"]),
        "close": _pick_column(df, ["Close", "close"]),
        "volume": _pick_column(df, ["Volume", "volume"], required=False),
        "adj_close": _pick_column(df, ["Adj Close", "adj_close", "AdjClose"], required=False),
    }
    missing = [key for key, value in column_map.items() if key not in {"volume", "adj_close"} and value is None]
    if missing:
        raise ValueError(f"yfinance price frame is missing required columns: {', '.join(missing)}")

    result = pd.DataFrame(
        {
            "security_id": security_id_of(ticker, US_MARKET_CONFIG),
            "trade_date": pd.to_datetime(df[column_map["trade_date"]], errors="coerce"),
            "open": _numeric(df[column_map["open"]]),
            "high": _numeric(df[column_map["high"]]),
            "low": _numeric(df[column_map["low"]]),
            "close": _numeric(df[column_map["close"]]),
            "volume": _numeric(df[column_map["volume"]]) if column_map["volume"] else pd.NA,
            "adj_close": (
                _numeric(df[column_map["adj_close"]])
                if column_map["adj_close"]
                else _numeric(df[column_map["close"]])
            ),
            "currency": US_MARKET_CONFIG.currency,
        }
    )
    result = result.dropna(subset=["trade_date", "close"])
    result = _drop_clickhouse_date_out_of_range(result, ticker=ticker)
    result = result.sort_values(["security_id", "trade_date"])
    result = result.drop_duplicates(["security_id", "trade_date"], keep="last")
    return result[US_PRICE_COLUMNS].reset_index(drop=True)


def read_normalized_us_price(path: str | Path = SILVER_US_PRICE_PATH) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if frame.empty:
        return pd.DataFrame(columns=US_PRICE_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"])
    frame = _drop_clickhouse_date_out_of_range(frame)
    return frame[US_PRICE_COLUMNS].reset_index(drop=True)


def _concat_price_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=US_PRICE_COLUMNS)
    return pd.concat(non_empty, ignore_index=True).sort_values(
        ["security_id", "trade_date"]
    ).reset_index(drop=True)


def _glob_files(path: str) -> list[str]:
    files = glob(path)
    if files:
        return files

    path_obj = Path(path)
    if path_obj.is_absolute():
        return files

    for base_dir in (PROJECT_ROOT, ENGINE_DIR):
        files = glob(str(base_dir / path_obj))
        if files:
            return files
    return files


def _should_log_progress(item_index: int, total_items: int, progress_interval: int) -> bool:
    if item_index in {1, total_items}:
        return True
    return progress_interval > 0 and item_index % progress_interval == 0


def _pick_column(df: pd.DataFrame, candidates: list[str], *, required: bool = True) -> str | None:
    normalized = {str(column).strip().lower(): column for column in df.columns}
    for candidate in candidates:
        column = normalized.get(candidate.lower())
        if column is not None:
            return column
    if required:
        return None
    return None


def _numeric(series: Any) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _drop_clickhouse_date_out_of_range(frame: pd.DataFrame, *, ticker: str | None = None) -> pd.DataFrame:
    valid_dates = frame["trade_date"].between(CLICKHOUSE_DATE_MIN, CLICKHOUSE_DATE_MAX)
    if valid_dates.all():
        return frame

    invalid = frame.loc[~valid_dates, "trade_date"]
    ticker_msg = f" ticker={ticker}" if ticker else ""
    print(
        "dropped US price rows with ClickHouse Date out of range"
        f"{ticker_msg}, rows={len(invalid):,}, "
        f"min={invalid.min().date()}, max={invalid.max().date()}",
        flush=True,
    )
    return frame.loc[valid_dates].copy()
