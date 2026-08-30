from __future__ import annotations

"""US earnings-call transcript bronze collector.

FMP is queried first. Alpha Vantage is only queried for quarters that do not
have a usable FMP transcript. Provider payloads are preserved inside a small
metadata envelope so interrupted full-universe runs can be resumed safely.
"""

from datetime import date, datetime, timezone
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
from engine.extractors._internal.us_consensus import (
    DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    DEFAULT_ALPHA_RETRIES,
    DEFAULT_FMP_MAX_CALLS_PER_MINUTE,
    DEFAULT_FMP_RETRIES,
    AlphaVantageRateLimitError,
    FmpRateLimitError,
    FmpRollingRateLimiter,
    ProviderAuthenticationError,
    RollingRateLimiter,
)
from engine.extractors._internal.yfinance_market_prices import (
    FILTERED_UNIVERSE_PATH,
    download_us_equity_universe,
    normalize_yfinance_ticker,
)


FMP_BASE_URL = "https://financialmodelingprep.com/stable"
ALPHA_VANTAGE_QUERY_URL = "https://www.alphavantage.co/query"
FMP_TRANSCRIPT_LIST_ENDPOINT = "earnings-transcript-list"
FMP_TRANSCRIPT_DATES_ENDPOINT = "earning-call-transcript-dates"
FMP_TRANSCRIPT_ENDPOINT = "earning-call-transcript"
ALPHA_VANTAGE_TRANSCRIPT_FUNCTION = "EARNINGS_CALL_TRANSCRIPT"
US_EARNINGS_CALL_SOURCE_PRIORITY = ("fmp", "alpha-vantage")
BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR = DATA_LAKE.bronze(
    "earnings-call-transcripts"
)
ALPHA_VANTAGE_FIRST_QUARTER = "2010Q1"
ALPHA_VANTAGE_CONNECT_TIMEOUT_SECONDS = 10.0
ALPHA_VANTAGE_READ_TIMEOUT_SECONDS = 15.0
ALPHA_VANTAGE_TOTAL_TIMEOUT_SECONDS = 75.0
SCHEMA_VERSION = 1


