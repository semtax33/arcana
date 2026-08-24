from __future__ import annotations

from pathlib import Path
import re
from time import sleep
import tempfile

import pandas as pd

from engine.core.paths import DATA_LAKE, market_symbol_csv_name
from engine.core.source_storage import write_source_dataframe
from engine.extractors._internal.finance_datareader_market_prices import (
    fetch_finance_datareader_price,
    finance_datareader_stock_codes,
)
from engine.extractors._internal.marcap_market_prices import (
    MARCAP_CACHE_DIR,
    fetch_marcap_price,
    iter_marcap_price_years,
)


DATE_COLUMN = "\ub0a0\uc9dc"
CSV_ENCODING = "utf-8-sig"
DEFAULT_PRICE_PROVIDERS = ("marcap", "finance-datareader")


class PriceProviderError(RuntimeError):
    """Raised when every configured Korean price provider fails."""


def fetch_price(
    stock_code: str,
    start_date: str,
    end_date: str,
    *,
    providers: tuple[str, ...] = DEFAULT_PRICE_PROVIDERS,
    marcap_cache_dir: str | Path = MARCAP_CACHE_DIR,
):
    errors: list[tuple[str, Exception]] = []
    successful_provider = False
    for provider in _normalize_price_providers(providers):
        try:
            if provider == "marcap":
                frame = fetch_marcap_price(
                    stock_code,
                    start_date,
                    end_date,
                    cache_dir=marcap_cache_dir,
                )
            else:
                frame = fetch_finance_datareader_price(stock_code, start_date, end_date)
            successful_provider = True
        except Exception as exc:
            errors.append((provider, exc))
            continue
        if not frame.empty:
            frame.attrs["provider"] = provider
            return frame

    if not successful_provider and errors:
        details = "; ".join(f"{provider}: {type(exc).__name__}: {exc}" for provider, exc in errors)
        raise PriceProviderError(
            f"all Korean price providers failed for stock_code={stock_code}: {details}"
        ) from errors[-1][1]
    result = pd.DataFrame()
    result.attrs["provider"] = None
    return result


def fetch_share(stock_code: str, start_date: str, end_date: str):
    from pykrx import stock

    df = stock.get_market_cap(start_date, end_date, stock_code)
    return df


def fetch_all_prices(
    stock_codes: list[str] | None,
    download_offset: int,
    start_date: str,
    end_date: str,
    *,
    providers: tuple[str, ...] = DEFAULT_PRICE_PROVIDERS,
    marcap_cache_dir: str | Path = MARCAP_CACHE_DIR,
    refresh_marcap_current_year: bool = True,
    output_dir: str | Path | None = None,
):
    providers = _normalize_price_providers(providers)
    output_dir = Path(output_dir or DATA_LAKE.bronze("krx", "price"))
    requested_codes = None
    if stock_codes is not None:
        requested_codes = sorted({_normalize_stock_code(code) for code in stock_codes})[
            max(0, int(download_offset)) :
        ]

    marcap_result = None
    marcap_error = None
    if "marcap" in providers:
        try:
            marcap_result = _fetch_all_prices_from_marcap(
                requested_codes,
                0 if requested_codes is not None else download_offset,
                start_date,
                end_date,
                cache_dir=marcap_cache_dir,
                refresh_current_year=refresh_marcap_current_year,
                output_dir=output_dir,
            )
        except Exception as exc:
            marcap_error = exc
            if requested_codes is None:
                raise PriceProviderError(
                    "marcap bulk download failed; a complete historical all-market fallback "
                    "is not available from FinanceDataReader"
                ) from exc
            print(f"[WARN] marcap bulk download failed; falling back to FinanceDataReader: {exc}")

    if marcap_result is not None:
        missing_codes = []
        if requested_codes is not None:
            missing_codes = sorted(set(requested_codes) - set(marcap_result["stock_codes"]))
        if missing_codes and "finance-datareader" in providers:
            fallback = _fetch_all_prices_from_finance_datareader(
                missing_codes,
                start_date,
                end_date,
                output_dir=output_dir,
            )
            marcap_result["finance_datareader"] = fallback
        return marcap_result

    if "finance-datareader" not in providers:
        if marcap_error is not None:
            raise PriceProviderError("marcap bulk download failed") from marcap_error
        raise PriceProviderError("no Korean price provider is configured")

    fallback_codes = requested_codes
    if fallback_codes is None:
        fallback_codes = finance_datareader_stock_codes()[max(0, int(download_offset)) :]
    return {
        "provider": "finance-datareader",
        "finance_datareader": _fetch_all_prices_from_finance_datareader(
            fallback_codes,
            start_date,
            end_date,
            output_dir=output_dir,
        ),
    }


