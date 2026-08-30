from __future__ import annotations

"""US consensus bronze collectors.

Vendor payloads stay isolated. Finnworlds is the primary target-price source,
FMP is the primary current estimate fallback, Alpha Vantage supplies
event-relative history, and Yahoo/yfinance supplies current fallback fields.
Revisions must never be calculated across provider boundaries.
"""

from collections import deque
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import (
    SourceRefreshLock,
    replace_file_with_permission_retry,
)
from engine.extractors._internal.yfinance_market_prices import (
    FILTERED_UNIVERSE_PATH,
    download_us_equity_universe,
    normalize_yfinance_ticker,
)


ALPHA_VANTAGE_QUERY_URL = "https://www.alphavantage.co/query"
FMP_BASE_URL = "https://financialmodelingprep.com/stable"
FINNWORLDS_COMPANY_RATINGS_URL = "https://api.finnworlds.com/api/v1/companyratings"
FMP_ENDPOINTS = {
    "analyst-estimates": "analyst-estimates",
    "price-target-summary": "price-target-summary",
}
ALPHA_VANTAGE_ENDPOINTS = {
    "earnings-estimates": "EARNINGS_ESTIMATES",
    "earnings": "EARNINGS",
    "overview": "OVERVIEW",
    "splits": "SPLITS",
}
BRONZE_US_CONSENSUS_DIR = DATA_LAKE.bronze("consensus")
BRONZE_FINNWORLDS_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "finnworlds"
BRONZE_FMP_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "fmp"
BRONZE_ALPHA_VANTAGE_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "alpha-vantage"
BRONZE_YAHOO_CONSENSUS_DIR = BRONZE_US_CONSENSUS_DIR / "yahoo"
US_CONSENSUS_SOURCE_PRIORITY = ("finnworlds", "fmp", "alpha-vantage", "yfinance")
FINNWORLDS_BACKFILL_SCHEMA_VERSION = 1
FINNWORLDS_BILLED_CALL_MULTIPLIER = 10
DEFAULT_FINNWORLDS_DATE_FROM = "2000-01-01"
DEFAULT_FINNWORLDS_MAX_CALLS_PER_MINUTE = 120
MAX_FINNWORLDS_CALLS_PER_MINUTE = 120
DEFAULT_FINNWORLDS_RETRIES = 3
DEFAULT_FMP_MAX_CALLS_PER_MINUTE = 720
MAX_FMP_CALLS_PER_MINUTE = 750
DEFAULT_FMP_RETRIES = 3
FMP_PAGE_LIMIT = 1000
DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE = 75
DEFAULT_ALPHA_RETRIES = 3


class AlphaVantageRateLimitError(RuntimeError):
    """Raised after Alpha Vantage has rejected all retry attempts."""


class FmpRateLimitError(RuntimeError):
    """Raised after FMP has rejected all rate-limit retry attempts."""


class FinnworldsRateLimitError(RuntimeError):
    """Raised after Finnworlds has rejected all rate-limit retry attempts."""


class ProviderAuthenticationError(RuntimeError):
    """Raised when a provider key or subscription cannot authorize requests."""