def download_us_earnings_call_transcripts(
    *,
    symbols: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    start_quarter: str | None = None,
    end_quarter: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    force: bool = False,
    refresh_universe: bool = True,
    refresh_recent_quarters: int = 2,
    fmp_max_calls_per_minute: int = DEFAULT_FMP_MAX_CALLS_PER_MINUTE,
    fmp_retries: int = DEFAULT_FMP_RETRIES,
    alpha_max_calls_per_minute: int = DEFAULT_ALPHA_MAX_CALLS_PER_MINUTE,
    alpha_retries: int = DEFAULT_ALPHA_RETRIES,
    alpha_retry_passes: int = 1,
    output_root: str | Path = BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR,
    http_get: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Download all available US earnings-call transcripts into bronze.

    With no explicit ``symbols``, the Nasdaq Trader US common-equity universe
    is used. FMP's catalog and per-symbol date endpoint identify its available
    transcripts. Alpha Vantage then checks uncovered quarters from 2010Q1
    onward. Existing complete snapshots are skipped; recent no-data snapshots
    are periodically rechecked.
    """
    if int(offset) < 0:
        raise ValueError("offset must be zero or greater")
    if limit is not None and int(limit) < 0:
        raise ValueError("limit must be zero or greater")
    if int(refresh_recent_quarters) < 0:
        raise ValueError("refresh_recent_quarters must be zero or greater")
    if int(alpha_retry_passes) < 0:
        raise ValueError("alpha_retry_passes must be zero or greater")

    resolved_sources = _parse_sources(sources)
    resolved_symbols = _resolve_symbols(
        symbols,
        refresh_universe=refresh_universe,
    )
    selected_symbols = resolved_symbols[int(offset) :]
    if limit is not None:
        selected_symbols = selected_symbols[: int(limit)]

    requested_start = _parse_quarter(start_quarter) if start_quarter else None
    requested_end = _parse_quarter(end_quarter) if end_quarter else _current_quarter()
    if requested_start is not None and requested_start > requested_end:
        raise ValueError("start_quarter must not be after end_quarter")
    alpha_start = max(
        requested_start or _parse_quarter(ALPHA_VANTAGE_FIRST_QUARTER),
        _parse_quarter(ALPHA_VANTAGE_FIRST_QUARTER),
    )
    alpha_quarters = list(_iter_quarters(alpha_start, requested_end))
    recent_cutoff = _shift_quarter(
        _current_quarter(),
        -(max(0, int(refresh_recent_quarters)) - 1),
    )

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    request_get = http_get or requests.get
    explicit_symbols = symbols is not None
    counts = _new_counts(
        symbols=len(selected_symbols),
        alpha_quarters=len(alpha_quarters),
        sources=resolved_sources,
    )

    fmp_key = os.getenv("FMP_API_KEY", "").strip() if "fmp" in resolved_sources else ""
    alpha_key = (
        os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
        if "alpha-vantage" in resolved_sources
        else ""
    )
    fmp_limiter = FmpRollingRateLimiter(
        max_calls_per_minute=fmp_max_calls_per_minute,
        state_path=_rate_limit_state_path(root, "fmp_rate_limit.json"),
        sleeper=sleeper,
    )
    alpha_limiter = RollingRateLimiter(
        max_calls_per_minute=alpha_max_calls_per_minute,
        state_path=_rate_limit_state_path(root, "alpha_vantage_rate_limit.json"),
        sleeper=sleeper,
    )

    fmp_catalog_symbols: set[str] | None = None
    alpha_retry_queue: list[tuple[str, int, int]] = []
    fmp_disabled = False
    alpha_disabled = False
    if "fmp" in resolved_sources and not fmp_key:
        fmp_disabled = True
        counts["providers"]["fmp"]["auth_disabled"] = True
        print(
            "[WARN] FMP transcripts disabled: FMP_API_KEY environment variable is not set",
            flush=True,
        )
    if "alpha-vantage" in resolved_sources and not alpha_key:
        alpha_disabled = True
        counts["providers"]["alpha-vantage"]["auth_disabled"] = True
        print(
            "[WARN] Alpha Vantage transcripts disabled: "
            "ALPHA_VANTAGE_API_KEY environment variable is not set",
            flush=True,
        )

    lock_root = (
        DATA_LAKE.root
        if root.resolve() == BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR.resolve()
        else root
    )
    with SourceRefreshLock("earnings-call-transcripts", data_lake_root=lock_root):
        if "fmp" in resolved_sources and not fmp_disabled and not explicit_symbols:
            try:
                catalog = _fmp_request(
                    FMP_TRANSCRIPT_LIST_ENDPOINT,
                    params={},
                    api_key=fmp_key,
                    limiter=fmp_limiter,
                    retries=fmp_retries,
                    http_get=request_get,
                    sleeper=sleeper,
                )
                counts["providers"]["fmp"]["requests"] += 1
                _write_json(
                    root / "fmp" / "catalog" / "earnings-transcript-list.json",
                    _envelope(
                        provider="FMP",
                        dataset="EARNINGS_TRANSCRIPT_LIST",
                        status="ok",
                        data=catalog,
                    ),
                )
                fmp_catalog_symbols = _catalog_symbols(catalog)
            except ProviderAuthenticationError:
                fmp_disabled = True
                counts["providers"]["fmp"]["auth_disabled"] = True
                print(
                    "[WARN] FMP transcript catalog is not authorized; "
                    "using Alpha Vantage fallback",
                    flush=True,
                )
            except Exception as exc:
                counts["failed"] += 1
                counts["providers"]["fmp"]["failed"] += 1
                print(
                    f"[WARN] FMP transcript catalog failed: {type(exc).__name__}; "
                    "querying per-symbol dates",
                    flush=True,
                )

        for symbol_index, symbol in enumerate(selected_symbols, start=int(offset)):
            fmp_covered: set[tuple[int, int]] = {
                (year, quarter)
                for year, quarter in alpha_quarters
                if _is_usable_transcript_snapshot(
                    _read_json(
                        _transcript_path(root, "fmp", symbol, year, quarter)
                    ),
                    provider="FMP",
                    symbol=symbol,
                    year=year,
                    quarter=quarter,
                )
            }
            fmp_events: set[tuple[int, int]] = set()
            should_query_fmp = (
                "fmp" in resolved_sources
                and not fmp_disabled
                and (fmp_catalog_symbols is None or symbol in fmp_catalog_symbols)
            )

            if should_query_fmp:
                try:
                    dates_payload = _fmp_request(
                        FMP_TRANSCRIPT_DATES_ENDPOINT,
                        params={"symbol": symbol},
                        api_key=fmp_key,
                        limiter=fmp_limiter,
                        retries=fmp_retries,
                        http_get=request_get,
                        sleeper=sleeper,
                    )
                    counts["providers"]["fmp"]["requests"] += 1
                    _write_json(
                        root / "fmp" / "dates" / f"ticker={symbol}.json",
                        _envelope(
                            provider="FMP",
                            dataset="EARNING_CALL_TRANSCRIPT_DATES",
                            status="ok",
                            symbol=symbol,
                            data=dates_payload,
                        ),
                    )
                    fmp_events = _fmp_event_quarters(
                        dates_payload,
                        start=requested_start,
                        end=requested_end,
                    )
                except ProviderAuthenticationError:
                    fmp_disabled = True
                    counts["providers"]["fmp"]["auth_disabled"] = True
                    print(
                        "[WARN] FMP transcript access is not authorized; "
                        "remaining symbols will use Alpha Vantage",
                        flush=True,
                    )
                except Exception as exc:
                    _record_failure(counts, "fmp")
                    print(
                        f"[WARN] FMP transcript dates symbol={symbol}: "
                        f"{type(exc).__name__}",
                        flush=True,
                    )

            for year, quarter in sorted(fmp_events):
                path = _transcript_path(root, "fmp", symbol, year, quarter)
                cached = _read_json(path)
                if _cache_is_fresh(
                    cached,
                    provider="FMP",
                    symbol=symbol,
                    year=year,
                    quarter=quarter,
                    force=force,
                    recent_cutoff=recent_cutoff,
                ):
                    _record_skip(counts, "fmp")
                    if cached.get("status") == "ok":
                        fmp_covered.add((year, quarter))
                    continue
                try:
                    payload = _fmp_request(
                        FMP_TRANSCRIPT_ENDPOINT,
                        params={"symbol": symbol, "year": year, "quarter": quarter},
                        api_key=fmp_key,
                        limiter=fmp_limiter,
                        retries=fmp_retries,
                        http_get=request_get,
                        sleeper=sleeper,
                    )
                    counts["providers"]["fmp"]["requests"] += 1
                    status = "ok" if _payload_has_transcript(payload) else "no_data"
                    if status == "no_data" and _is_usable_transcript_snapshot(
                        cached,
                        provider="FMP",
                        symbol=symbol,
                        year=year,
                        quarter=quarter,
                    ):
                        _record_skip(counts, "fmp")
                        fmp_covered.add((year, quarter))
                    else:
                        _write_json(
                            path,
                            _envelope(
                                provider="FMP",
                                dataset="EARNING_CALL_TRANSCRIPT",
                                status=status,
                                symbol=symbol,
                                year=year,
                                quarter=quarter,
                                data=payload,
                            ),
                        )
                        _record_write(counts, "fmp", status=status)
                        if status == "ok":
                            fmp_covered.add((year, quarter))
                except ProviderAuthenticationError:
                    fmp_disabled = True
                    counts["providers"]["fmp"]["auth_disabled"] = True
                    print(
                        "[WARN] FMP transcript access is not authorized; "
                        "switching to Alpha Vantage",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    _record_failure(counts, "fmp")
                    print(
                        f"[WARN] FMP transcript symbol={symbol} "
                        f"quarter={_format_quarter((year, quarter))}: "
                        f"{type(exc).__name__}",
                        flush=True,
                    )

            if "alpha-vantage" in resolved_sources and not alpha_disabled:
                for year, quarter in alpha_quarters:
                    if (year, quarter) in fmp_covered:
                        counts["priority_skipped"] += 1
                        continue
                    path = _transcript_path(
                        root,
                        "alpha-vantage",
                        symbol,
                        year,
                        quarter,
                    )
                    cached = _read_json(path)
                    if _cache_is_fresh(
                        cached,
                        provider="ALPHA_VANTAGE",
                        symbol=symbol,
                        year=year,
                        quarter=quarter,
                        force=force,
                        recent_cutoff=recent_cutoff,
                    ):
                        _record_skip(counts, "alpha-vantage")
                        continue
                    try:
                        payload = _alpha_request(
                            symbol,
                            year=year,
                            quarter=quarter,
                            api_key=alpha_key,
                            limiter=alpha_limiter,
                            retries=alpha_retries,
                            http_get=request_get,
                            sleeper=sleeper,
                        )
                        counts["providers"]["alpha-vantage"]["requests"] += 1
                        status = "ok" if _payload_has_transcript(payload) else "no_data"
                        if status == "no_data" and _is_usable_transcript_snapshot(
                            cached,
                            provider="ALPHA_VANTAGE",
                            symbol=symbol,
                            year=year,
                            quarter=quarter,
                        ):
                            _record_skip(counts, "alpha-vantage")
                        else:
                            _write_json(
                                path,
                                _envelope(
                                    provider="ALPHA_VANTAGE",
                                    dataset="EARNINGS_CALL_TRANSCRIPT",
                                    status=status,
                                    symbol=symbol,
                                    year=year,
                                    quarter=quarter,
                                    data=payload,
                                ),
                            )
                            _record_write(counts, "alpha-vantage", status=status)
                    except ProviderAuthenticationError:
                        alpha_disabled = True
                        counts["providers"]["alpha-vantage"]["auth_disabled"] = True
                        print(
                            "[WARN] Alpha Vantage transcript access is not authorized; "
                            "stopping Alpha Vantage requests",
                            flush=True,
                        )
                        break
                    except Exception as exc:
                        _record_failure(counts, "alpha-vantage")
                        alpha_retry_queue.append((symbol, year, quarter))
                        print(
                            f"[WARN] Alpha Vantage transcript symbol={symbol} "
                            f"quarter={_format_quarter((year, quarter))}: "
                            f"{type(exc).__name__}",
                            flush=True,
                        )

            print(
                f"earnings-call transcripts symbol={symbol} "
                f"index={symbol_index} written={counts['written']} "
                f"failed={counts['failed']}",
                flush=True,
            )

        pending_alpha_retries = alpha_retry_queue
        for retry_pass in range(1, int(alpha_retry_passes) + 1):
            if not pending_alpha_retries or alpha_disabled:
                break
            print(
                "earnings-call transcripts alpha retry "
                f"pass={retry_pass} pending={len(pending_alpha_retries)}",
                flush=True,
            )
            next_pending: list[tuple[str, int, int]] = []
            for retry_index, (symbol, year, quarter) in enumerate(
                pending_alpha_retries
            ):
                path = _transcript_path(
                    root,
                    "alpha-vantage",
                    symbol,
                    year,
                    quarter,
                )
                try:
                    payload = _alpha_request(
                        symbol,
                        year=year,
                        quarter=quarter,
                        api_key=alpha_key,
                        limiter=alpha_limiter,
                        retries=alpha_retries,
                        http_get=request_get,
                        sleeper=sleeper,
                    )
                    counts["providers"]["alpha-vantage"]["requests"] += 1
                    status = "ok" if _payload_has_transcript(payload) else "no_data"
                    _write_json(
                        path,
                        _envelope(
                            provider="ALPHA_VANTAGE",
                            dataset="EARNINGS_CALL_TRANSCRIPT",
                            status=status,
                            symbol=symbol,
                            year=year,
                            quarter=quarter,
                            data=payload,
                        ),
                    )
                    _record_write(counts, "alpha-vantage", status=status)
                    _record_recovery(counts, "alpha-vantage")
                    print(
                        f"earnings-call transcripts alpha retry recovered symbol={symbol} "
                        f"quarter={_format_quarter((year, quarter))}",
                        flush=True,
                    )
                except ProviderAuthenticationError:
                    alpha_disabled = True
                    counts["providers"]["alpha-vantage"]["auth_disabled"] = True
                    next_pending.extend(pending_alpha_retries[retry_index:])
                    print(
                        "[WARN] Alpha Vantage transcript access is not authorized; "
                        "stopping retry pass",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    next_pending.append((symbol, year, quarter))
                    print(
                        f"[WARN] Alpha Vantage transcript retry symbol={symbol} "
                        f"quarter={_format_quarter((year, quarter))}: "
                        f"{type(exc).__name__}",
                        flush=True,
                    )
            pending_alpha_retries = next_pending

    return counts


def audit_us_earnings_call_transcripts(
    *,
    symbols: Iterable[str] | None = None,
    start_quarter: str = ALPHA_VANTAGE_FIRST_QUARTER,
    end_quarter: str | None = None,
    output_root: str | Path = BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR,
    verification_path: str | Path | None = None,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Verify provider priority and quarter coverage for a completed bronze run."""
    if int(sample_limit) < 0:
        raise ValueError("sample_limit must be zero or greater")
    root = Path(output_root)
    resolved_symbols = _resolve_symbols(symbols, refresh_universe=False)
    start = max(
        _parse_quarter(start_quarter),
        _parse_quarter(ALPHA_VANTAGE_FIRST_QUARTER),
    )
    end = _parse_quarter(end_quarter) if end_quarter else _current_quarter()
    if start > end:
        raise ValueError("start_quarter must not be after end_quarter")
    quarters = list(_iter_quarters(start, end))
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_quarter": _format_quarter(start),
        "end_quarter": _format_quarter(end),
        "symbols": len(resolved_symbols),
        "quarters_per_symbol": len(quarters),
        "expected": len(resolved_symbols) * len(quarters),
        "covered": 0,
        "transcripts": 0,
        "no_data": 0,
        "transcript_segments": 0,
        "providers": {"fmp": 0, "alpha-vantage": 0},
        "missing": 0,
        "invalid": 0,
        "missing_sample": [],
        "invalid_sample": [],
        "complete": False,
    }
    for symbol in resolved_symbols:
        for year, quarter in quarters:
            quarter_name = _format_quarter((year, quarter))
            fmp_path = _transcript_path(root, "fmp", symbol, year, quarter)
            fmp_payload = _read_json(fmp_path)
            if _is_usable_transcript_snapshot(
                fmp_payload,
                provider="FMP",
                symbol=symbol,
                year=year,
                quarter=quarter,
            ):
                report["covered"] += 1
                report["transcripts"] += 1
                report["providers"]["fmp"] += 1
                report["transcript_segments"] += _transcript_segment_count(
                    fmp_payload.get("data")
                )
                continue

            alpha_path = _transcript_path(
                root,
                "alpha-vantage",
                symbol,
                year,
                quarter,
            )
            if not alpha_path.exists():
                report["missing"] += 1
                _append_sample(
                    report["missing_sample"],
                    {"symbol": symbol, "quarter": quarter_name},
                    limit=sample_limit,
                )
                continue
            alpha_payload = _read_json(alpha_path)
            valid_identity = _is_usable_snapshot_identity(
                alpha_payload,
                provider="ALPHA_VANTAGE",
                symbol=symbol,
                year=year,
                quarter=quarter,
            )
            status = alpha_payload.get("status") if isinstance(alpha_payload, dict) else None
            valid_content = status == "no_data" or (
                status == "ok"
                and _payload_has_transcript(alpha_payload.get("data"))
            )
            if not valid_identity or not valid_content:
                report["invalid"] += 1
                _append_sample(
                    report["invalid_sample"],
                    {
                        "symbol": symbol,
                        "quarter": quarter_name,
                        "path": str(alpha_path),
                    },
                    limit=sample_limit,
                )
                continue
            report["covered"] += 1
            report["providers"]["alpha-vantage"] += 1
            if status == "ok":
                report["transcripts"] += 1
                report["transcript_segments"] += _transcript_segment_count(
                    alpha_payload.get("data")
                )
            else:
                report["no_data"] += 1

    report["complete"] = (
        report["covered"] == report["expected"]
        and report["missing"] == 0
        and report["invalid"] == 0
    )
    if verification_path is not None:
        _write_json(Path(verification_path), report)
    return report


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
    for attempt in range(attempts):
        limiter.acquire()
        try:
            response = http_get(
                f"{FMP_BASE_URL}/{endpoint}",
                params=params,
                headers={"apikey": api_key},
                timeout=(10, 60),
            )
        except Exception:
            if attempt + 1 == attempts:
                raise RuntimeError(f"FMP request failed for endpoint={endpoint}") from None
            sleeper(min(60.0, 2.0**attempt))
            continue
        status_code = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except Exception:
            payload = None
        if _authentication_failure(status_code, payload):
            raise ProviderAuthenticationError(
                f"FMP authorization failed for endpoint={endpoint}"
            )
        if status_code == 429:
            if attempt + 1 == attempts:
                raise FmpRateLimitError(f"FMP rate limit exceeded for endpoint={endpoint}")
            sleeper(_retry_delay(response, attempt, rate_limited=True))
            continue
        if status_code < 400 and isinstance(payload, list):
            return payload
        if attempt + 1 == attempts:
            raise RuntimeError(f"FMP request failed for endpoint={endpoint}")
        sleeper(_retry_delay(response, attempt, rate_limited=False))
    raise AssertionError("unreachable")


