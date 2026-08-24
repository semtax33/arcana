from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
import tempfile

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import replace_file_with_permission_retry


MARCAP_DATA_URL = (
    "https://raw.githubusercontent.com/FinanceData/marcap/master/data/"
    "marcap-{year}.parquet"
)
MARCAP_CACHE_DIR = DATA_LAKE.bronze("marcap", "data")
MARCAP_FIRST_YEAR = 1995
MARCAP_REQUIRED_COLUMNS = ("Date", "Code", "Open", "High", "Low", "Close", "Volume")

DATE_COLUMN = "날짜"
OPEN_COLUMN = "시가"
HIGH_COLUMN = "고가"
LOW_COLUMN = "저가"
CLOSE_COLUMN = "종가"
VOLUME_COLUMN = "거래량"
CHANGE_RATE_COLUMN = "등락률"


def fetch_marcap_price(
    stock_code: str,
    start_date: str,
    end_date: str,
    *,
    cache_dir: str | Path = MARCAP_CACHE_DIR,
    download: bool = True,
    refresh_current_year: bool = False,
) -> pd.DataFrame:
    frames = [
        frame.drop(columns=["stock_code"])
        for _, frame in iter_marcap_price_years(
            start_date,
            end_date,
            stock_codes=[stock_code],
            cache_dir=cache_dir,
            download=download,
            refresh_current_year=refresh_current_year,
        )
        if not frame.empty
    ]
    if not frames:
        return _empty_price_frame()
    result = pd.concat(frames, ignore_index=True)
    return result.sort_values(DATE_COLUMN, kind="stable").reset_index(drop=True)


def iter_marcap_price_years(
    start_date: str,
    end_date: str,
    *,
    stock_codes: Sequence[str] | None = None,
    cache_dir: str | Path = MARCAP_CACHE_DIR,
    download: bool = True,
    refresh_current_year: bool = False,
) -> Iterator[tuple[int, pd.DataFrame]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        raise ValueError("start_date must be earlier than or equal to end_date")
    if end.year < MARCAP_FIRST_YEAR:
        return

    normalized_codes = None
    if stock_codes is not None:
        normalized_codes = {_normalize_stock_code(code) for code in stock_codes}

    for year in range(max(start.year, MARCAP_FIRST_YEAR), end.year + 1):
        path = ensure_marcap_year_file(
            year,
            cache_dir=cache_dir,
            download=download,
            refresh=refresh_current_year and year == date.today().year,
        )
        source = pd.read_parquet(path)
        missing = sorted(set(MARCAP_REQUIRED_COLUMNS) - set(source.columns))
        if missing:
            raise ValueError(f"marcap file is missing required columns: {missing}; path={path}")
        frame = normalize_marcap_price_frame(source, start_date=start, end_date=end)
        if normalized_codes is not None and not frame.empty:
            frame = frame.loc[frame["stock_code"].isin(normalized_codes)].copy()
        yield year, frame


def normalize_marcap_price_frame(
    source: pd.DataFrame,
    *,
    start_date=None,
    end_date=None,
) -> pd.DataFrame:
    missing = sorted(set(MARCAP_REQUIRED_COLUMNS) - set(source.columns))
    if missing:
        raise ValueError(f"marcap frame is missing required columns: {missing}")

    dates = pd.to_datetime(source["Date"], errors="coerce")
    valid = dates.notna()
    if start_date is not None:
        valid &= dates >= pd.Timestamp(start_date)
    if end_date is not None:
        valid &= dates <= pd.Timestamp(end_date)

    filtered = source.loc[valid].copy()
    if filtered.empty:
        return _empty_price_frame(include_stock_code=True)

    result = pd.DataFrame(index=filtered.index)
    result["stock_code"] = filtered["Code"].map(_normalize_stock_code)
    result[DATE_COLUMN] = dates.loc[filtered.index].dt.strftime("%Y-%m-%d")
    result[OPEN_COLUMN] = pd.to_numeric(filtered["Open"], errors="coerce")
    result[HIGH_COLUMN] = pd.to_numeric(filtered["High"], errors="coerce")
    result[LOW_COLUMN] = pd.to_numeric(filtered["Low"], errors="coerce")
    result[CLOSE_COLUMN] = pd.to_numeric(filtered["Close"], errors="coerce")
    result[VOLUME_COLUMN] = pd.to_numeric(filtered["Volume"], errors="coerce")
    change_column = _first_existing_column(filtered, "ChangesRatio", "ChagesRatio")
    if change_column is not None:
        result[CHANGE_RATE_COLUMN] = pd.to_numeric(filtered[change_column], errors="coerce")
    else:
        result[CHANGE_RATE_COLUMN] = pd.NA
    return result.reset_index(drop=True)


def ensure_marcap_year_file(
    year: int,
    *,
    cache_dir: str | Path = MARCAP_CACHE_DIR,
    download: bool = True,
    refresh: bool = False,
    timeout: float = 120.0,
) -> Path:
    year = int(year)
    if year < MARCAP_FIRST_YEAR:
        raise ValueError(f"marcap data starts in {MARCAP_FIRST_YEAR}")
    cache_dir = Path(cache_dir)
    path = cache_dir / f"marcap-{year}.parquet"
    if path.is_file() and path.stat().st_size > 0 and not refresh:
        return path
    if not download:
        raise FileNotFoundError(f"marcap year file not found: {path}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = MARCAP_DATA_URL.format(year=year)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".marcap-{year}.",
        suffix=".parquet.download",
        dir=cache_dir,
        delete=False,
    )
    staged = Path(handle.name)
    handle.close()
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            expected_size = int(response.headers.get("content-length") or 0)
            with staged.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if staged.stat().st_size <= 0:
            raise ValueError(f"downloaded marcap file is empty: {url}")
        if expected_size and staged.stat().st_size != expected_size:
            raise ValueError(
                "downloaded marcap file size mismatch: "
                f"expected={expected_size}, actual={staged.stat().st_size}, url={url}"
            )
        _validate_marcap_file(staged, year)
        replace_file_with_permission_retry(staged, path)
    except Exception:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def _validate_marcap_file(path: Path, year: int) -> None:
    frame = pd.read_parquet(path, columns=["Date", "Code", "Open", "High", "Low", "Close", "Volume"])
    if frame.empty:
        raise ValueError(f"marcap file contains no rows: {path}")
    dates = pd.to_datetime(frame["Date"], errors="coerce")
    if not dates.notna().any() or not dates.dropna().dt.year.eq(int(year)).all():
        raise ValueError(f"marcap file contains invalid dates for year={year}: {path}")


def _normalize_stock_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text.upper()


def _parse_date(value) -> pd.Timestamp:
    parsed = pd.to_datetime(str(value).strip(), errors="raise")
    return pd.Timestamp(parsed).normalize()


def _first_existing_column(frame: pd.DataFrame, *names: str) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _empty_price_frame(*, include_stock_code: bool = False) -> pd.DataFrame:
    columns = [DATE_COLUMN, OPEN_COLUMN, HIGH_COLUMN, LOW_COLUMN, CLOSE_COLUMN, VOLUME_COLUMN, CHANGE_RATE_COLUMN]
    if include_stock_code:
        columns.insert(0, "stock_code")
    return pd.DataFrame(columns=columns)
