from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.request import Request, urlopen

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import (
    json_source_validator,
    validate_nonempty_file,
    write_source_bytes,
    write_source_dataframe,
)
from engine.markets.us import US_MARKET_CONFIG
from engine.transformers._internal.edgar_identity import (
    DEFAULT_EDGAR_LOCAL_DATA_DIR,
    configure_edgar_data_directory,
    configure_edgar_identity,
)


SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/{cik_file_key}.json"
SEC_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
SEC_TICKER_ALIASES_PATH = DATA_LAKE.meta("sec_ticker_aliases.csv")
SEC_COMPANYFACTS_DIR = DATA_LAKE.bronze("sec", "companyfacts")
SEC_FILINGS_DIR = DATA_LAKE.bronze("sec", "fillings")
SEC_FILINGS_IR_DIR = SEC_FILINGS_DIR / "ir"
EDGAR_LOCAL_DATA_DIR = DEFAULT_EDGAR_LOCAL_DATA_DIR
SEC_US_EQUITY_UNIVERSE_PATH = DATA_LAKE.bronze(
    "yfinance", "universe", "us_equity_universe.csv"
)
DEFAULT_SEC_USER_AGENT = "Arcana contact@example.com"
SEC_PRIMARY_FORMS = ("10-K", "10-Q", "8-K")
SEC_IR_EXHIBIT_PATTERN = re.compile(r"^EX-99(?:\.\d+)?$", re.IGNORECASE)
SEC_FILING_SOURCE = "sec-edgartools-filing-html"
WINDOWS_RESERVED_PATH_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass
class SecFilingHtmlDownloadSummary:
    output_dir: Path
    start_date: str
    end_date: str
    symbols_requested: int
    ir_only: bool = False
    workers: int = 1
    symbols_processed: int = 0
    symbols_resumed: int = 0
    symbols_unmapped: int = 0
    filings_seen: int = 0
    primary_html_written: int = 0
    ir_html_written: int = 0
    existing_files_skipped: int = 0
    non_html_documents_skipped: int = 0
    errors: int = 0
    latest_run_path: Path | None = None
    error_log_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in ("output_dir", "latest_run_path", "error_log_path"):
            value = payload.get(name)
            payload[name] = str(value) if value is not None else None
        return payload


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


FilingProvider = Callable[[Mapping[str, str], Sequence[str], str, str], Iterable[Any]]


