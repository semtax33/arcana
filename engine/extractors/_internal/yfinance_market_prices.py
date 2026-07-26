from __future__ import annotations

from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
import re
import ssl
from time import sleep
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_dataframe


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
BRONZE_YFINANCE_PRICE_DIR = DATA_LAKE.bronze("yfinance", "price")
BRONZE_YFINANCE_UNIVERSE_DIR = DATA_LAKE.bronze("yfinance", "universe")
FILTERED_UNIVERSE_PATH = BRONZE_YFINANCE_UNIVERSE_DIR / "us_equity_universe.csv"

EXCLUDED_INSTRUMENT_PATTERN = (
    r"\b(?:"
    r"warrants?|rights?|units?|funds?|etfs?|etns?|notes?|bonds?|debentures?|"
    r"trusts?|closed end|closed-end|index|option|calls?|puts?"
    r")\b"
)
INCLUDED_EQUITY_PATTERN = (
    r"\b(?:common stock|common shares?|capital stock|ordinary shares?|ordinary stock|"
    r"american depositary|adr|ads)\b"
)
PREFERRED_SHARE_PATTERN = r"\b(?:preferred|preference)\b"
# ADR/ADS are eligible securities but are never deduplicated as domestic share classes.
PREFERRED_OR_ADR_PATTERN = r"\b(?:preferred|preference|american depositary|adr|ads|depositary shares?)\b"
SHARE_CLASS_PATTERN = re.compile(
    r"\bclass\s+(?P<class>iii|ii|iv|v|a|b|c|d|e|f|g|h|i)\b",
    flags=re.IGNORECASE,
)
SYMBOL_CLASS_PATTERN = re.compile(r"[./-](?P<class>[A-Z])$")
NON_VOTING_PATTERN = re.compile(
    r"\b(?:non[- ]?voting|no voting|without voting)\b",
    flags=re.IGNORECASE,
)
LIMITED_VOTING_PATTERN = re.compile(
    r"\b(?:limited voting|restricted voting|low voting)\b",
    flags=re.IGNORECASE,
)
# NasdaqTrader names rarely expose exact votes. This keeps explicit non-voting first,
# then uses a conservative US dual-class heuristic for public share classes.
CLASS_VOTING_PRIORITY = {
    "C": 0,
    "A": 1,
    "B": 2,
    "D": 3,
    "E": 4,
    "F": 5,
    "G": 6,
    "H": 7,
    "I": 8,
    "II": 9,
    "III": 10,
    "IV": 11,
    "V": 12,
}