def _fetch_all_prices_from_marcap(
    stock_codes: list[str] | None,
    download_offset: int,
    start_date: str,
    end_date: str,
    *,
    cache_dir: str | Path,
    refresh_current_year: bool,
    output_dir: Path,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    years: list[int] = []
    min_date = None
    max_date = None

    with tempfile.TemporaryDirectory(prefix=".marcap-price-", dir=output_dir.parent) as temp_dir:
        staging_dir = Path(temp_dir)
        for year, frame in iter_marcap_price_years(
            start_date,
            end_date,
            stock_codes=stock_codes,
            cache_dir=cache_dir,
            refresh_current_year=refresh_current_year,
        ):
            years.append(year)
            total_rows += len(frame)
            if not frame.empty:
                year_min = frame[DATE_COLUMN].min()
                year_max = frame[DATE_COLUMN].max()
                min_date = year_min if min_date is None else min(min_date, year_min)
                max_date = year_max if max_date is None else max(max_date, year_max)
            print(
                f"[INFO] marcap year={year} rows={len(frame):,} "
                f"symbols={frame['stock_code'].nunique() if not frame.empty else 0:,}",
                flush=True,
            )
            for stock_code, group in frame.groupby("stock_code", sort=False):
                staged_path = staging_dir / market_symbol_csv_name(stock_code)
                group.drop(columns=["stock_code"]).to_csv(
                    staged_path,
                    mode="a",
                    header=not staged_path.exists(),
                    index=False,
                    encoding=CSV_ENCODING,
                )

        staged_paths = sorted(staging_dir.glob("*.csv"))
        staged_paths = staged_paths[max(0, int(download_offset)) :]
        written_codes = []
        for offset, staged_path in enumerate(staged_paths, start=max(0, int(download_offset))):
            stock_code = staged_path.stem.removeprefix("kr_")
            print(
                f"writing marcap {stock_code} (download_offset : {offset})....",
                flush=True,
            )
            merge_symbol_csv(
                output_dir / market_symbol_csv_name(stock_code),
                staged_path,
                source="marcap",
            )
            written_codes.append(stock_code)

    return {
        "provider": "marcap",
        "years": years,
        "rows": total_rows,
        "files": len(written_codes),
        "stock_codes": written_codes,
        "min_date": min_date,
        "max_date": max_date,
    }


def _fetch_all_prices_from_finance_datareader(
    stock_codes: list[str],
    start_date: str,
    end_date: str,
    *,
    output_dir: Path,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = 0
    written = 0
    empty = 0
    failures: dict[str, str] = {}
    for offset, stock_code in enumerate(stock_codes):
        print(f"downloading FinanceDataReader {stock_code} (offset : {offset})....", flush=True)
        try:
            prices = fetch_finance_datareader_price(stock_code, start_date, end_date)
        except Exception as exc:
            failures[stock_code] = f"{type(exc).__name__}: {exc}"
            print(f"[WARN] FinanceDataReader failed stock_code={stock_code}: {exc}", flush=True)
            continue
        if prices.empty:
            empty += 1
            continue
        _write_download_then_merge(
            output_dir / market_symbol_csv_name(stock_code),
            prices,
            source="finance-datareader",
        )
        rows += len(prices)
        written += 1
        sleep(0.1)
    return {
        "rows": rows,
        "files": written,
        "empty": empty,
        "failures": failures,
    }


def _normalize_price_providers(providers) -> tuple[str, ...]:
    aliases = {
        "marcap": "marcap",
        "fdr": "finance-datareader",
        "financedatareader": "finance-datareader",
        "finance-datareader": "finance-datareader",
    }
    normalized = []
    for provider in providers:
        key = str(provider).strip().lower()
        if key not in aliases:
            raise ValueError(f"unsupported Korean price provider: {provider}")
        canonical = aliases[key]
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise ValueError("at least one Korean price provider is required")
    return tuple(normalized)


def _normalize_stock_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text.upper()


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


def _write_download_then_merge(
    path: str | Path,
    frame: pd.DataFrame,
    *,
    source: str = "krx-market-data",
) -> pd.DataFrame:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_download_path = _temporary_csv_path(path, "download")
    try:
        frame.to_csv(temp_download_path, index=False, encoding=CSV_ENCODING)
        return merge_symbol_csv(path, temp_download_path, source=source)
    finally:
        _unlink_if_exists(temp_download_path)


def merge_symbol_csv(
    path: str | Path,
    new_csv_path: str | Path,
    *,
    source: str = "krx-market-data",
) -> pd.DataFrame:
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
    _atomic_write_csv(merged, path, source=source)
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


def _atomic_write_csv(
    frame: pd.DataFrame,
    path: Path,
    *,
    source: str = "krx-market-data",
) -> None:
    write_source_dataframe(
        path,
        frame,
        source=source,
        encoding=CSV_ENCODING,
    )


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
