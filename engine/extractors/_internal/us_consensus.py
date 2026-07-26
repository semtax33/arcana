from __future__ import annotations

"""US consensus bronze collectors.

The collectors deliberately keep vendor payloads separate.  Alpha Vantage is
used for its event-relative history and Yahoo/yfinance for daily snapshots;
callers must never calculate a revision across those provider boundaries.
"""

from collections import deque
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import SourceRefreshLock
from engine.extractors._internal.yfinance_market_prices import (
    FILTERED_UNIVERSE_PATH,
    download_us_equity_universe,
    normalize_yfinance_ticker,
)


ALPHA_VANTAGE_QUERY_URL = "https://www.alphavantage.co/query"
ALPHA_VANTAGE_ENDPOINTS = {
    "earnings-estimates": "EARNINGS_ESTIMATES",
    "earnings": "EARNINGS",
    "splits": "SPLITS",
}
BRONZE_US_CONSENSUS_DIR = DATA_LAKE.bronze("consensus")
BRONZE_ALPHA_VANTAGE_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "alpha-vantage"
BRONZE_YAHOO_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "yahoo"
DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE = 75
DEFAULT_ALPHA_RETRIES = 3


class AlphaVantageRateLimitError(RuntimeError):
    """Raised after Alpha Vantage has rejected all retry attempts."""