def download_us_price_histories(
    *,
    symbols: Iterable[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
    force: bool = False,
    sleep_seconds: float = 0.0,
    output_dir: str | Path = BRONZE_YFINANCE_PRICE_DIR,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Path]:
    resolved_symbols = _resolve_download_symbols(symbols)
    selected_symbols = resolved_symbols[max(offset, 0):]
    if limit is not None:
        selected_symbols = selected_symbols[: max(limit, 0)]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for index, ticker in enumerate(selected_symbols, start=max(offset, 0)):
        out_path = output_dir / f"{ticker}.csv"
        existing = pd.DataFrame()
        effective_start = start_date
        if out_path.exists() and not force:
            existing = pd.read_csv(out_path)
            latest = _latest_yfinance_frame_date(existing)
            if latest is not None:
                next_date = latest + timedelta(days=1)
                requested_start = _to_date(start_date) if start_date else None
                effective_start = max(
                    value for value in (next_date, requested_start) if value is not None
                ).isoformat()
                requested_end = _to_date(end_date) if end_date else date.today()
                if next_date > requested_end:
                    print(f"skipping {ticker} (already fresh through {latest})")
                    continue

        print(f"downloading {ticker} (download_offset : {index})....")
        frame = fetch_yfinance_price(
            ticker,
            start_date=effective_start,
            end_date=end_date,
        )
        if frame.empty:
            print(f"empty yfinance result: {ticker}")
            continue

        if not existing.empty:
            frame = _merge_yfinance_price_frames(existing, frame)
        write_source_dataframe(
            out_path,
            frame,
            source="yfinance-price",
            encoding="utf-8-sig",
            metadata={"ticker": ticker},
        )
        written.append(out_path)
        if sleep_seconds > 0:
            sleep(sleep_seconds)

    return written


def _latest_yfinance_frame_date(frame: pd.DataFrame) -> date | None:
    if frame is None or frame.empty:
        return None
    column = next(
        (
            candidate
            for candidate in ("Date", "Datetime", "date", "trade_date")
            if candidate in frame.columns
        ),
        None,
    )
    if column is None:
        return None
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    return values.max().date() if not values.empty else None


def _merge_yfinance_price_frames(
    existing: pd.DataFrame,
    incremental: pd.DataFrame,
) -> pd.DataFrame:
    frames = [frame.copy() for frame in (existing, incremental) if not frame.empty]
    merged = pd.concat(frames, ignore_index=True, sort=False)
    date_column = next(
        (
            candidate
            for candidate in ("Date", "Datetime", "date", "trade_date")
            if candidate in merged.columns
        ),
        None,
    )
    if date_column is None:
        raise ValueError("yfinance source is missing a date column")
    merged["_parsed_date"] = pd.to_datetime(merged[date_column], errors="coerce")
    merged = merged.dropna(subset=["_parsed_date"]).copy()
    merged[date_column] = merged["_parsed_date"].dt.strftime("%Y-%m-%d")
    return (
        merged.sort_values("_parsed_date", kind="stable")
        .drop_duplicates(date_column, keep="last")
        .drop(columns=["_parsed_date"])
        .reset_index(drop=True)
    )


def _to_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def fetch_yfinance_price(
    ticker: str,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    normalize_ticker: bool = True,
) -> pd.DataFrame:
    yf = _import_yfinance()
    ticker = (
        normalize_yfinance_ticker(ticker)
        if normalize_ticker
        else str(ticker or "").strip().upper()
    )
    download_kwargs = {
        "interval": "1d",
        "auto_adjust": False,
        "actions": True,
        "repair": True,
        "progress": False,
    }
    if start_date or end_date:
        if start_date:
            download_kwargs["start"] = _to_yfinance_date(start_date)
        if end_date:
            download_kwargs["end"] = _to_yfinance_date(end_date, add_day=True)
    else:
        download_kwargs["period"] = "max"

    frame = yf.download(ticker, **download_kwargs)
    if frame is None or frame.empty:
        return pd.DataFrame()

    frame = _flatten_yfinance_columns(frame)
    if frame.index.name is not None or not isinstance(frame.index, pd.RangeIndex):
        frame = frame.reset_index()
    return frame


def download_us_equity_universe(
    *,
    output_dir: str | Path = BRONZE_YFINANCE_UNIVERSE_DIR,
) -> pd.DataFrame:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    nasdaq = parse_nasdaq_symbol_directory_text(
        _download_text(NASDAQ_LISTED_URL),
        source="nasdaqlisted",
    )
    other = parse_nasdaq_symbol_directory_text(
        _download_text(OTHER_LISTED_URL),
        source="otherlisted",
    )

    write_source_dataframe(
        output_dir / "nasdaqlisted.csv",
        nasdaq,
        source="nasdaqtrader-universe",
    )
    write_source_dataframe(
        output_dir / "otherlisted.csv",
        other,
        source="nasdaqtrader-universe",
    )
    universe = filter_us_equity_universe(nasdaq, other)
    write_source_dataframe(
        output_dir / "us_equity_universe.csv",
        universe,
        source="nasdaqtrader-universe",
    )
    return universe


def parse_nasdaq_symbol_directory_text(text: str, *, source: str) -> pd.DataFrame:
    rows = [
        line
        for line in str(text).splitlines()
        if line.strip() and not line.startswith("File Creation Time")
    ]
    if not rows:
        return pd.DataFrame()

    frame = pd.read_csv(StringIO("\n".join(rows)), sep="|", dtype=str).fillna("")
    frame["source"] = source
    return frame


def filter_us_equity_universe(nasdaq: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    frames = [
        _standardize_symbol_directory_frame(nasdaq, symbol_col="Symbol", source="nasdaqlisted"),
        _standardize_symbol_directory_frame(other, symbol_col="ACT Symbol", source="otherlisted"),
    ]
    combined = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=_universe_columns())

    combined["ticker"] = combined["raw_symbol"].map(normalize_yfinance_ticker)
    combined["security_name_lower"] = combined["security_name"].str.lower()
    combined["is_etf"] = combined["is_etf"].str.upper()
    combined["test_issue"] = combined["test_issue"].str.upper()

    is_preferred_share = combined["security_name_lower"].str.contains(
        PREFERRED_SHARE_PATTERN,
        regex=True,
        na=False,
    )
    has_included_equity_text = combined["security_name_lower"].str.contains(
        INCLUDED_EQUITY_PATTERN,
        regex=True,
        na=False,
    )
    has_excluded_instrument_text = combined["security_name_lower"].str.contains(
        EXCLUDED_INSTRUMENT_PATTERN,
        regex=True,
        na=False,
    )

    keep = (
        combined["ticker"].ne("")
        & combined["is_etf"].ne("Y")
        & combined["test_issue"].ne("Y")
        & has_included_equity_text
        & ~has_excluded_instrument_text
        & ~is_preferred_share
    )
    result = combined.loc[keep, _universe_columns()].copy()
    result = _keep_lowest_voting_share_classes(result)
    result = result.drop_duplicates("ticker", keep="first")
    return result.sort_values("ticker").reset_index(drop=True)


def normalize_yfinance_ticker(symbol: object) -> str:
    ticker = str(symbol or "").strip().upper()
    for old, new in ((".", "-"), ("/", "-"), ("^", "-"), ("$", "-")):
        ticker = ticker.replace(old, new)
    return "".join(ticker.split())


def _resolve_download_symbols(symbols: Iterable[str] | None) -> list[str]:
    if symbols is not None:
        return sorted({normalize_yfinance_ticker(symbol) for symbol in symbols if str(symbol).strip()})

    universe = download_us_equity_universe()
    return universe["ticker"].dropna().astype(str).tolist()


def _standardize_symbol_directory_frame(
    frame: pd.DataFrame,
    *,
    symbol_col: str,
    source: str,
) -> pd.DataFrame:
    if frame is None or frame.empty or symbol_col not in frame.columns:
        return pd.DataFrame(columns=_universe_columns())

    exchange = frame["Exchange"] if "Exchange" in frame.columns else _default_series(frame, "XNAS")
    return pd.DataFrame(
        {
            "ticker": "",
            "raw_symbol": frame[symbol_col].astype(str).str.strip(),
            "security_name": _column_or_default(frame, "Security Name").astype(str).str.strip(),
            "exchange": exchange,
            "source": _column_or_default(frame, "source", source).astype(str),
            "is_etf": _column_or_default(frame, "ETF").astype(str).str.strip(),
            "test_issue": _column_or_default(frame, "Test Issue").astype(str).str.strip(),
        }
    )


def _universe_columns() -> list[str]:
    return [
        "ticker",
        "raw_symbol",
        "security_name",
        "exchange",
        "source",
        "is_etf",
        "test_issue",
    ]


def _keep_lowest_voting_share_classes(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame

    ranked = frame.copy()
    ranked["_share_class"] = ranked.apply(_share_class_of_row, axis=1)
    class_mask = ranked["_share_class"].ne("")
    if not class_mask.any():
        return ranked.drop(columns=["_share_class"])

    ranked["_issuer_key"] = ranked.apply(_issuer_group_key_of_row, axis=1)
    ranked["_voting_rank"] = ranked.apply(_voting_rank_of_row, axis=1)

    keep_indexes = set(ranked.index[~class_mask])
    class_rows = ranked.loc[class_mask].copy()
    for _, group in class_rows.groupby("_issuer_key", sort=False):
        ordered = group.sort_values(["_voting_rank", "ticker"], kind="stable")
        keep_indexes.add(ordered.index[0])

    return (
        ranked.loc[sorted(keep_indexes)]
        .drop(columns=["_share_class", "_issuer_key", "_voting_rank"])
        .reset_index(drop=True)
    )


def _share_class_of_row(row: pd.Series) -> str:
    security_name = str(row.get("security_name", ""))
    if re.search(PREFERRED_OR_ADR_PATTERN, security_name, flags=re.IGNORECASE):
        return ""

    name_match = SHARE_CLASS_PATTERN.search(security_name)
    if name_match:
        return name_match.group("class").upper()

    raw_symbol = str(row.get("raw_symbol", ""))
    symbol_match = SYMBOL_CLASS_PATTERN.search(raw_symbol.upper())
    if symbol_match:
        return symbol_match.group("class").upper()

    return ""


def _issuer_group_key_of_row(row: pd.Series) -> str:
    name = str(row.get("security_name", "")).lower()
    name = SHARE_CLASS_PATTERN.sub("", name)
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(
        r"\b(?:common|ordinary|capital)\s+(?:stock|shares?)\b",
        " ",
        name,
    )
    name = re.sub(r"\b(?:stock|shares?)\b", " ", name)
    name = re.sub(
        r"\b(?:new|incorporated|inc|corp|corporation|plc|ltd|limited|company|co)\b",
        " ",
        name,
    )
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    if name:
        return name
    return normalize_yfinance_ticker(row.get("raw_symbol", ""))


def _voting_rank_of_row(row: pd.Series) -> int:
    security_name = str(row.get("security_name", ""))
    if NON_VOTING_PATTERN.search(security_name):
        return -2
    if LIMITED_VOTING_PATTERN.search(security_name):
        return -1

    share_class = str(row.get("_share_class", "")).upper()
    return CLASS_VOTING_PRIORITY.get(share_class, 99)


def _download_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Arcana yfinance universe loader"})
    try:
        return _read_url_text(request)
    except URLError as exc:
        if not _is_ssl_certificate_error(exc):
            raise

        context = _certifi_ssl_context()
        if context is None:
            raise RuntimeError(
                "SSL certificate verification failed while downloading NasdaqTrader symbols. "
                "Install certifi or fix the local Python certificate store: pip install certifi"
            ) from exc

        return _read_url_text(request, context=context)


def _read_url_text(request: Request, *, context: ssl.SSLContext | None = None) -> str:
    kwargs = {"timeout": 60}
    if context is not None:
        kwargs["context"] = context
    with urlopen(request, **kwargs) as response:
        return response.read().decode("utf-8")


def _is_ssl_certificate_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _certifi_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def _column_or_default(frame: pd.DataFrame, column: str, default: str = "") -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return _default_series(frame, default)


def _default_series(frame: pd.DataFrame, value: str) -> pd.Series:
    return pd.Series([value] * len(frame), index=frame.index)


def _import_yfinance():
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is required for US price downloads. Install it with: pip install yfinance"
        ) from exc
    return yf


def _to_yfinance_date(value: str | date, *, add_day: bool = False) -> str:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d").date()
        else:
            parsed = date.fromisoformat(text)
    if add_day:
        parsed += timedelta(days=1)
    return parsed.strftime("%Y-%m-%d")


def _flatten_yfinance_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame.columns, pd.MultiIndex):
        return frame

    flattened = frame.copy()
    known_columns = {
        "Open",
        "High",
        "Low",
        "Close",
        "Adj Close",
        "Volume",
        "Dividends",
        "Stock Splits",
    }
    flattened.columns = [
        next((str(part) for part in column if str(part) in known_columns), str(column[-1]))
        for column in flattened.columns
    ]
    return flattened
