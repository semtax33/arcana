from __future__ import annotations

import json
from pathlib import Path
from time import sleep
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import (
    json_source_validator,
    write_source_bytes,
    write_source_dataframe,
)
from engine.markets.us import US_MARKET_CONFIG


SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/{cik_file_key}.json"
SEC_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
SEC_COMPANYFACTS_DIR = DATA_LAKE.bronze("sec", "companyfacts")
DEFAULT_SEC_USER_AGENT = "Arcana contact@example.com"


def download_sec_company_tickers(
    output_path: str | Path = SEC_TICKER_MAP_PATH,
    *,
    user_agent: str = DEFAULT_SEC_USER_AGENT,
) -> pd.DataFrame:
    request = Request(
        SEC_COMPANY_TICKERS_URL,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rows = []
    for item in payload.values():
        rows.append(
            {
                "cik": str(int(item.get("cik_str"))),
                "ticker": US_MARKET_CONFIG.normalize_symbol(item.get("ticker")),
                "title": str(item.get("title", "")).strip(),
            }
        )

    df = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_source_dataframe(
        output_path,
        df,
        source="sec-company-tickers",
        encoding="utf-8-sig",
    )
    return df


def download_us_companyfacts(
    *,
    symbols: Iterable[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
    force: bool = False,
    sleep_seconds: float = 0.1,
    output_dir: str | Path = SEC_COMPANYFACTS_DIR,
    ticker_map_path: str | Path = SEC_TICKER_MAP_PATH,
    user_agent: str = DEFAULT_SEC_USER_AGENT,
) -> list[Path]:
    resolved_symbols = list(symbols) if symbols is not None else None
    ticker_map = _load_sec_ticker_map(ticker_map_path)
    if _needs_ticker_map(resolved_symbols) and ticker_map.empty:
        ticker_map = download_sec_company_tickers(ticker_map_path, user_agent=user_agent)

    downloads = _resolve_companyfacts_downloads(symbols=resolved_symbols, ticker_map=ticker_map)
    start = max(offset, 0)
    selected_downloads = downloads[start:]
    if limit is not None:
        selected_downloads = selected_downloads[: max(limit, 0)]

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for index, item in enumerate(selected_downloads, start=start):
        cik = item["cik"]
        cik_file_key = _cik_file_key(cik)
        symbol = item["ticker"] or cik_file_key
        out_path = output / f"{cik_file_key}.json"
        if out_path.exists() and not force:
            print(f"skipping {symbol} ({cik_file_key}, download_offset : {index})")
            continue

        print(f"downloading {symbol} ({cik_file_key}, download_offset : {index})....")
        write_source_bytes(
            out_path,
            _download_sec_companyfacts(cik, user_agent=user_agent),
            source="sec-companyfacts",
            validator=json_source_validator,
            metadata={"ticker": symbol, "cik": cik_file_key},
        )
        written.append(out_path)
        if sleep_seconds > 0:
            sleep(sleep_seconds)

    return written


def _download_sec_companyfacts(cik: str, *, user_agent: str = DEFAULT_SEC_USER_AGENT) -> bytes:
    cik_file_key = _cik_file_key(cik)
    request = Request(
        SEC_COMPANYFACTS_URL_TEMPLATE.format(cik_file_key=cik_file_key),
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def _load_sec_ticker_map(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return _empty_ticker_map()

    try:
        frame = pd.read_csv(path, dtype={"cik": "string", "ticker": "string", "title": "string"})
    except pd.errors.EmptyDataError:
        return _empty_ticker_map()

    if frame.empty:
        return _empty_ticker_map()

    for column in ("cik", "ticker", "title"):
        if column not in frame.columns:
            frame[column] = ""

    normalized = frame.loc[:, ["cik", "ticker", "title"]].copy()
    normalized["cik"] = normalized["cik"].map(_normalize_cik)
    normalized["ticker"] = normalized["ticker"].map(US_MARKET_CONFIG.normalize_symbol)
    normalized["title"] = normalized["title"].fillna("").astype(str).str.strip()
    normalized = normalized.loc[normalized["cik"].ne("") & normalized["ticker"].ne("")]
    return normalized.drop_duplicates("cik", keep="first").reset_index(drop=True)


def _empty_ticker_map() -> pd.DataFrame:
    return pd.DataFrame(columns=["cik", "ticker", "title"])


def _needs_ticker_map(symbols: Iterable[str] | None) -> bool:
    if symbols is None:
        return True
    return any(not _normalize_cik(symbol) for symbol in symbols)


def _resolve_companyfacts_downloads(
    *,
    symbols: Iterable[str] | None,
    ticker_map: pd.DataFrame,
) -> list[dict[str, str]]:
    rows = _ticker_rows(ticker_map)
    by_ticker = {row["ticker"]: row for row in rows}
    by_cik = {row["cik"]: row for row in rows}

    if symbols is None:
        if not rows:
            raise RuntimeError(
                "SEC ticker map is empty. Run `python -m engine.workflows.download --market us sec-tickers` first."
            )
        return rows

    resolved: list[dict[str, str]] = []
    seen_ciks: set[str] = set()
    missing: list[str] = []
    for raw_symbol in symbols:
        symbol = US_MARKET_CONFIG.normalize_symbol(raw_symbol)
        if not symbol:
            continue

        cik = _normalize_cik(symbol)
        row = by_cik.get(cik) if cik else by_ticker.get(symbol)
        if row is None and cik:
            row = {"cik": cik, "ticker": "", "title": ""}
        if row is None:
            missing.append(symbol)
            continue
        if row["cik"] in seen_ciks:
            continue
        resolved.append(row)
        seen_ciks.add(row["cik"])

    if missing:
        missing_symbols = ", ".join(missing)
        raise ValueError(
            f"unknown SEC symbols without CIK mapping: {missing_symbols}. "
            "Run `python -m engine.workflows.download --market us sec-tickers` first."
        )
    return resolved


def _ticker_rows(ticker_map: pd.DataFrame) -> list[dict[str, str]]:
    if ticker_map.empty:
        return []
    rows = ticker_map.sort_values("ticker").to_dict("records")
    return [
        {
            "cik": _normalize_cik(row.get("cik")),
            "ticker": US_MARKET_CONFIG.normalize_symbol(row.get("ticker")),
            "title": str(row.get("title") or "").strip(),
        }
        for row in rows
        if _normalize_cik(row.get("cik"))
    ]


def _normalize_cik(value: object) -> str:
    text = str(value or "").strip()
    if text.upper().startswith("CIK"):
        text = text[3:]
    text = text.strip()
    if not text.isdigit():
        return ""
    return str(int(text))


def _cik_file_key(cik: str) -> str:
    return f"CIK{int(_normalize_cik(cik)):010d}"
