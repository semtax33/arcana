from __future__ import annotations

from datetime import date, datetime
from glob import glob
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.paths import DATA_LAKE, PROJECT_ROOT, market_csv_name

ENGINE_DIR = Path(__file__).resolve().parent
BRONZE_BENCHMARK_DIR = DATA_LAKE.bronze("krx", "benchmark")
SILVER_BENCHMARK_PATH = DATA_LAKE.silver(
    "krx",
    "benchmark",
    market_csv_name("normalized_benchmark_price"),
)

DEFAULT_BENCHMARK_INDEX_CODES = {
    "KOSPI200": "1028",
    "KOSDAQ": "2001",
}

BENCHMARK_PRICE_COLUMNS = [
    "benchmark_id",
    "trade_date",
    "country",
    "market_mic",
    "benchmark_family",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "currency",
]

DATE_COLUMNS = ["trade_date", "date", "index", "\ub0a0\uc9dc", "\uc77c\uc790"]
OPEN_COLUMNS = ["open", "\uc2dc\uac00"]
HIGH_COLUMNS = ["high", "\uace0\uac00"]
LOW_COLUMNS = ["low", "\uc800\uac00"]
CLOSE_COLUMNS = ["close", "\uc885\uac00"]
VOLUME_COLUMNS = ["volume", "\uac70\ub798\ub7c9"]


def normalize_benchmark_id(benchmark_id: str) -> str:
    text = str(benchmark_id or "").strip().upper()
    if not text:
        raise ValueError("benchmark_id must not be empty")
    return text


def normalize_provider_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    return date.fromisoformat(text).strftime("%Y%m%d")


def normalize_benchmark_price_frame(
    frame: pd.DataFrame,
    *,
    benchmark_id: str,
) -> pd.DataFrame:
    benchmark_id = normalize_benchmark_id(benchmark_id)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BENCHMARK_PRICE_COLUMNS)

    df = frame.copy()
    if df.index.name is not None or not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()

    column_map = {
        "trade_date": _pick_column(df, DATE_COLUMNS),
        "open": _pick_column(df, OPEN_COLUMNS),
        "high": _pick_column(df, HIGH_COLUMNS),
        "low": _pick_column(df, LOW_COLUMNS),
        "close": _pick_column(df, CLOSE_COLUMNS),
        "volume": _pick_column(df, VOLUME_COLUMNS, required=False),
    }
    missing = [key for key, value in column_map.items() if key != "volume" and value is None]
    if missing:
        raise ValueError(f"benchmark frame is missing required columns: {', '.join(missing)}")

    result = pd.DataFrame(
        {
            "benchmark_id": benchmark_id,
            "trade_date": pd.to_datetime(df[column_map["trade_date"]], errors="coerce"),
            "country": "KR",
            "market_mic": "KRX",
            "benchmark_family": benchmark_id,
            "open": _numeric(df[column_map["open"]]),
            "high": _numeric(df[column_map["high"]]),
            "low": _numeric(df[column_map["low"]]),
            "close": _numeric(df[column_map["close"]]),
            "volume": _numeric(df[column_map["volume"]]) if column_map["volume"] else pd.NA,
            "currency": "KRW",
        }
    )
    result = result.dropna(subset=["trade_date", "close"])
    result["trade_date"] = result["trade_date"].dt.date
    return result[BENCHMARK_PRICE_COLUMNS].sort_values(
        ["benchmark_id", "trade_date"]
    ).reset_index(drop=True)


def normalize_benchmark_prices(
    path: str | Path | None = None,
    *,
    output_path: str | Path | None = SILVER_BENCHMARK_PATH,
) -> pd.DataFrame:
    files = _glob_files(str(path or (BRONZE_BENCHMARK_DIR / "*.csv")))
    if not files:
        raise FileNotFoundError("benchmark CSV files were not found")

    frames = []
    for file in files:
        file_path = Path(file)
        benchmark_id = normalize_benchmark_id(file_path.stem)
        frame = pd.read_csv(file_path)
        frames.append(normalize_benchmark_price_frame(frame, benchmark_id=benchmark_id))

    result = _concat_benchmark_frames(frames)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False, encoding="utf-8-sig")
    return result


def _resolve_benchmark_ids(
    benchmark_ids: list[str] | None,
    index_codes: dict[str, str],
) -> list[str]:
    if benchmark_ids is None:
        return sorted(index_codes)
    return [normalize_benchmark_id(benchmark_id) for benchmark_id in benchmark_ids]


def _concat_benchmark_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=BENCHMARK_PRICE_COLUMNS)
    return pd.concat(non_empty, ignore_index=True).sort_values(
        ["benchmark_id", "trade_date"]
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