def _alpha_request(
    symbol: str,
    *,
    year: int,
    quarter: int,
    api_key: str,
    limiter: RollingRateLimiter,
    retries: int,
    http_get: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    attempts = max(0, int(retries)) + 1
    formatted_quarter = _format_quarter((year, quarter))
    for attempt in range(attempts):
        limiter.acquire()
        params = {
            "function": ALPHA_VANTAGE_TRANSCRIPT_FUNCTION,
            "symbol": symbol,
            "quarter": formatted_quarter,
            "apikey": api_key,
        }
        try:
            if http_get is requests.get:
                response = _alpha_streaming_get(http_get, params=params)
            else:
                response = http_get(
                    ALPHA_VANTAGE_QUERY_URL,
                    params=params,
                    timeout=(10, 60),
                )
        except Exception:
            if attempt + 1 == attempts:
                raise AlphaVantageRateLimitError(
                    f"Alpha Vantage request failed for symbol={symbol}, "
                    f"quarter={formatted_quarter}"
                ) from None
            sleeper(60.0 * (2**attempt))
            continue
        status_code = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except Exception:
            payload = None
        if _authentication_failure(status_code, payload) or _premium_failure(payload):
            raise ProviderAuthenticationError(
                "Alpha Vantage transcript authorization or subscription failed"
            )
        limited = (
            status_code == 429
            or (isinstance(payload, dict) and "Note" in payload)
            or _rate_limit_information(payload)
        )
        failed = (
            status_code >= 400
            or not isinstance(payload, dict)
            or (isinstance(payload, dict) and "Error Message" in payload)
        )
        if not limited and not failed:
            return payload
        if attempt + 1 == attempts:
            raise AlphaVantageRateLimitError(
                f"Alpha Vantage request failed for symbol={symbol}, "
                f"quarter={formatted_quarter}"
            )
        sleeper(_retry_delay(response, attempt, rate_limited=limited, base=60.0))
    raise AssertionError("unreachable")


def _alpha_streaming_get(
    http_get: Callable[..., Any],
    *,
    params: dict[str, Any],
    total_timeout: float = ALPHA_VANTAGE_TOTAL_TIMEOUT_SECONDS,
) -> Any:
    """Read a real Alpha response with a wall-clock deadline.

    ``requests`` read timeouts only limit the gap between received bytes. A
    server that slowly drips a response can therefore hold the full-universe
    collector indefinitely. Streaming lets us enforce a total response
    deadline while retaining the ordinary mock-friendly request path in tests.
    """
    started = time.monotonic()
    response = http_get(
        ALPHA_VANTAGE_QUERY_URL,
        params=params,
        timeout=(
            ALPHA_VANTAGE_CONNECT_TIMEOUT_SECONDS,
            ALPHA_VANTAGE_READ_TIMEOUT_SECONDS,
        ),
        stream=True,
    )
    content = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                content.extend(chunk)
            if time.monotonic() - started > float(total_timeout):
                raise requests.Timeout("Alpha Vantage total response timeout")
        if time.monotonic() - started > float(total_timeout):
            raise requests.Timeout("Alpha Vantage total response timeout")
        response._content = bytes(content)
        response._content_consumed = True
        return response
    except Exception:
        response.close()
        raise


def _authentication_failure(status_code: int, payload: Any) -> bool:
    if status_code in {401, 402, 403}:
        return True
    if not isinstance(payload, dict):
        return False
    text = " ".join(str(payload.get(key, "")) for key in ("Error Message", "error", "message", "Information"))
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "invalid api key",
            "invalid apikey",
            "api key is invalid",
            "not authorized",
            "unauthorized",
            "forbidden",
        )
    )