def download_us_filing_htmls(
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    forms: Sequence[str] = SEC_PRIMARY_FORMS,
    offset: int = 0,
    limit: int | None = None,
    force: bool = False,
    resume: bool = True,
    ir_only: bool = False,
    workers: int = 1,
    sleep_seconds: float = 0.1,
    retries: int = 3,
    retry_backoff_seconds: float = 2.0,
    output_dir: str | Path = SEC_FILINGS_DIR,
    ticker_map_path: str | Path = SEC_TICKER_MAP_PATH,
    ticker_aliases_path: str | Path = SEC_TICKER_ALIASES_PATH,
    universe_path: str | Path = SEC_US_EQUITY_UNIVERSE_PATH,
    filings_provider: FilingProvider | None = None,
    now: datetime | None = None,
) -> SecFilingHtmlDownloadSummary:
    """Download primary filing HTML and 8-K EX-99.x HTML with edgartools.

    Primary 10-K, 10-Q, and 8-K documents are written below ``fillings/{form}``.
    HTML EX-99, EX-99.1, EX-99.2, and other numeric EX-99.x attachments from
    8-K filings are written below ``fillings/ir``. Stable accession-based names,
    per-company checkpoints, and atomic writes make full-universe runs resumable.
    """
    resolved_forms = ["8-K"] if ir_only else _normalize_sec_forms(forms)
    worker_count = max(1, int(workers))
    resolved_end = _normalize_sec_filing_date(end_date) or (now or datetime.now(timezone.utc)).date().isoformat()
    resolved_start = _normalize_sec_filing_date(start_date)
    if not resolved_start:
        resolved_start = f"{max(1994, int(resolved_end[:4]) - 10):04d}-01-01"
    if resolved_start > resolved_end:
        raise ValueError("SEC filing start_date must not be later than end_date")

    explicit_symbols = list(symbols) if symbols is not None else None
    ticker_map = _load_sec_filing_ticker_map(ticker_map_path)
    if ticker_map.empty:
        download_sec_company_tickers(ticker_map_path)
        ticker_map = _load_sec_filing_ticker_map(ticker_map_path)
    ticker_aliases = _load_sec_filing_ticker_map(ticker_aliases_path)
    if not ticker_aliases.empty:
        ticker_map = pd.concat([ticker_aliases, ticker_map], ignore_index=True)
        ticker_map = ticker_map.drop_duplicates("ticker", keep="first").reset_index(drop=True)
    company_rows, unmapped = _resolve_sec_filing_companies(
        symbols=explicit_symbols,
        ticker_map=ticker_map,
        universe_path=universe_path,
    )
    selected = company_rows[max(0, int(offset)) :]
    if limit is not None:
        selected = selected[: max(0, int(limit))]

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "ir").mkdir(parents=True, exist_ok=True)
    checkpoint_dir = output / "_checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    retrieved_at = _utc_now(now).isoformat()
    query = {
        "forms": resolved_forms,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "ir_only": bool(ir_only),
    }
    query_fingerprint = hashlib.sha256(
        json.dumps(query, sort_keys=True).encode("utf-8")
    ).hexdigest()
    summary = SecFilingHtmlDownloadSummary(
        output_dir=output,
        start_date=resolved_start,
        end_date=resolved_end,
        symbols_requested=len(selected),
        ir_only=bool(ir_only),
        workers=worker_count,
        symbols_unmapped=unmapped,
    )
    errors: list[dict[str, Any]] = []

    provider = filings_provider
    if provider is None:
        _configure_edgartools_data_directory()
        try:
            from edgar import set_identity  # type: ignore
        except ImportError as exc:
            raise RuntimeError("edgartools is required to download SEC filing HTML") from exc
        configure_edgar_identity(set_identity)
        provider = _edgartools_filings_provider

    pending: list[tuple[int, Mapping[str, str], Path]] = []
    for company_index, company_row in enumerate(selected, start=max(0, int(offset))):
        symbol = company_row["ticker"]
        cik_key = _cik_file_key(company_row["cik"])
        checkpoint_path = checkpoint_dir / f"{cik_key}.json"
        if resume and not force and _completed_filing_checkpoint(checkpoint_path, query_fingerprint):
            summary.symbols_resumed += 1
            print(f"[RESUME] SEC filing HTML {symbol} ({cik_key}, offset={company_index})", flush=True)
            continue
        pending.append((company_index, company_row, checkpoint_path))

    def collect_company(job: tuple[int, Mapping[str, str], Path]) -> dict[str, Any]:
        company_index, company_row, checkpoint_path = job
        symbol = company_row["ticker"]
        cik_key = _cik_file_key(company_row["cik"])
        print(f"[SEC FILINGS] {symbol} ({cik_key}, offset={company_index})", flush=True)
        symbol_errors: list[dict[str, Any]] = []
        symbol_paths: list[str] = []
        symbol_filings = 0
        result = {
            "filings_seen": 0,
            "primary_html_written": 0,
            "ir_html_written": 0,
            "existing_files_skipped": 0,
            "non_html_documents_skipped": 0,
        }
        try:
            filings = _call_filing_provider_with_retry(
                provider,
                company_row,
                resolved_forms,
                resolved_start,
                resolved_end,
                retries=max(0, int(retries)),
                retry_backoff_seconds=max(0.0, float(retry_backoff_seconds)),
            )
            for filing in filings:
                filing_form = str(_safe_attr(filing, "form") or "").upper()
                if filing_form not in resolved_forms:
                    continue
                symbol_filings += 1
                result["filings_seen"] += 1
                try:
                    attachments = _safe_attr(filing, "attachments")
                    if not ir_only:
                        primary = (
                            _safe_attr(attachments, "primary_html_document")
                            if attachments is not None
                            else None
                        )
                        if primary is None:
                            result["non_html_documents_skipped"] += 1
                        else:
                            status, target = _save_sec_filing_attachment(
                                filing=filing,
                                attachment=primary,
                                company_row=company_row,
                                category="primary",
                                output_dir=output,
                                retrieved_at=retrieved_at,
                                force=force,
                            )
                            if target is not None:
                                symbol_paths.append(target.relative_to(output).as_posix())
                            if status == "written":
                                result["primary_html_written"] += 1
                            elif status == "existing":
                                result["existing_files_skipped"] += 1
                            else:
                                result["non_html_documents_skipped"] += 1

                    if filing_form == "8-K" and attachments is not None:
                        for attachment in attachments:
                            if not is_sec_ir_exhibit(attachment):
                                continue
                            status, target = _save_sec_filing_attachment(
                                filing=filing,
                                attachment=attachment,
                                company_row=company_row,
                                category="ir",
                                output_dir=output,
                                retrieved_at=retrieved_at,
                                force=force,
                            )
                            if target is not None:
                                symbol_paths.append(target.relative_to(output).as_posix())
                            if status == "written":
                                result["ir_html_written"] += 1
                            elif status == "existing":
                                result["existing_files_skipped"] += 1
                            else:
                                result["non_html_documents_skipped"] += 1
                except Exception as exc:
                    error = _filing_error(company_row, filing, exc)
                    symbol_errors.append(error)
        except Exception as exc:
            error = _filing_error(company_row, None, exc)
            symbol_errors.append(error)

        checkpoint = {
            "schema_version": 1,
            "source": SEC_FILING_SOURCE,
            "status": "complete" if not symbol_errors else "partial",
            "ticker": symbol,
            "cik": cik_key,
            "query": query,
            "query_fingerprint": query_fingerprint,
            "filings_seen": symbol_filings,
            "files": sorted(set(symbol_paths)),
            "errors": symbol_errors,
            "updated_at": _utc_now(None).isoformat(),
        }
        _write_json_source(checkpoint_path, checkpoint, source=SEC_FILING_SOURCE)
        if sleep_seconds > 0:
            sleep(float(sleep_seconds))
        result["errors"] = symbol_errors
        return result

    if worker_count == 1:
        company_results = map(collect_company, pending)
        for result in company_results:
            _merge_sec_company_result(summary, errors, result)
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="sec-ir") as executor:
            future_to_job = {executor.submit(collect_company, job): job for job in pending}
            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - worker has its own error boundary.
                    _, company_row, _ = job
                    result = {
                        "filings_seen": 0,
                        "primary_html_written": 0,
                        "ir_html_written": 0,
                        "existing_files_skipped": 0,
                        "non_html_documents_skipped": 0,
                        "errors": [_filing_error(company_row, None, exc)],
                    }
                _merge_sec_company_result(summary, errors, result)

    summary.errors = len(errors)
    if errors:
        error_dir = output / "_errors"
        timestamp = _utc_now(now).strftime("%Y%m%dT%H%M%S%fZ")
        error_log_path = error_dir / f"errors_{timestamp}.json"
        _write_json_source(
            error_log_path,
            {"schema_version": 1, "source": SEC_FILING_SOURCE, "query": query, "errors": errors},
            source=SEC_FILING_SOURCE,
        )
        summary.error_log_path = error_log_path

    latest_run_path = output / "latest_run.json"
    summary.latest_run_path = latest_run_path
    _write_json_source(
        latest_run_path,
        {
            "schema_version": 1,
            "source": SEC_FILING_SOURCE,
            "retrieved_at": _utc_now(now).isoformat(),
            "query": query,
            "summary": summary.to_dict(),
            "edgartools_version": _edgartools_version(),
        },
        source=SEC_FILING_SOURCE,
    )
    return summary