class RollingRateLimiter:
    """Persist a strict rolling-window request budget across collector restarts."""

    def __init__(
        self,
        *,
        max_calls_per_minute: int = DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= int(max_calls_per_minute) <= DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE:
            raise ValueError("alpha_max_calls_per_minute must be between 1 and 75")
        self.max_calls_per_minute = int(max_calls_per_minute)
        self.state_path = Path(state_path or DATA_LAKE.meta("consensus", "alpha_vantage_rate_limit.json"))
        self.clock = clock
        self.sleeper = sleeper

    def acquire(self) -> None:
        """Reserve one request slot, sleeping until a rolling 60-second slot exists."""
        while True:
            now = float(self.clock())
            timestamps = deque(value for value in self._load() if now - value < 60.0 and value <= now + 1.0)
            if len(timestamps) < self.max_calls_per_minute:
                timestamps.append(now)
                self._save(list(timestamps))
                return
            self.sleeper(max(0.01, 60.0 - (now - timestamps[0])))

    def _load(self) -> list[float]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            values = payload.get("request_timestamps", []) if isinstance(payload, dict) else []
            return [float(value) for value in values]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _save(self, timestamps: list[float]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".alpha-rate-", suffix=".json", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"request_timestamps": timestamps}, handle)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def download_us_consensus(
    *,
    symbols: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    snapshot_date: str | date | None = None,
    force: bool = False,
    alpha_max_calls_per_minute: int = DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    alpha_retries: int = DEFAULT_ALPHA_RETRIES,
    output_root: str | Path = BRONZE_US_CONSENSUS_DIR,
    http_get: Callable[..., Any] | None = None,
    yahoo_ticker_factory: Callable[[str], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Download selected vendors into the US consensus bronze layout."""
    resolved_sources = _parse_sources(sources)
    resolved_symbols = _resolve_symbols(symbols)
    day = _snapshot_day(snapshot_date)
    root = Path(output_root)
    counts = {"symbols": len(resolved_symbols), "written": 0, "skipped": 0, "failed": 0}

    if "alpha-vantage" in resolved_sources:
        api_key = _alpha_api_key()
        limiter = RollingRateLimiter(
            max_calls_per_minute=alpha_max_calls_per_minute,
            state_path=root.parent.parent / "meta" / "consensus" / "alpha_vantage_rate_limit.json"
            if root != BRONZE_US_CONSENSUS_DIR
            else None,
            sleeper=sleeper,
        )
        with SourceRefreshLock("us-consensus-alpha-vantage"):
            for symbol in resolved_symbols:
                for dataset, function in ALPHA_VANTAGE_ENDPOINTS.items():
                    path = root / "alpha-vantage" / dataset / f"snapshot_date={day}" / f"ticker={symbol}.json"
                    if path.exists() and not force:
                        counts["skipped"] += 1
                        continue
                    try:
                        payload = _alpha_payload(
                            symbol,
                            function=function,
                            api_key=api_key,
                            limiter=limiter,
                            retries=alpha_retries,
                            http_get=http_get or requests.get,
                            sleeper=sleeper,
                        )
                        _write_json(path, payload)
                        counts["written"] += 1
                    except Exception as exc:  # isolated failures keep a universe backfill resumable.
                        counts["failed"] += 1
                        print(f"[WARN] alpha-vantage {dataset} symbol={symbol}: {type(exc).__name__}", flush=True)

    if "yahoo" in resolved_sources:
        factory = yahoo_ticker_factory or _default_yahoo_ticker_factory
        for symbol in resolved_symbols:
            path = root / "yahoo" / f"snapshot_date={day}" / f"ticker={symbol}.json"
            if path.exists() and not force:
                counts["skipped"] += 1
                continue
            try:
                payload = _yahoo_payload(symbol, factory)
                _write_json(path, payload)
                counts["written"] += 1
            except Exception as exc:
                counts["failed"] += 1
                print(f"[WARN] yahoo consensus symbol={symbol}: {type(exc).__name__}", flush=True)
    return counts


def _alpha_api_key() -> str:
    value = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not value:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is required for Alpha Vantage consensus downloads")
    return value


def _alpha_payload(
    symbol: str,
    *,
    function: str,
    api_key: str,
    limiter: RollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    attempts = max(0, int(retries)) + 1
    for attempt in range(attempts):
        limiter.acquire()  # every attempt, including a timeout, consumes a budget slot.
        try:
            response = http_get(
                ALPHA_VANTAGE_QUERY_URL,
                params={"function": function, "symbol": symbol, "apikey": api_key},
                timeout=(10, 60),
            )
        except Exception as exc:
            if attempt + 1 == attempts:
                raise AlphaVantageRateLimitError(
                    f"Alpha Vantage request failed for function={function}, symbol={symbol}"
                ) from exc
            sleeper(60.0 * (2**attempt))
            continue
        status = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except Exception as exc:
            payload = {}
            parse_error = exc
        else:
            parse_error = None
        limited = status == 429 or (isinstance(payload, dict) and any(key in payload for key in ("Note", "Information")))
        failed = status >= 400 or parse_error is not None or (isinstance(payload, dict) and "Error Message" in payload)
        if not limited and not failed and isinstance(payload, dict):
            return payload
        if attempt + 1 == attempts:
            raise AlphaVantageRateLimitError(f"Alpha Vantage request failed for function={function}, symbol={symbol}")
        retry_after = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
        try:
            delay = float(retry_after) if retry_after else 60.0 * (2**attempt if limited else 1)
        except (TypeError, ValueError):
            delay = 60.0
        sleeper(max(0.0, delay))
    raise AssertionError("unreachable")


def _default_yahoo_ticker_factory(symbol: str) -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on optional package installation.
        raise RuntimeError("yfinance is required for Yahoo consensus downloads") from exc
    return yf.Ticker(symbol)


def _yahoo_payload(symbol: str, ticker_factory: Callable[[str], Any]) -> dict[str, Any]:
    ticker = ticker_factory(symbol)
    methods = {
        "earnings_estimate": "get_earnings_estimate",
        "revenue_estimate": "get_revenue_estimate",
        "eps_trend": "get_eps_trend",
        "eps_revisions": "get_eps_revisions",
        "earnings_history": "get_earnings_history",
        "earnings_dates": "get_earnings_dates",
        "analyst_price_targets": "get_analyst_price_targets",
        "recommendations_summary": "get_recommendations_summary",
        "upgrades_downgrades": "get_upgrades_downgrades",
    }
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, method_name in methods.items():
        try:
            value = getattr(ticker, method_name)()
            data[name] = _json_ready_frame(value)
        except Exception as exc:  # one unavailable Yahoo module must not discard the rest of the snapshot.
            errors[name] = type(exc).__name__
    return {"symbol": symbol, "provider": "YAHOO_FINANCE", "data": data, "errors": errors}


def _json_ready_frame(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        frame = value.copy().reset_index()
        if frame.columns[0] != "index" and frame.columns[0] not in {"period", "date", "Date"}:
            frame = frame.rename(columns={frame.columns[0]: "index"})
        return {"kind": "dataframe", "records": json.loads(frame.to_json(orient="records", date_format="iso"))}
    if isinstance(value, pd.Series):
        return {"kind": "series", "values": json.loads(value.to_json(date_format="iso"))}
    return value


def _parse_sources(sources: Iterable[str] | None) -> set[str]:
    values = [str(item).strip().lower() for item in (sources or ("alpha-vantage", "yahoo"))]
    result = {item for item in values if item}
    aliases = {"alpha": "alpha-vantage", "yfinance": "yahoo", "all": "all"}
    result = {aliases.get(item, item) for item in result}
    if "all" in result:
        return {"alpha-vantage", "yahoo"}
    invalid = result - {"alpha-vantage", "yahoo"}
    if invalid:
        raise ValueError(f"unknown US consensus source(s): {', '.join(sorted(invalid))}")
    return result


def _resolve_symbols(symbols: Iterable[str] | None) -> list[str]:
    if symbols is not None:
        return list(dict.fromkeys(normalize_yfinance_ticker(value) for value in symbols if str(value).strip()))
    path = FILTERED_UNIVERSE_PATH
    if not path.exists():
        download_us_equity_universe()
    frame = pd.read_csv(path, dtype=str)
    column = "ticker" if "ticker" in frame.columns else "symbol"
    return list(dict.fromkeys(normalize_yfinance_ticker(value) for value in frame[column].dropna()))


def _snapshot_day(value: str | date | None) -> str:
    if value is None:
        return date.today().isoformat()
    return pd.Timestamp(value).date().isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".consensus-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