def _premium_failure(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    information = str(payload.get("Information", "")).lower()
    return "premium" in information or "subscription" in information


def _rate_limit_information(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    information = str(payload.get("Information", "")).lower()
    return any(
        token in information
        for token in ("rate limit", "call frequency", "calls per minute")
    )


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
    return min(300.0, (60.0 if rate_limited else base) * (2**attempt))


def _parse_sources(sources: Iterable[str] | None) -> tuple[str, ...]:
    aliases = {
        "all": US_EARNINGS_CALL_SOURCE_PRIORITY,
        "fmp": ("fmp",),
        "alpha": ("alpha-vantage",),
        "alpha-vantage": ("alpha-vantage",),
        "alphavantage": ("alpha-vantage",),
    }
    selected: set[str] = set()
    for source in sources or US_EARNINGS_CALL_SOURCE_PRIORITY:
        key = str(source).strip().lower()
        if not key:
            continue
        if key not in aliases:
            raise ValueError(f"unknown earnings-call transcript source: {source}")
        selected.update(aliases[key])
    return tuple(
        source for source in US_EARNINGS_CALL_SOURCE_PRIORITY if source in selected
    )


def _resolve_symbols(
    symbols: Iterable[str] | None,
    *,
    refresh_universe: bool,
) -> list[str]:
    if symbols is not None:
        return sorted(
            {
                normalize_yfinance_ticker(symbol)
                for symbol in symbols
                if str(symbol).strip()
            }
        )
    if refresh_universe or not FILTERED_UNIVERSE_PATH.exists():
        universe = download_us_equity_universe()
    else:
        universe = pd.read_csv(FILTERED_UNIVERSE_PATH, dtype=str)
    column = "ticker" if "ticker" in universe.columns else "symbol"
    return sorted(
        {
            normalize_yfinance_ticker(symbol)
            for symbol in universe[column].dropna()
            if str(symbol).strip()
        }
    )


def _catalog_symbols(payload: list[Any]) -> set[str]:
    symbols: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") or item.get("ticker")
        if symbol:
            symbols.add(normalize_yfinance_ticker(symbol))
    return symbols


def _fmp_event_quarters(
    payload: list[Any],
    *,
    start: tuple[int, int] | None,
    end: tuple[int, int],
) -> set[tuple[int, int]]:
    events: set[tuple[int, int]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        year_value = item.get("fiscalYear", item.get("year"))
        quarter_value = item.get("quarter")
        try:
            year = int(str(year_value).strip())
            quarter = int(str(quarter_value).strip().upper().removeprefix("Q"))
            parsed = _parse_quarter(f"{year}Q{quarter}")
        except (TypeError, ValueError):
            continue
        if (start is None or parsed >= start) and parsed <= end:
            events.add(parsed)
    return events


def _parse_quarter(value: str | tuple[int, int]) -> tuple[int, int]:
    if isinstance(value, tuple) and len(value) == 2:
        year, quarter = int(value[0]), int(value[1])
    else:
        text = str(value).strip().upper().replace("-", "")
        if len(text) != 6 or text[4] != "Q":
            raise ValueError(f"quarter must use YYYYQ1..YYYYQ4 format: {value}")
        year, quarter = int(text[:4]), int(text[5])
    if year < 1900 or quarter not in {1, 2, 3, 4}:
        raise ValueError(f"invalid quarter: {value}")
    return year, quarter


def _format_quarter(value: tuple[int, int]) -> str:
    return f"{int(value[0]):04d}Q{int(value[1])}"


def _current_quarter() -> tuple[int, int]:
    today = date.today()
    return today.year, ((today.month - 1) // 3) + 1


def _quarter_index(value: tuple[int, int]) -> int:
    return int(value[0]) * 4 + int(value[1]) - 1


def _quarter_from_index(value: int) -> tuple[int, int]:
    year, zero_based = divmod(int(value), 4)
    return year, zero_based + 1


def _shift_quarter(value: tuple[int, int], amount: int) -> tuple[int, int]:
    return _quarter_from_index(_quarter_index(value) + int(amount))


def _iter_quarters(
    start: tuple[int, int],
    end: tuple[int, int],
) -> Iterable[tuple[int, int]]:
    for index in range(_quarter_index(start), _quarter_index(end) + 1):
        yield _quarter_from_index(index)


def _payload_has_transcript(payload: Any) -> bool:
    if isinstance(payload, list):
        return any(_payload_has_transcript(item) for item in payload)
    if not isinstance(payload, dict):
        return False
    for key in ("content", "text"):
        if str(payload.get(key, "")).strip():
            return True
    transcript = payload.get("transcript")
    if isinstance(transcript, str):
        return bool(transcript.strip())
    if isinstance(transcript, (list, dict)):
        return _payload_has_transcript(transcript)
    return False


def _transcript_segment_count(payload: Any) -> int:
    if isinstance(payload, list):
        return sum(_transcript_segment_count(item) for item in payload)
    if not isinstance(payload, dict):
        return 0
    transcript = payload.get("transcript")
    if isinstance(transcript, list):
        return len(transcript)
    return 1 if _payload_has_transcript(payload) else 0


def _append_sample(target: list[Any], value: Any, *, limit: int) -> None:
    if len(target) < int(limit):
        target.append(value)


def _envelope(
    *,
    provider: str,
    dataset: str,
    status: str,
    data: Any,
    symbol: str | None = None,
    year: int | None = None,
    quarter: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "dataset": dataset,
        "status": status,
        "complete": True,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    if symbol is not None:
        payload["symbol"] = symbol
    if year is not None and quarter is not None:
        payload["fiscal_year"] = int(year)
        payload["quarter"] = int(quarter)
    return payload


def _transcript_path(
    root: Path,
    provider: str,
    symbol: str,
    year: int,
    quarter: int,
) -> Path:
    return (
        root
        / provider
        / f"ticker={symbol}"
        / f"fiscal_year={int(year):04d}"
        / f"quarter=Q{int(quarter)}.json"
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".earnings-call-",
        suffix=".json.tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        replace_file_with_permission_retry(Path(temporary), path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _cache_is_fresh(
    payload: dict[str, Any] | None,
    *,
    provider: str,
    symbol: str,
    year: int,
    quarter: int,
    force: bool,
    recent_cutoff: tuple[int, int],
) -> bool:
    if force or not isinstance(payload, dict):
        return False
    complete = _is_usable_snapshot_identity(
        payload,
        provider=provider,
        symbol=symbol,
        year=year,
        quarter=quarter,
    )
    if not complete:
        return False
    if payload.get("status") == "ok":
        return _payload_has_transcript(payload.get("data"))
    return (int(year), int(quarter)) < recent_cutoff


def _is_usable_transcript_snapshot(
    payload: dict[str, Any] | None,
    *,
    provider: str,
    symbol: str,
    year: int,
    quarter: int,
) -> bool:
    return _is_usable_snapshot_identity(
        payload,
        provider=provider,
        symbol=symbol,
        year=year,
        quarter=quarter,
    ) and payload.get("status") == "ok" and _payload_has_transcript(
        payload.get("data")
    )


def _is_usable_snapshot_identity(
    payload: dict[str, Any] | None,
    *,
    provider: str,
    symbol: str,
    year: int,
    quarter: int,
) -> bool:
    return bool(
        isinstance(payload, dict)
        and (
            payload.get("complete") is True
            and payload.get("provider") == provider
            and payload.get("symbol") == symbol
            and payload.get("fiscal_year") == int(year)
            and payload.get("quarter") == int(quarter)
            and payload.get("status") in {"ok", "no_data"}
        )
    )


def _rate_limit_state_path(root: Path, filename: str) -> Path:
    if root.resolve() == BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR.resolve():
        return DATA_LAKE.meta("consensus", filename)
    return root / "meta" / "rate-limits" / filename


def _new_counts(
    *,
    symbols: int,
    alpha_quarters: int,
    sources: Iterable[str],
) -> dict[str, Any]:
    return {
        "symbols": int(symbols),
        "alpha_quarters_per_symbol": int(alpha_quarters),
        "written": 0,
        "skipped": 0,
        "priority_skipped": 0,
        "failed": 0,
        "recovered": 0,
        "no_data": 0,
        "providers": {
            source: {
                "requests": 0,
                "written": 0,
                "skipped": 0,
                "failed": 0,
                "recovered": 0,
                "no_data": 0,
                "auth_disabled": False,
            }
            for source in sources
        },
    }


def _record_write(counts: dict[str, Any], provider: str, *, status: str) -> None:
    counts["written"] += 1
    counts["providers"][provider]["written"] += 1
    if status == "no_data":
        counts["no_data"] += 1
        counts["providers"][provider]["no_data"] += 1


def _record_skip(counts: dict[str, Any], provider: str) -> None:
    counts["skipped"] += 1
    counts["providers"][provider]["skipped"] += 1


def _record_failure(counts: dict[str, Any], provider: str) -> None:
    counts["failed"] += 1
    counts["providers"][provider]["failed"] += 1


def _record_recovery(counts: dict[str, Any], provider: str) -> None:
    counts["failed"] = max(0, counts["failed"] - 1)
    counts["recovered"] += 1
    counts["providers"][provider]["failed"] = max(
        0,
        counts["providers"][provider]["failed"] - 1,
    )
    counts["providers"][provider]["recovered"] += 1


__all__ = [
    "ALPHA_VANTAGE_FIRST_QUARTER",
    "ALPHA_VANTAGE_QUERY_URL",
    "ALPHA_VANTAGE_TRANSCRIPT_FUNCTION",
    "BRONZE_US_EARNINGS_CALL_TRANSCRIPTS_DIR",
    "FMP_BASE_URL",
    "US_EARNINGS_CALL_SOURCE_PRIORITY",
    "audit_us_earnings_call_transcripts",
    "download_us_earnings_call_transcripts",
]