class _PersistentRollingRateLimiter:
    """Persist a strict rolling-window request budget across collector restarts."""

    def __init__(
        self,
        *,
        max_calls_per_minute: int,
        maximum: int,
        state_path: str | Path,
        temporary_prefix: str,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= int(max_calls_per_minute) <= int(maximum):
            raise ValueError(f"max_calls_per_minute must be between 1 and {maximum}")
        self.max_calls_per_minute = int(max_calls_per_minute)
        self.state_path = Path(state_path)
        self.temporary_prefix = temporary_prefix
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
        fd, temporary = tempfile.mkstemp(
            prefix=self.temporary_prefix,
            suffix=".json",
            dir=self.state_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"request_timestamps": timestamps}, handle)
            replace_file_with_permission_retry(Path(temporary), self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class RollingRateLimiter(_PersistentRollingRateLimiter):
    """Alpha Vantage's persisted rolling-window limiter."""

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
        super().__init__(
            max_calls_per_minute=max_calls_per_minute,
            maximum=DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
            state_path=state_path or DATA_LAKE.meta("consensus", "alpha_vantage_rate_limit.json"),
            temporary_prefix=".alpha-rate-",
            clock=clock,
            sleeper=sleeper,
        )


class FmpRollingRateLimiter(_PersistentRollingRateLimiter):
    """FMP's persisted rolling-window limiter."""

    def __init__(
        self,
        *,
        max_calls_per_minute: int = DEFAULT_FMP_MAX_CALLS_PER_MINUTE,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= int(max_calls_per_minute) <= MAX_FMP_CALLS_PER_MINUTE:
            raise ValueError("fmp_max_calls_per_minute must be between 1 and 750")
        super().__init__(
            max_calls_per_minute=max_calls_per_minute,
            maximum=MAX_FMP_CALLS_PER_MINUTE,
            state_path=state_path or DATA_LAKE.meta("consensus", "fmp_rate_limit.json"),
            temporary_prefix=".fmp-rate-",
            clock=clock,
            sleeper=sleeper,
        )


class FinnworldsRollingRateLimiter(_PersistentRollingRateLimiter):
    """Finnworlds Developer membership persisted rolling-window limiter."""

    def __init__(
        self,
        *,
        max_calls_per_minute: int = DEFAULT_FINNWORLDS_MAX_CALLS_PER_MINUTE,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= int(max_calls_per_minute) <= MAX_FINNWORLDS_CALLS_PER_MINUTE:
            raise ValueError("finnworlds_max_calls_per_minute must be between 1 and 120")
        super().__init__(
            max_calls_per_minute=max_calls_per_minute,
            maximum=MAX_FINNWORLDS_CALLS_PER_MINUTE,
            state_path=state_path
            or DATA_LAKE.meta("consensus", "finnworlds_rate_limit.json"),
            temporary_prefix=".finnworlds-rate-",
            clock=clock,
            sleeper=sleeper,
        )


def download_us_consensus(
    *,
    symbols: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    snapshot_date: str | date | None = None,
    force: bool = False,
    finnworlds_date_from: str | date = DEFAULT_FINNWORLDS_DATE_FROM,
    finnworlds_date_to: str | date | None = None,
    finnworlds_max_calls_per_minute: int = DEFAULT_FINNWORLDS_MAX_CALLS_PER_MINUTE,
    finnworlds_retries: int = DEFAULT_FINNWORLDS_RETRIES,
    fmp_max_calls_per_minute: int = DEFAULT_FMP_MAX_CALLS_PER_MINUTE,
    fmp_retries: int = DEFAULT_FMP_RETRIES,
    alpha_max_calls_per_minute: int = DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    alpha_retries: int = DEFAULT_ALPHA_RETRIES,
    output_root: str | Path = BRONZE_US_CONSENSUS_DIR,
    http_get: Callable[..., Any] | None = None,
    yahoo_ticker_factory: Callable[[str], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Download selected vendors into the US consensus bronze layout."""
    resolved_sources = _parse_sources(sources)
    resolved_symbols = _resolve_symbols(symbols)
    day = _snapshot_day(snapshot_date)
    root = Path(output_root)
    counts: dict[str, Any] = {
        "symbols": len(resolved_symbols),
        "written": 0,
        "skipped": 0,
        "failed": 0,
        "no_data": 0,
        "fallback_symbols": 0,
        "providers": {
            source: {
                "written": 0,
                "skipped": 0,
                "failed": 0,
                "no_data": 0,
                "auth_disabled": False,
            }
            for source in resolved_sources
        },
    }
    request_get = http_get or requests.get
    fallback_symbols: set[str] = set()

    if "finnworlds" in resolved_sources:
        fallback_symbols.update(
            _download_finnworlds_company_ratings(
                resolved_symbols,
                day=day,
                date_from=_snapshot_day(finnworlds_date_from),
                date_to=_snapshot_day(finnworlds_date_to or day),
                force=force,
                root=root,
                counts=counts,
                max_calls_per_minute=finnworlds_max_calls_per_minute,
                retries=finnworlds_retries,
                http_get=request_get,
                sleeper=sleeper,
            )
        )

    if "fmp" in resolved_sources:
        provider_counts = counts["providers"]["fmp"]
        api_key = os.getenv("FMP_API_KEY", "").strip()
        if not api_key:
            provider_counts["auth_disabled"] = True
            fallback_symbols.update(resolved_symbols)
            print(
                "[WARN] fmp consensus disabled: FMP_API_KEY environment variable is not set",
                flush=True,
            )
        else:
            limiter = FmpRollingRateLimiter(
                max_calls_per_minute=fmp_max_calls_per_minute,
                state_path=_rate_limit_state_path(root, "fmp_rate_limit.json"),
                sleeper=sleeper,
            )
            lock_root = DATA_LAKE.root if root == BRONZE_US_CONSENSUS_DIR else root
            auth_disabled = False
            with SourceRefreshLock("fmp", data_lake_root=lock_root):
                for symbol_index, symbol in enumerate(resolved_symbols):
                    if auth_disabled:
                        fallback_symbols.add(symbol)
                        continue
                    jobs = [
                        (
                            root
                            / "fmp"
                            / "analyst-estimates"
                            / f"period={period}"
                            / f"snapshot_date={day}"
                            / f"ticker={symbol}.json",
                            "ANALYST_ESTIMATES",
                            period,
                            lambda period=period: _fmp_estimates_payload(
                                symbol,
                                period=period,
                                api_key=api_key,
                                limiter=limiter,
                                retries=fmp_retries,
                                http_get=request_get,
                                sleeper=sleeper,
                            ),
                        )
                        for period in ("annual", "quarter")
                    ]
                    jobs.append(
                        (
                            root
                            / "fmp"
                            / "price-target-summary"
                            / f"snapshot_date={day}"
                            / f"ticker={symbol}.json",
                            "PRICE_TARGET_SUMMARY",
                            None,
                            lambda: _fmp_price_target_payload(
                                symbol,
                                api_key=api_key,
                                limiter=limiter,
                                retries=fmp_retries,
                                http_get=request_get,
                                sleeper=sleeper,
                            ),
                        )
                    )
                    symbol_has_no_data = False
                    for path, dataset, period, fetch_payload in jobs:
                        existing = _read_valid_json(path)
                        if (
                            not force
                            and _is_complete_fmp_payload(
                                existing,
                                symbol=symbol,
                                dataset=dataset,
                                period=period,
                            )
                        ):
                            _increment_counts(counts, provider_counts, "skipped")
                            symbol_has_no_data = symbol_has_no_data or not _payload_has_rows(existing)
                            continue
                        try:
                            payload = fetch_payload()
                        except ProviderAuthenticationError:
                            provider_counts["auth_disabled"] = True
                            auth_disabled = True
                            fallback_symbols.update(resolved_symbols[symbol_index:])
                            print(
                                "[WARN] fmp consensus authentication or subscription access failed; "
                                "switching to the next provider",
                                flush=True,
                            )
                            break
                        except Exception as exc:
                            _increment_counts(counts, provider_counts, "failed")
                            fallback_symbols.add(symbol)
                            print(
                                f"[WARN] fmp consensus symbol={symbol}: {type(exc).__name__}",
                                flush=True,
                            )
                            continue
                        _write_json(path, payload)
                        _increment_counts(counts, provider_counts, "written")
                        if not _payload_has_rows(payload):
                            symbol_has_no_data = True
                            _increment_counts(counts, provider_counts, "no_data")
                    if symbol_has_no_data:
                        fallback_symbols.add(symbol)

    if "alpha-vantage" in resolved_sources:
        provider_counts = counts["providers"]["alpha-vantage"]
        try:
            api_key = _alpha_api_key()
        except ValueError:
            api_key = ""
            provider_counts["auth_disabled"] = True
            fallback_symbols.update(resolved_symbols)
            print(
                "[WARN] alpha-vantage consensus disabled: "
                "ALPHA_VANTAGE_API_KEY environment variable is not set",
                flush=True,
            )
        if api_key:
            auth_disabled = False
        limiter = RollingRateLimiter(
            max_calls_per_minute=alpha_max_calls_per_minute,
            state_path=_rate_limit_state_path(root, "alpha_vantage_rate_limit.json"),
            sleeper=sleeper,
        )
        lock_root = DATA_LAKE.root if root == BRONZE_US_CONSENSUS_DIR else root
        if api_key:
            with SourceRefreshLock("alpha-vantage", data_lake_root=lock_root):
                for symbol_index, symbol in enumerate(resolved_symbols):
                    if auth_disabled:
                        fallback_symbols.add(symbol)
                        continue
                    for dataset, function in ALPHA_VANTAGE_ENDPOINTS.items():
                        path = (
                            root
                            / "alpha-vantage"
                            / dataset
                            / f"snapshot_date={day}"
                            / f"ticker={symbol}.json"
                        )
                        existing = _read_valid_json(path)
                        if existing is not None and not force:
                            _increment_counts(counts, provider_counts, "skipped")
                            continue
                        try:
                            payload = _alpha_payload(
                                symbol,
                                function=function,
                                api_key=api_key,
                                limiter=limiter,
                                retries=alpha_retries,
                                http_get=request_get,
                                sleeper=sleeper,
                            )
                            _write_json(path, payload)
                            _increment_counts(counts, provider_counts, "written")
                            if not _payload_has_rows(payload):
                                _increment_counts(counts, provider_counts, "no_data")
                        except ProviderAuthenticationError:
                            provider_counts["auth_disabled"] = True
                            auth_disabled = True
                            fallback_symbols.update(resolved_symbols[symbol_index:])
                            print(
                                "[WARN] alpha-vantage consensus authentication or subscription "
                                "access failed; switching to yfinance",
                                flush=True,
                            )
                            break
                        except Exception as exc:  # isolated failures keep a universe backfill resumable.
                            _increment_counts(counts, provider_counts, "failed")
                            fallback_symbols.add(symbol)
                            print(
                                f"[WARN] alpha-vantage {dataset} symbol={symbol}: "
                                f"{type(exc).__name__}",
                                flush=True,
                            )

    if "yfinance" in resolved_sources:
        provider_counts = counts["providers"]["yfinance"]
        factory = yahoo_ticker_factory or _default_yahoo_ticker_factory
        for symbol in resolved_symbols:
            path = root / "yahoo" / f"snapshot_date={day}" / f"ticker={symbol}.json"
            existing = _read_valid_json(path)
            if existing is not None and not force:
                _increment_counts(counts, provider_counts, "skipped")
                continue
            try:
                payload = _yahoo_payload(symbol, factory)
                _write_json(path, payload)
                _increment_counts(counts, provider_counts, "written")
                if not _payload_has_rows(payload):
                    _increment_counts(counts, provider_counts, "no_data")
            except Exception as exc:
                _increment_counts(counts, provider_counts, "failed")
                print(f"[WARN] yahoo consensus symbol={symbol}: {type(exc).__name__}", flush=True)
    counts["fallback_symbols"] = len(fallback_symbols)
    return counts


def _download_finnworlds_company_ratings(
    symbols: list[str],
    *,
    day: str,
    date_from: str,
    date_to: str,
    force: bool,
    root: Path,
    counts: dict[str, Any],
    max_calls_per_minute: int,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> set[str]:
    """Download one complete Finnworlds company-ratings response per symbol."""
    if pd.Timestamp(date_from) > pd.Timestamp(date_to):
        raise ValueError("finnworlds_date_from must not be after finnworlds_date_to")

    provider_counts = counts["providers"]["finnworlds"]
    provider_counts.update(
        {
            "requests": 0,
            "billed_calls": 0,
            "interrupted": False,
        }
    )
    universe_hash = hashlib.sha256(
        "\n".join(sorted(symbols)).encode("utf-8")
    ).hexdigest()
    signature_payload = {
        "provider": "FINNWORLDS",
        "dataset": "COMPANY_RATINGS",
        "schema_version": FINNWORLDS_BACKFILL_SCHEMA_VERSION,
        "date_from": date_from,
        "date_to": date_to,
        "universe_hash": universe_hash,
    }
    run_signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    checkpoint_path = _finnworlds_checkpoint_path(root, run_signature)
    checkpoint = _load_finnworlds_checkpoint(
        checkpoint_path,
        signature_payload=signature_payload,
        run_signature=run_signature,
        symbols=symbols,
        reset=force,
    )
    provider_counts["run_signature"] = run_signature
    provider_counts["checkpoint_path"] = str(checkpoint_path)

    completed: set[str] = set()
    no_data: set[str] = set()
    if not force:
        for symbol in symbols:
            existing = _read_valid_json(
                _finnworlds_bronze_path(root, day=day, symbol=symbol)
            )
            if not _is_complete_finnworlds_payload(
                existing,
                symbol=symbol,
                date_from=date_from,
                date_to=date_to,
            ):
                continue
            if _payload_has_rows(existing):
                completed.add(symbol)
            else:
                no_data.add(symbol)

    failed = set(str(value) for value in checkpoint.get("failed", []))
    failed.difference_update(completed | no_data)
    _update_finnworlds_checkpoint(
        checkpoint,
        symbols=symbols,
        completed=completed,
        no_data=no_data,
        failed=failed,
        status="running",
    )
    _write_json(checkpoint_path, checkpoint)

    remaining = len(symbols) - len(completed) - len(no_data)
    estimated_seconds = (
        (remaining / max(1, int(max_calls_per_minute))) * 60.0
        if remaining
        else 0.0
    )
    print(
        "[INFO] finnworlds target-price backfill "
        f"total={len(symbols):,}, completed={len(completed):,}, "
        f"no_data={len(no_data):,}, failed={len(failed):,}, "
        f"remaining={remaining:,}, "
        f"estimated_remaining_minutes={estimated_seconds / 60.0:.1f}, "
        f"estimated_billed_calls={remaining * FINNWORLDS_BILLED_CALL_MULTIPLIER:,}",
        flush=True,
    )

    api_key = os.getenv("FINNWORLDS_API_KEY", "").strip()
    if not api_key:
        provider_counts["auth_disabled"] = True
        provider_counts["interrupted"] = True
        checkpoint["status"] = "interrupted_auth"
        checkpoint["last_error"] = "FINNWORLDS_API_KEY is not set"
        _update_finnworlds_checkpoint(
            checkpoint,
            symbols=symbols,
            completed=completed,
            no_data=no_data,
            failed=failed,
            status="interrupted_auth",
        )
        _write_json(checkpoint_path, checkpoint)
        print(
            "[WARN] finnworlds consensus disabled: "
            "FINNWORLDS_API_KEY environment variable is not set",
            flush=True,
        )
        return set(symbols) - completed - no_data

    limiter = FinnworldsRollingRateLimiter(
        max_calls_per_minute=max_calls_per_minute,
        state_path=_rate_limit_state_path(root, "finnworlds_rate_limit.json"),
        sleeper=sleeper,
    )
    lock_root = DATA_LAKE.root if root == BRONZE_US_CONSENSUS_DIR else root
    auth_interrupted = False

    def record_request() -> None:
        provider_counts["requests"] = int(provider_counts["requests"]) + 1
        provider_counts["billed_calls"] = (
            int(provider_counts["billed_calls"])
            + FINNWORLDS_BILLED_CALL_MULTIPLIER
        )
        checkpoint["http_requests"] = int(checkpoint.get("http_requests", 0)) + 1
        checkpoint["billed_calls"] = int(checkpoint.get("billed_calls", 0)) + (
            FINNWORLDS_BILLED_CALL_MULTIPLIER
        )
        checkpoint["updated_at"] = _utc_now()
        _write_json(checkpoint_path, checkpoint)

    try:
        with SourceRefreshLock("finnworlds", data_lake_root=lock_root):
            _cleanup_finnworlds_temporary_files(root, day=day)
            for symbol_index, symbol in enumerate(symbols):
                if symbol in completed or symbol in no_data:
                    _increment_counts(counts, provider_counts, "skipped")
                    continue
                failed.discard(symbol)
                path = _finnworlds_bronze_path(root, day=day, symbol=symbol)
                try:
                    response_payload = _finnworlds_request(
                        symbol,
                        date_from=date_from,
                        date_to=date_to,
                        api_key=api_key,
                        limiter=limiter,
                        retries=retries,
                        http_get=http_get,
                        sleeper=sleeper,
                        on_request=record_request,
                    )
                    envelope = {
                        "symbol": symbol,
                        "provider": "FINNWORLDS",
                        "dataset": "COMPANY_RATINGS",
                        "schema_version": FINNWORLDS_BACKFILL_SCHEMA_VERSION,
                        "date_from": date_from,
                        "date_to": date_to,
                        "snapshot_date": day,
                        "complete": True,
                        "data": response_payload,
                    }
                    _write_json(path, envelope)
                    if not _is_complete_finnworlds_payload(
                        _read_valid_json(path),
                        symbol=symbol,
                        date_from=date_from,
                        date_to=date_to,
                    ):
                        raise RuntimeError("Finnworlds bronze verification failed")
                    _increment_counts(counts, provider_counts, "written")
                    if _payload_has_rows(envelope):
                        completed.add(symbol)
                    else:
                        no_data.add(symbol)
                        _increment_counts(counts, provider_counts, "no_data")
                except ProviderAuthenticationError:
                    provider_counts["auth_disabled"] = True
                    provider_counts["interrupted"] = True
                    auth_interrupted = True
                    checkpoint["last_error"] = "ProviderAuthenticationError"
                    _update_finnworlds_checkpoint(
                        checkpoint,
                        symbols=symbols,
                        completed=completed,
                        no_data=no_data,
                        failed=failed,
                        status="interrupted_auth",
                    )
                    _write_json(checkpoint_path, checkpoint)
                    print(
                        "[WARN] finnworlds authentication or subscription access failed; "
                        "backfill checkpoint saved",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    failed.add(symbol)
                    _increment_counts(counts, provider_counts, "failed")
                    checkpoint["last_error"] = f"{symbol}:{type(exc).__name__}"
                    print(
                        f"[WARN] finnworlds consensus symbol={symbol}: "
                        f"{type(exc).__name__}",
                        flush=True,
                    )
                _update_finnworlds_checkpoint(
                    checkpoint,
                    symbols=symbols,
                    completed=completed,
                    no_data=no_data,
                    failed=failed,
                    status="running",
                )
                _write_json(checkpoint_path, checkpoint)
    except KeyboardInterrupt:
        provider_counts["interrupted"] = True
        _update_finnworlds_checkpoint(
            checkpoint,
            symbols=symbols,
            completed=completed,
            no_data=no_data,
            failed=failed,
            status="interrupted",
        )
        _write_json(checkpoint_path, checkpoint)
        raise

    finished = len(completed) + len(no_data) == len(symbols)
    final_status = (
        "complete"
        if finished
        else "interrupted_auth"
        if auth_interrupted
        else "partial"
    )
    _update_finnworlds_checkpoint(
        checkpoint,
        symbols=symbols,
        completed=completed,
        no_data=no_data,
        failed=failed,
        status=final_status,
    )
    _write_json(checkpoint_path, checkpoint)
    remaining_symbols = set(symbols) - completed - no_data
    print(
        "[DONE] finnworlds target-price backfill "
        f"status={final_status}, total={len(symbols):,}, "
        f"completed={len(completed):,}, "
        f"no_data={len(no_data):,}, failed={len(failed):,}, "
        f"remaining={len(remaining_symbols):,}, "
        f"estimated_remaining_minutes="
        f"{len(remaining_symbols) / max(1, max_calls_per_minute):.1f}, "
        f"run_http_requests={provider_counts['requests']:,}, "
        f"run_billed_calls={provider_counts['billed_calls']:,}, "
        f"cumulative_http_requests={int(checkpoint.get('http_requests', 0)):,}, "
        f"cumulative_billed_calls={int(checkpoint.get('billed_calls', 0)):,}",
        flush=True,
    )
    return remaining_symbols


def _finnworlds_request(
    symbol: str,
    *,
    date_from: str,
    date_to: str,
    api_key: str,
    limiter: FinnworldsRollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
    on_request: Callable[[], None],
) -> dict[str, Any]:
    attempts = max(0, int(retries)) + 1
    for attempt in range(attempts):
        limiter.acquire()
        on_request()
        try:
            response = http_get(
                FINNWORLDS_COMPANY_RATINGS_URL,
                params={
                    "key": api_key,
                    "ticker": symbol,
                    "date_from": date_from,
                    "date_to": date_to,
                },
                headers={"accept": "application/json"},
                timeout=(10, 60),
            )
        except Exception:
            if attempt + 1 == attempts:
                raise RuntimeError(
                    f"Finnworlds request failed for symbol={symbol}"
                ) from None
            sleeper(min(60.0, 2.0**attempt))
            continue

        status = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except Exception:
            payload = None
        if _is_authentication_failure(status, payload):
            raise ProviderAuthenticationError("Finnworlds authorization failed")

        api_status = payload.get("status", {}) if isinstance(payload, dict) else {}
        try:
            api_code = int(api_status.get("code", 200))
        except (TypeError, ValueError):
            api_code = 200
        limited = status == 429 or api_code == 429
        valid = _is_valid_finnworlds_response(payload)
        if status < 400 and not limited and valid:
            return payload
        if _is_finnworlds_no_data_response(payload):
            return payload if isinstance(payload, dict) else {}
        if attempt + 1 == attempts:
            if limited:
                raise FinnworldsRateLimitError("Finnworlds rate limit exceeded")
            raise RuntimeError(f"Finnworlds request failed for symbol={symbol}")
        sleeper(_retry_delay(response, attempt, rate_limited=limited))
    raise AssertionError("unreachable")


def _is_valid_finnworlds_response(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = payload.get("status", {})
    if isinstance(status, dict) and status.get("code") not in (None, 200, "200"):
        return False
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    output = result.get("output")
    return isinstance(output, dict)


def _is_finnworlds_no_data_response(payload: Any) -> bool:
    text = _provider_error_text(payload).lower()
    return any(
        marker in text
        for marker in ("no data", "not found", "no result", "no ratings")
    )


def _finnworlds_response_has_data(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result", {})
    output = result.get("output", {}) if isinstance(result, dict) else {}
    if not isinstance(output, dict):
        return False
    analysts = output.get("analysts")
    if isinstance(analysts, list) and any(isinstance(row, dict) for row in analysts):
        return True
    consensus = output.get("analyst_consensus")
    return isinstance(consensus, dict) and any(
        value not in (None, "", "None", [], {})
        for value in consensus.values()
    )


def _finnworlds_bronze_path(root: Path, *, day: str, symbol: str) -> Path:
    return (
        root
        / "finnworlds"
        / "company-ratings"
        / f"snapshot_date={day}"
        / f"ticker={symbol}.json"
    )


def _cleanup_finnworlds_temporary_files(root: Path, *, day: str) -> None:
    snapshot_dir = (
        root
        / "finnworlds"
        / "company-ratings"
        / f"snapshot_date={day}"
    )
    if not snapshot_dir.exists():
        return
    for path in snapshot_dir.glob(".consensus-*.json"):
        try:
            path.unlink()
        except OSError:
            pass


def _finnworlds_checkpoint_path(root: Path, run_signature: str) -> Path:
    if root == BRONZE_US_CONSENSUS_DIR:
        return DATA_LAKE.meta(
            "consensus",
            f"finnworlds_backfill_{run_signature}.json",
        )
    return (
        root
        / "meta"
        / "consensus"
        / f"finnworlds_backfill_{run_signature}.json"
    )


def _load_finnworlds_checkpoint(
    path: Path,
    *,
    signature_payload: dict[str, Any],
    run_signature: str,
    symbols: list[str],
    reset: bool,
) -> dict[str, Any]:
    existing = None if reset else _read_valid_json(path)
    if (
        isinstance(existing, dict)
        and existing.get("run_signature") == run_signature
        and existing.get("signature") == signature_payload
    ):
        return existing
    now = _utc_now()
    return {
        "run_signature": run_signature,
        "signature": signature_payload,
        "status": "pending",
        "symbols": len(symbols),
        "completed": [],
        "no_data": [],
        "failed": [],
        "pending": list(symbols),
        "http_requests": 0,
        "billed_calls": 0,
        "last_error": "",
        "started_at": now,
        "updated_at": now,
    }


def _update_finnworlds_checkpoint(
    checkpoint: dict[str, Any],
    *,
    symbols: list[str],
    completed: set[str],
    no_data: set[str],
    failed: set[str],
    status: str,
) -> None:
    successful = completed | no_data
    checkpoint["status"] = status
    checkpoint["symbols"] = len(symbols)
    checkpoint["completed"] = sorted(completed)
    checkpoint["no_data"] = sorted(no_data)
    checkpoint["failed"] = sorted(failed - successful)
    checkpoint["pending"] = [
        symbol
        for symbol in symbols
        if symbol not in successful and symbol not in failed
    ]
    checkpoint["updated_at"] = _utc_now()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _increment_counts(
    totals: dict[str, Any],
    provider_counts: dict[str, Any],
    key: str,
) -> None:
    totals[key] = int(totals.get(key, 0)) + 1
    provider_counts[key] = int(provider_counts.get(key, 0)) + 1


def _rate_limit_state_path(root: Path, filename: str) -> Path | None:
    if root == BRONZE_US_CONSENSUS_DIR:
        return None
    return root / "meta" / "consensus" / filename


def _fmp_estimates_payload(
    symbol: str,
    *,
    period: str,
    api_key: str,
    limiter: FmpRollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    page = 0
    pages = 0
    while True:
        payload = _fmp_request(
            "analyst-estimates",
            params={
                "symbol": symbol,
                "period": period,
                "page": page,
                "limit": FMP_PAGE_LIMIT,
            },
            api_key=api_key,
            limiter=limiter,
            retries=retries,
            http_get=http_get,
            sleeper=sleeper,
        )
        pages += 1
        rows.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < FMP_PAGE_LIMIT:
            break
        page += 1
    return {
        "symbol": symbol,
        "provider": "FMP",
        "dataset": "ANALYST_ESTIMATES",
        "period": period,
        "pages": pages,
        "complete": True,
        "data": rows,
    }


def _fmp_price_target_payload(
    symbol: str,
    *,
    api_key: str,
    limiter: FmpRollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    payload = _fmp_request(
        "price-target-summary",
        params={"symbol": symbol},
        api_key=api_key,
        limiter=limiter,
        retries=retries,
        http_get=http_get,
        sleeper=sleeper,
    )
    return {
        "symbol": symbol,
        "provider": "FMP",
        "dataset": "PRICE_TARGET_SUMMARY",
        "complete": True,
        "data": [item for item in payload if isinstance(item, dict)],
    }


def _fmp_request(
    endpoint: str,
    *,
    params: dict[str, Any],
    api_key: str,
    limiter: FmpRollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> list[Any]:
    attempts = max(0, int(retries)) + 1
    url = f"{FMP_BASE_URL}/{FMP_ENDPOINTS[endpoint]}"
    for attempt in range(attempts):
        limiter.acquire()
        try:
            response = http_get(
                url,
                params=params,
                headers={"apikey": api_key},
                timeout=(10, 60),
            )
        except Exception:
            if attempt + 1 == attempts:
                raise RuntimeError(
                    f"FMP request failed for endpoint={endpoint}"
                ) from None
            sleeper(min(60.0, 2.0**attempt))
            continue
        status = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except Exception as exc:
            payload = None
            parse_error = exc
        else:
            parse_error = None
        if _is_authentication_failure(status, payload):
            raise ProviderAuthenticationError(f"FMP authorization failed for endpoint={endpoint}")
        if status == 429:
            if attempt + 1 == attempts:
                raise FmpRateLimitError(f"FMP rate limit exceeded for endpoint={endpoint}")
            sleeper(_retry_delay(response, attempt, rate_limited=True))
            continue
        failed = (
            status >= 400
            or parse_error is not None
            or not isinstance(payload, list)
        )
        if not failed:
            return payload
        if attempt + 1 == attempts:
            raise RuntimeError(f"FMP request failed for endpoint={endpoint}")
        sleeper(_retry_delay(response, attempt, rate_limited=False))
    raise AssertionError("unreachable")


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
        except Exception:
            if attempt + 1 == attempts:
                raise AlphaVantageRateLimitError(
                    f"Alpha Vantage request failed for function={function}, symbol={symbol}"
                ) from None
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
        if _is_authentication_failure(status, payload):
            raise ProviderAuthenticationError(
                f"Alpha Vantage authorization failed for function={function}"
            )
        limited = status == 429 or (isinstance(payload, dict) and any(key in payload for key in ("Note", "Information")))
        failed = status >= 400 or parse_error is not None or (isinstance(payload, dict) and "Error Message" in payload)
        if not limited and not failed and isinstance(payload, dict):
            return payload
        if attempt + 1 == attempts:
            raise AlphaVantageRateLimitError(f"Alpha Vantage request failed for function={function}, symbol={symbol}")
        sleeper(_retry_delay(response, attempt, rate_limited=limited, base=60.0))
    raise AssertionError("unreachable")


def _retry_delay(
    response: Any,
    attempt: int,
    *,
    rate_limited: bool,
    base: float = 2.0,
) -> float:
    retry_after = getattr(response, "headers", {}).get("Retry-After")
    try:
        if retry_after:
            return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(300.0, base * (2**attempt)))


def _is_authentication_failure(status: int, payload: Any) -> bool:
    if status in {401, 403}:
        return True
    text = _provider_error_text(payload).lower()
    if not text:
        return False
    key_markers = (
        "invalid api key",
        "invalid apikey",
        "api key is invalid",
        "api key has expired",
        "expired api key",
        "missing api key",
        "unauthorized",
        "access denied",
        "subscription",
        "premium endpoint",
        "entitlement",
        "not available under your current subscription",
    )
    return any(marker in text for marker in key_markers)


def _provider_error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    values = []
    for key in ("Error Message", "error", "message", "Information", "Note"):
        value = payload.get(key)
        if value is not None:
            values.append(str(value))
    status = payload.get("status")
    if isinstance(status, dict):
        for key in ("message", "details", "error"):
            value = status.get(key)
            if value is not None:
                values.append(str(value))
    return " ".join(values)


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
        "operating_income_estimate": "get_operating_income_estimate",
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


def _parse_sources(sources: Iterable[str] | None) -> tuple[str, ...]:
    aliases = {
        "all": US_CONSENSUS_SOURCE_PRIORITY,
        "finnworlds": ("finnworlds",),
        "finnworld": ("finnworlds",),
        "fmp": ("fmp",),
        "alpha": ("alpha-vantage",),
        "alpha-vantage": ("alpha-vantage",),
        "yahoo": ("yfinance",),
        "yfinance": ("yfinance",),
    }
    values = sources or US_CONSENSUS_SOURCE_PRIORITY
    selected: set[str] = set()
    for item in values:
        key = str(item).strip().lower()
        if not key:
            continue
        if key not in aliases:
            raise ValueError(f"unknown US consensus source: {item}")
        selected.update(aliases[key])
    return tuple(source for source in US_CONSENSUS_SOURCE_PRIORITY if source in selected)


def _resolve_symbols(
    symbols: Iterable[str] | None,
    *,
    refresh_universe: bool = True,
) -> list[str]:
    if symbols is not None:
        return list(dict.fromkeys(normalize_yfinance_ticker(value) for value in symbols if str(value).strip()))
    if refresh_universe:
        download_us_equity_universe()
    path = FILTERED_UNIVERSE_PATH
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


def _read_valid_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_complete_fmp_payload(
    payload: dict[str, Any] | None,
    *,
    symbol: str,
    dataset: str,
    period: str | None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("complete") is not True
        or payload.get("provider") != "FMP"
        or payload.get("dataset") != dataset
        or payload.get("symbol") != symbol
        or not isinstance(payload.get("data"), list)
    ):
        return False
    if dataset == "ANALYST_ESTIMATES":
        pages = payload.get("pages")
        return (
            payload.get("period") == period
            and isinstance(pages, int)
            and not isinstance(pages, bool)
            and pages >= 1
        )
    return dataset == "PRICE_TARGET_SUMMARY"


def _is_complete_finnworlds_payload(
    payload: dict[str, Any] | None,
    *,
    symbol: str,
    date_from: str,
    date_to: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("complete") is True
        and payload.get("provider") == "FINNWORLDS"
        and payload.get("dataset") == "COMPANY_RATINGS"
        and payload.get("schema_version") == FINNWORLDS_BACKFILL_SCHEMA_VERSION
        and payload.get("symbol") == symbol
        and payload.get("date_from") == date_from
        and payload.get("date_to") == date_to
        and isinstance(payload.get("data"), dict)
        and (
            _is_valid_finnworlds_response(payload.get("data"))
            or _is_finnworlds_no_data_response(payload.get("data"))
        )
    )


def _payload_has_rows(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("provider") == "FINNWORLDS":
        return _finnworlds_response_has_data(payload.get("data"))
    if payload.get("provider") == "FMP":
        return bool(payload.get("data"))
    data = payload.get("data")
    if isinstance(data, list):
        return bool(data)
    if isinstance(data, dict):
        return any(bool(value) for value in data.values())
    ignored = {"symbol", "provider", "dataset", "errors"}
    return any(value not in (None, "", [], {}) for key, value in payload.items() if key not in ignored)