def _merge_sec_company_result(
    summary: SecFilingHtmlDownloadSummary,
    errors: list[dict[str, Any]],
    result: Mapping[str, Any],
) -> None:
    summary.symbols_processed += 1
    for name in (
        "filings_seen",
        "primary_html_written",
        "ir_html_written",
        "existing_files_skipped",
        "non_html_documents_skipped",
    ):
        setattr(summary, name, getattr(summary, name) + int(result.get(name, 0)))
    errors.extend(result.get("errors", []))


def is_sec_ir_exhibit(attachment: Any) -> bool:
    document_type = str(_safe_attr(attachment, "document_type") or "").strip().upper()
    return bool(SEC_IR_EXHIBIT_PATTERN.fullmatch(document_type))


def _save_sec_filing_attachment(
    *,
    filing: Any,
    attachment: Any,
    company_row: Mapping[str, str],
    category: str,
    output_dir: Path,
    retrieved_at: str,
    force: bool,
) -> tuple[str, Path | None]:
    filing_form = str(_safe_attr(filing, "form") or "UNKNOWN").upper()
    filing_date = _normalize_sec_filing_date(_safe_attr(filing, "filing_date")) or "unknown-date"
    accession = str(
        _safe_attr(filing, "accession_no")
        or _safe_attr(filing, "accession_number")
        or "unknown-accession"
    ).strip()
    symbol = _safe_path_part(company_row.get("ticker"), fallback=_cik_file_key(company_row["cik"]))
    document_type = str(_safe_attr(attachment, "document_type") or "HTML").strip().upper()
    document_name = str(_safe_attr(attachment, "document") or "document.html").strip()
    stored_name = _safe_path_part(document_name, fallback="document.html")
    if not stored_name.lower().endswith((".htm", ".html")):
        stored_name = f"{stored_name}.html"
    stem = f"{filing_date}_{_safe_path_part(accession)}"
    if category == "ir":
        directory = output_dir / "ir" / symbol
        filename = f"{stem}_{_safe_path_part(document_type)}_{stored_name}"
    else:
        directory = output_dir / _safe_path_part(filing_form) / symbol
        filename = f"{stem}_{stored_name}"
    target = directory / filename
    metadata_path = target.with_suffix(target.suffix + ".metadata.json")

    if target.is_file() and target.stat().st_size > 0 and not force:
        if not metadata_path.exists():
            metadata = _sec_attachment_metadata(
                filing=filing,
                attachment=attachment,
                company_row=company_row,
                category=category,
                target=target,
                output_dir=output_dir,
                retrieved_at=retrieved_at,
                byte_size=target.stat().st_size,
                sha256=_sha256_path(target),
            )
            _write_json_source(metadata_path, metadata, source=SEC_FILING_SOURCE)
        return "existing", target

    content = _safe_attr(attachment, "content")
    payload = _content_bytes(content)
    declared_html = _attachment_is_html(attachment)
    if not payload or (not declared_html and not _looks_like_html(payload)):
        return "non_html", None

    write_source_bytes(
        target,
        payload,
        source=SEC_FILING_SOURCE,
        validator=validate_nonempty_file,
        metadata={"ticker": company_row.get("ticker", ""), "form": filing_form, "category": category},
    )
    metadata = _sec_attachment_metadata(
        filing=filing,
        attachment=attachment,
        company_row=company_row,
        category=category,
        target=target,
        output_dir=output_dir,
        retrieved_at=retrieved_at,
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    _write_json_source(metadata_path, metadata, source=SEC_FILING_SOURCE)
    return "written", target


def _sec_attachment_metadata(
    *,
    filing: Any,
    attachment: Any,
    company_row: Mapping[str, str],
    category: str,
    target: Path,
    output_dir: Path,
    retrieved_at: str,
    byte_size: int,
    sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SEC_FILING_SOURCE,
        "provider": "edgartools",
        "edgartools_version": _edgartools_version(),
        "category": category,
        "ticker": company_row.get("ticker", ""),
        "cik": _cik_file_key(company_row["cik"]),
        "company_name": company_row.get("title", ""),
        "form": str(_safe_attr(filing, "form") or ""),
        "filing_date": _normalize_sec_filing_date(_safe_attr(filing, "filing_date")),
        "period_of_report": str(_safe_attr(filing, "period_of_report") or ""),
        "accession_number": str(
            _safe_attr(filing, "accession_no") or _safe_attr(filing, "accession_number") or ""
        ),
        "document_type": str(_safe_attr(attachment, "document_type") or ""),
        "sequence_number": str(_safe_attr(attachment, "sequence_number") or ""),
        "document_name": str(_safe_attr(attachment, "document") or ""),
        "description": str(_safe_attr(attachment, "description") or ""),
        "source_url": str(_safe_attr(attachment, "url") or ""),
        "stored_path": target.relative_to(output_dir).as_posix(),
        "byte_size": int(byte_size),
        "sha256": sha256,
        "retrieved_at": retrieved_at,
    }


def _edgartools_filings_provider(
    company_row: Mapping[str, str],
    forms: Sequence[str],
    start_date: str,
    end_date: str,
) -> Iterable[Any]:
    _configure_edgartools_data_directory()
    from edgar import Company  # type: ignore

    company = Company(int(company_row["cik"]))
    return company.get_filings(
        form=list(forms),
        filing_date=f"{start_date}:{end_date}",
        amendments=False,
        sort_by="filing_date",
        trigger_full_load=True,
    )


def _configure_edgartools_data_directory(
    data_dir: str | Path | None = None,
) -> Path:
    """Compatibility wrapper for the shared edgartools directory setup."""
    return configure_edgar_data_directory(data_dir or EDGAR_LOCAL_DATA_DIR)


def _call_filing_provider_with_retry(
    provider: FilingProvider,
    company_row: Mapping[str, str],
    forms: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    retries: int,
    retry_backoff_seconds: float,
) -> Iterable[Any]:
    for attempt in range(retries + 1):
        try:
            return provider(company_row, forms, start_date, end_date)
        except Exception:
            if attempt >= retries:
                raise
            sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
    raise AssertionError("SEC filing provider retry loop exited unexpectedly")


def _load_sec_filing_ticker_map(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return _empty_ticker_map()
    try:
        frame = pd.read_csv(path, dtype={"cik": "string", "ticker": "string", "title": "string"})
    except pd.errors.EmptyDataError:
        return _empty_ticker_map()
    for column in ("cik", "ticker", "title"):
        if column not in frame.columns:
            frame[column] = ""
    normalized = frame.loc[:, ["cik", "ticker", "title"]].copy()
    normalized["cik"] = normalized["cik"].map(_normalize_cik)
    normalized["ticker"] = normalized["ticker"].map(US_MARKET_CONFIG.normalize_symbol)
    normalized["title"] = normalized["title"].fillna("").astype(str).str.strip()
    normalized = normalized.loc[normalized["cik"].ne("") & normalized["ticker"].ne("")]
    return normalized.drop_duplicates("ticker", keep="first").reset_index(drop=True)


def _resolve_sec_filing_companies(
    *,
    symbols: Sequence[str] | None,
    ticker_map: pd.DataFrame,
    universe_path: str | Path,
) -> tuple[list[dict[str, str]], int]:
    explicit = symbols is not None
    if symbols is None:
        universe = Path(universe_path)
        if universe.exists():
            frame = pd.read_csv(universe, dtype=str)
            column = "ticker" if "ticker" in frame.columns else "symbol"
            symbols = frame[column].dropna().astype(str).tolist() if column in frame.columns else []
        else:
            symbols = ticker_map["ticker"].dropna().astype(str).tolist()

    by_ticker = {
        str(row["ticker"]): {
            "cik": str(row["cik"]),
            "ticker": str(row["ticker"]),
            "title": str(row["title"]),
        }
        for row in ticker_map.to_dict("records")
    }
    by_cik: dict[str, dict[str, str]] = {}
    for row in by_ticker.values():
        by_cik.setdefault(row["cik"], row)

    resolved: list[dict[str, str]] = []
    missing: list[str] = []
    seen_ciks: set[str] = set()
    for raw_symbol in symbols or []:
        symbol = US_MARKET_CONFIG.normalize_symbol(raw_symbol)
        cik = _normalize_cik(symbol)
        row = by_cik.get(cik) if cik else by_ticker.get(symbol)
        if row is None and cik:
            row = {"cik": cik, "ticker": _cik_file_key(cik), "title": ""}
        if row is None:
            missing.append(symbol)
            continue
        if row["cik"] in seen_ciks:
            continue
        resolved.append(dict(row))
        seen_ciks.add(row["cik"])
    if explicit and missing:
        raise ValueError(
            "unknown SEC symbols without CIK mapping: " + ", ".join(missing)
        )
    return resolved, len(missing)


def _completed_filing_checkpoint(path: Path, query_fingerprint: str) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if payload.get("status") != "complete" or payload.get("query_fingerprint") != query_fingerprint:
        return False
    root = path.parent.parent
    files = payload.get("files", [])
    return isinstance(files, list) and all(
        (root / str(relative)).is_file() and (root / str(relative)).stat().st_size > 0
        for relative in files
    )


def _write_json_source(path: Path, payload: Mapping[str, Any], *, source: str) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    return write_source_bytes(path, encoded, source=source, validator=json_source_validator)


def _normalize_sec_forms(forms: Sequence[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(form).strip().upper() for form in forms if str(form).strip()))
    unsupported = sorted(set(normalized) - set(SEC_PRIMARY_FORMS))
    if unsupported:
        raise ValueError("unsupported SEC filing forms: " + ", ".join(unsupported))
    if not normalized:
        raise ValueError("at least one SEC filing form is required")
    return normalized


def _normalize_sec_filing_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid SEC filing date: {value}") from exc


def _attachment_is_html(attachment: Any) -> bool:
    method = getattr(attachment, "is_html", None)
    if callable(method):
        try:
            return bool(method())
        except Exception:
            pass
    document = str(_safe_attr(attachment, "document") or "").lower()
    return document.endswith((".htm", ".html"))


def _content_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None:
        return b""
    return str(value).encode("utf-8")


def _looks_like_html(payload: bytes) -> bool:
    sample = payload[:16384].lstrip().lower()
    return any(
        marker in sample
        for marker in (b"<!doctype html", b"<html", b"<head", b"<body", b"<table", b"<div", b"<ix:header")
    )


def _safe_attr(value: Any, name: str) -> Any:
    if value is None:
        return None
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _safe_path_part(value: Any, *, fallback: str = "document") -> str:
    text = str(value or "").strip()
    text = re.sub(r'[\\/*?:"<>|]+', "_", text).strip(" ._")
    text = text or fallback
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_PATH_NAMES:
        text = f"_{text}"
    if len(text) > 96:
        suffix = Path(text).suffix[:12]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        prefix_length = max(1, 96 - len(suffix) - len(digest) - 1)
        text = f"{text[:prefix_length].rstrip(' ._')}_{digest}{suffix}"
    return text


def _filing_error(company_row: Mapping[str, str], filing: Any, exc: Exception) -> dict[str, Any]:
    return {
        "ticker": company_row.get("ticker", ""),
        "cik": _cik_file_key(company_row["cik"]),
        "form": str(_safe_attr(filing, "form") or ""),
        "filing_date": str(_safe_attr(filing, "filing_date") or ""),
        "accession_number": str(
            _safe_attr(filing, "accession_no") or _safe_attr(filing, "accession_number") or ""
        ),
        "error_type": type(exc).__name__,
        "message": str(exc)[:1000],
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _edgartools_version() -> str:
    try:
        return importlib.metadata.version("edgartools")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
