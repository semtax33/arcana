from __future__ import annotations

"""Bronze collectors for US dividend-event histories.

The collector intentionally writes provider payloads before any normalization.
That makes a long-running all-universe backfill resumable and keeps the source
priority decision in the silver transformation layer.
"""

from datetime import date
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import SourceRefreshLock
from engine.extractors._internal.us_consensus import (
    DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    DEFAULT_ALPHA_RETRIES,
    RollingRateLimiter,
    _alpha_api_key,
    _alpha_payload,
    _resolve_symbols,
    _snapshot_day,
    _write_json,
)
from engine.extractors._internal.yfinance_market_prices import normalize_yfinance_ticker
from engine.extractors._internal.yfinance_market_prices import download_us_equity_universe


BRONZE_US_DIVIDEND_DIR = DATA_LAKE.bronze("dividend")
BRONZE_ALPHA_VANTAGE_DIVIDEND_DIR = BRONZE_US_DIVIDEND_DIR / "alpha-vantage"
BRONZE_YFINANCE_DIVIDEND_DIR = BRONZE_US_DIVIDEND_DIR / "yfinance"
US_DIVIDEND_SOURCE_PRIORITY = ("alpha-vantage", "edgartools", "yfinance")


def download_us_dividends(
    *,
    symbols: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    snapshot_date: str | date | None = None,
    force: bool = False,
    alpha_max_calls_per_minute: int = DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    alpha_retries: int = DEFAULT_ALPHA_RETRIES,
    output_root: str | Path = BRONZE_US_DIVIDEND_DIR,
    http_get: Callable[..., Any] | None = None,
    yfinance_ticker_factory: Callable[[str], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Download US dividend history into provider-separated bronze snapshots.

    A lower-priority provider is called only for tickers without a usable event
    from a higher-priority provider.  The edgartools stage is deliberately a
    no-op placeholder for the future ``999.Ex`` implementation.
    """
    resolved_sources = _parse_sources(sources)
    api_key = _alpha_api_key() if "alpha-vantage" in resolved_sources else None
    if symbols is None:
        # The requested all-US mode should include newly listed eligible names,
        # not merely the universe cached by a prior price or consensus run.
        download_us_equity_universe()
    resolved_symbols = list(
        dict.fromkeys(
            normalize_yfinance_ticker(symbol)
            for symbol in _resolve_symbols(symbols)
            if str(symbol).strip()
        )
    )
    day = _snapshot_day(snapshot_date)
    root = Path(output_root)
    counts = {
        "symbols": len(resolved_symbols),
        "written": 0,
        "skipped": 0,
        "failed": 0,
        "not_implemented": 0,
    }
    uncovered = set(resolved_symbols)

    if "alpha-vantage" in resolved_sources:
        limiter = RollingRateLimiter(
            max_calls_per_minute=alpha_max_calls_per_minute,
            state_path=_rate_limit_state_path(root),
            sleeper=sleeper,
        )
        lock_root = DATA_LAKE.root if root == BRONZE_US_DIVIDEND_DIR else root
        with SourceRefreshLock("alpha-vantage", data_lake_root=lock_root):
            for symbol in resolved_symbols:
                path = _snapshot_path(root, "alpha-vantage", day, symbol)
                payload = _read_json(path) if path.exists() and not force else None
                if payload is not None:
                    counts["skipped"] += 1
                else:
                    try:
                        payload = _alpha_payload(
                            symbol,
                            function="DIVIDENDS",
                            api_key=api_key or "",
                            limiter=limiter,
                            retries=alpha_retries,
                            http_get=http_get or requests.get,
                            sleeper=sleeper,
                        )
                        _write_json(path, payload)
                        counts["written"] += 1
                    except Exception as exc:  # keep a full-universe run resumable.
                        counts["failed"] += 1
                        print(f"[WARN] alpha-vantage dividends symbol={symbol}: {type(exc).__name__}", flush=True)
                        continue
                if _has_usable_dividend_event(payload):
                    uncovered.discard(symbol)

    if "edgartools" in resolved_sources and uncovered:
        # Reserved for the requested edgartools 999.Ex parser.  Do not issue
        # network requests until that provider is implemented.
        counts["not_implemented"] += len(uncovered)
        print(
            "[INFO] edgartools 999.Ex dividend fallback is not implemented; "
            f"remaining_symbols={len(uncovered):,}",
            flush=True,
        )

    if "yfinance" in resolved_sources and uncovered:
        factory = yfinance_ticker_factory or _default_yfinance_ticker_factory
        for symbol in sorted(uncovered):
            path = _snapshot_path(root, "yfinance", day, symbol)
            payload = _read_json(path) if path.exists() and not force else None
            if payload is not None:
                counts["skipped"] += 1
                continue
            try:
                payload = _yfinance_dividend_payload(symbol, factory)
                _write_json(path, payload)
                counts["written"] += 1
            except Exception as exc:
                counts["failed"] += 1
                print(f"[WARN] yfinance dividends symbol={symbol}: {type(exc).__name__}", flush=True)
    return counts


def _parse_sources(sources: Iterable[str] | None) -> set[str]:
    aliases = {
        "all": set(US_DIVIDEND_SOURCE_PRIORITY),
        "alpha": {"alpha-vantage"},
        "alpha-vantage": {"alpha-vantage"},
        "edgar": {"edgartools"},
        "edgartools": {"edgartools"},
        "yahoo": {"yfinance"},
        "yfinance": {"yfinance"},
    }
    values = sources or US_DIVIDEND_SOURCE_PRIORITY
    result: set[str] = set()
    for value in values:
        key = str(value).strip().lower()
        if not key:
            continue
        if key not in aliases:
            raise ValueError(f"unknown US dividend source: {value}")
        result.update(aliases[key])
    return result


def _snapshot_path(root: Path, provider: str, snapshot_date: str, symbol: str) -> Path:
    return root / provider / f"snapshot_date={snapshot_date}" / f"ticker={symbol}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _rate_limit_state_path(root: Path) -> Path | None:
    if root == BRONZE_US_DIVIDEND_DIR:
        # The default is the same persisted state used by US consensus.
        return None
    return root.parent / "meta" / "consensus" / "alpha_vantage_rate_limit.json"


def _has_usable_dividend_event(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    values = payload.get("data")
    if not isinstance(values, list):
        return False
    for item in values:
        if not isinstance(item, dict):
            continue
        amount = pd.to_numeric(item.get("amount"), errors="coerce")
        if pd.isna(amount) or float(amount) <= 0:
            continue
        if any(item.get(field) for field in ("ex_dividend_date", "payment_date", "record_date", "declaration_date")):
            return True
    return False


def _default_yfinance_ticker_factory(symbol: str) -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on optional package installation.
        raise RuntimeError("yfinance is required for US dividend fallback") from exc
    return yf.Ticker(symbol)


def _yfinance_dividend_payload(symbol: str, ticker_factory: Callable[[str], Any]) -> dict[str, Any]:
    values = getattr(ticker_factory(symbol), "dividends")
    if callable(values):
        values = values()
    if not isinstance(values, pd.Series):
        values = pd.Series(values)
    records = []
    for dividend_date, amount in values.items():
        parsed_date = pd.to_datetime(dividend_date, errors="coerce")
        parsed_amount = pd.to_numeric(amount, errors="coerce")
        if pd.isna(parsed_date) or pd.isna(parsed_amount) or float(parsed_amount) <= 0:
            continue
        records.append(
            {
                "ex_dividend_date": parsed_date.date().isoformat(),
                "amount": float(parsed_amount),
            }
        )
    return {"symbol": symbol, "provider": "YFINANCE", "data": records}
