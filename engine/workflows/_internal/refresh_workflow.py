from __future__ import annotations

import argparse
import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name, market_symbol_csv_name
from engine.core.source_storage import (
    SourceArchiveSession,
    SourceRefreshLock,
    new_source_run_id,
    replace_file_with_permission_retry,
    write_source_dataframe,
)
from engine.extractors.filings import (
    collect_dart_report_metadata,
    deduplicate_report_metadata,
    download_business_infos,
    download_dividend_histories,
    download_statement_comments,
    download_statements,
)
from engine.loaders import dividends as dividend_loader
from engine.loaders import factors as factor_loader
from engine.loaders import filings as filing_loader
from engine.loaders import market_data as market_loader
from engine.loaders import securities as security_loader
from engine.loaders import factor_snapshots as factor_snapshot_loader
from engine.transformers.market_data import normalize_price, normalize_shares
from engine.workflows._internal import download_workflow


KST = ZoneInfo("Asia/Seoul")
DEFAULT_START_DATE = "20100101"
KR_REFRESH_TABLES = {
    "issuers",
    "security_master",
    "identifiers",
    "price_daily",
    "stock_shares",
    "dart_report_metadata",
    "stock_dividend",
    "factor_catalog",
    "fact_daily_factors",
    "fact_daily_factor_snapshot",
}
TABLE_DATE_COLUMNS = {
    "price_daily": "trade_date",
    "stock_shares": "trade_date",
    "dart_report_metadata": "report_date",
    "stock_dividend": "trade_date",
    "fact_daily_factors": "trade_date",
    "fact_daily_factor_snapshot": "trade_date",
}
SECURITY_TABLES = {
    "issuers": "issuers",
    "security_master": "security-master",
    "identifiers": "identifiers",
}


class KRXEmptyResponseError(RuntimeError):
    """Raised when KRX returns no rows for a requested trading-day window."""


@dataclass(frozen=True)
class RefreshWindow:
    start_date: str | None
    end_date: str
    latest_date: date | None

    @property
    def start_iso(self) -> str | None:
        return _to_iso_date(self.start_date) if self.start_date else None

    @property
    def end_iso(self) -> str:
        return _to_iso_date(self.end_date)

    @property
    def has_work(self) -> bool:
        return self.start_date is not None and self.start_date <= self.end_date


class RefreshState:
    def __init__(self, path: Path, data: dict[str, Any], *, enabled: bool = True):
        self.path = path
        self.data = data
        self.enabled = enabled
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        signature: dict[str, Any],
        resume: bool,
        enabled: bool,
    ) -> "RefreshState":
        path = Path(path)
        data = None
        loaded_existing = False
        if resume and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"cannot resume from invalid refresh state: {path}; "
                    "use --no-resume to start a new run"
                ) from exc
            if data and data.get("signature") != signature:
                previous_targets = set(data.get("signature", {}).get("targets", []))
                completed_steps = set(data.get("completed_steps", []))
                if not enabled:
                    print(
                        f"[DRY-RUN] ignoring state with different options: {path}",
                        flush=True,
                    )
                    data = None
                elif previous_targets and previous_targets <= completed_steps:
                    print(
                        f"[RESUME] previous run is complete; starting a new state: {path}",
                        flush=True,
                    )
                    data = None
                else:
                    raise RuntimeError(
                        f"refresh state options differ from an incomplete run: {path}; "
                        "rerun with the same options, choose another --resume-state-path, "
                        "or use --no-resume to explicitly start over"
                    )
            elif data:
                loaded_existing = True
        if data is None:
            data = {
                "signature": signature,
                "started_at": datetime.now(KST).isoformat(timespec="seconds"),
                "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
                "completed_steps": [],
                "step_windows": {},
                "completed_symbols": {},
            }
        state = cls(path, data, enabled=enabled)
        state.save()
        if resume:
            if loaded_existing:
                counts = ", ".join(
                    f"{dataset}:{len(symbols):,}"
                    for dataset, symbols in sorted(
                        state.data.get("completed_symbols", {}).items()
                    )
                ) or "none"
                print(
                    f"[RESUME] loaded state path={path} "
                    f"completed_steps={len(state.data.get('completed_steps', []))} "
                    f"completed_symbols={counts}",
                    flush=True,
                )
            else:
                print(f"[RESUME] starting new state path={path}", flush=True)
        return state

    def save(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.data["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            staged = self.path.with_name(f".{self.path.name}.{time.time_ns()}.tmp")
            staged.write_text(
                json.dumps(
                    self.data,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            try:
                replace_file_with_permission_retry(staged, self.path)
            except Exception:
                try:
                    staged.unlink()
                except FileNotFoundError:
                    pass
                raise

    def is_step_completed(self, step: str) -> bool:
        return step in self.data.get("completed_steps", [])

    def complete_step(self, step: str, window: RefreshWindow | None = None) -> None:
        completed_steps = self.data.setdefault("completed_steps", [])
        if step not in completed_steps:
            completed_steps.append(step)
        if window is not None:
            self.data.setdefault("step_windows", {})[step] = refresh_window_to_state(window)
        self.save()

    def step_window(self, step: str) -> RefreshWindow | None:
        raw = self.data.get("step_windows", {}).get(step)
        return refresh_window_from_state(raw) if raw else None

    def record_window(self, step: str, window: RefreshWindow) -> None:
        with self._lock:
            self.data.setdefault("step_windows", {})[step] = refresh_window_to_state(
                window
            )
            self.save()

    def is_symbol_completed(self, dataset: str, symbol: str) -> bool:
        return str(symbol) in self.completed_symbols(dataset)

    def completed_symbols(self, dataset: str) -> set[str]:
        with self._lock:
            symbols = self.data.get("completed_symbols", {}).get(dataset, [])
            return {str(symbol) for symbol in symbols}

    def complete_symbol(self, dataset: str, symbol: str) -> None:
        with self._lock:
            symbols = self.data.setdefault("completed_symbols", {}).setdefault(dataset, [])
            symbol = str(symbol)
            if symbol not in symbols:
                symbols.append(symbol)
            self.save()

    def reset_symbols(self, dataset: str) -> None:
        self.data.setdefault("completed_symbols", {})[dataset] = []
        self.save()

    def reset_step(self, step: str) -> None:
        with self._lock:
            completed_steps = self.data.setdefault("completed_steps", [])
            self.data["completed_steps"] = [
                completed for completed in completed_steps if completed != step
            ]
            self.data.setdefault("step_windows", {}).pop(step, None)
            self.save()

class ProgressTracker:
    def __init__(self, steps: list[str]):
        self.steps = steps
        self.total = len(steps)
        self.current = 0
        self.started_at = time.monotonic()
        self._step_started_at = self.started_at

    def begin(self, name: str) -> None:
        self.current += 1
        self._step_started_at = time.monotonic()
        print(
            f"[PROGRESS] refresh step={self.current}/{self.total} "
            f"name={name} status=start elapsed={format_elapsed(self.started_at)}",
            flush=True,
        )

    def done(self, name: str) -> None:
        print(
            f"[PROGRESS] refresh step={self.current}/{self.total} "
            f"name={name} status=done step_elapsed={format_elapsed(self._step_started_at)} "
            f"elapsed={format_elapsed(self.started_at)}",
            flush=True,
        )

def get_normalize_workflow():
    from engine.workflows._internal import normalize_workflow

    return normalize_workflow

def main() -> None:
    args = build_arg_parser().parse_args()
    if args.dry_run:
        run_refresh(args)
        return
    run_id = new_source_run_id()
    with (
        SourceRefreshLock(args.market),
        SourceArchiveSession(args.market, run_id=run_id),
    ):
        run_refresh(args)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh market bronze, silver, gold, factors, and factor snapshots."
    )
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument(
        "--targets",
        default="all",
        choices=[
            "all",
            "market-data",
            "filings",
            "business-info",
            "dividends",
            "consensus",
            "benchmarks-wacc",
            "operating-metrics",
            "factors",
            "snapshots",
        ],
    )
    parser.add_argument(
        "--end-date",
        type=parse_date_arg,
        default=today_kst().strftime("%Y%m%d"),
        help="Inclusive end date. Accepts YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--symbols",
        help="Optional comma-separated market symbols. Defaults to the full market universe.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument("--stock-retries", type=int, default=3)
    parser.add_argument("--stock-retry-backoff", type=float, default=30.0)
    parser.add_argument("--financial-basis", default="annual", choices=["annual", "quarterly", "ttm"])
    parser.add_argument(
        "--complete-universe-ratio",
        type=float,
        default=0.99,
        help="Minimum recent cross-section ratio used to select the latest complete trade date.",
    )
    parser.add_argument(
        "--consensus-sources",
        default="hankyung,valuefinder,equity",
        help="Comma-separated KR consensus sources.",
    )
    parser.add_argument("--consensus-html-pages", type=int, default=1)
    parser.add_argument("--hankyung-token")
    parser.add_argument("--valuefinder-cookie")
    parser.add_argument("--equity-cookie")
    parser.add_argument("--consensus-stale-days", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-clickhouse", action="store_true")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continue a previous refresh with the same market, targets, end date, and options.",
    )
    parser.add_argument(
        "--resume-state-path",
        default=None,
        help="Optional path for the refresh resume state JSON file.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=100,
        help="Print per-symbol progress every N symbols for supported refresh steps. Use 0 to disable interval logs.",
    )
    parser.add_argument(
        "--clickhouse-mode",
        default="overlap-truncate",
        choices=["overlap-truncate", "always-truncate", "append-only"],
    )
    return parser


def run_refresh(args: argparse.Namespace) -> None:
    if args.market == "us":
        run_us_refresh(args)
        return
    if args.market != "kr":
        raise ValueError("market must be 'kr' or 'us'")

    targets = expand_targets(args.targets, market=args.market)
    validate_refresh_options(args, targets)
    end_date = parse_date_arg(args.end_date)
    stock_codes = parse_symbols_arg(getattr(args, "symbols", None))
    if stock_codes is None:
        stock_codes = download_workflow._stock_codes()
    state = RefreshState.open(
        resume_state_path(args),
        signature=resume_signature(args, end_date, targets),
        resume=args.resume,
        enabled=not args.dry_run,
    )
    progress = ProgressTracker(refresh_step_names(targets))
    effective_krx_end_date = (
        resolve_krx_effective_end_date(end_date)
        if "market-data" in targets
        else None
    )
    client = None
    if not args.skip_clickhouse and not args.dry_run:
        client = get_clickhouse_client()

    try:
        if "market-data" in targets:
            completed_market_window = state.step_window("market-data")
            market_step_is_current = (
                state.is_step_completed("market-data")
                and completed_market_window is not None
                and completed_market_window.end_date == effective_krx_end_date
            )
            if market_step_is_current:
                market_window = completed_market_window
                print("[RESUME] skipping completed step: market-data", flush=True)
            else:
                if state.is_step_completed("market-data"):
                    print(
                        "[RESUME] reopening completed step: market-data "
                        f"saved_end={completed_market_window.end_date if completed_market_window else '-'} "
                        f"effective_end={effective_krx_end_date}",
                        flush=True,
                    )
                    state.reset_step("market-data")
                progress.begin("market-data")
                market_window = run_market_data_refresh(
                    args,
                    stock_codes,
                    end_date,
                    client,
                    state,
                    effective_end_date=effective_krx_end_date,
                )
                state.complete_step("market-data", market_window)
                progress.done("market-data")
        else:
            market_window = build_refresh_window(
                latest_krx_bronze_date("price"),
                end_date=effective_krx_end_date or end_date,
                force_full=args.force_full,
            )

        dart_window = None
        if {"filings", "business-info"} & targets:
            dart_window = build_refresh_window(
                latest_dart_metadata_report_date(),
                end_date=end_date,
                force_full=args.force_full,
            )

        if "filings" in targets:
            if state.is_step_completed("filings"):
                print("[RESUME] skipping completed step: filings", flush=True)
            else:
                progress.begin("filings")
                run_filing_refresh(args, stock_codes, dart_window, client, state)
                state.complete_step("filings", dart_window)
                progress.done("filings")
        if "business-info" in targets:
            if state.is_step_completed("business-info"):
                print("[RESUME] skipping completed step: business-info", flush=True)
            else:
                progress.begin("business-info")
                run_business_info_refresh(args, stock_codes, dart_window, state)
                state.complete_step("business-info", dart_window)
                progress.done("business-info")
        if "dividends" in targets:
            dividend_window = build_refresh_window(
                latest_dividend_date(),
                end_date=end_date,
                force_full=args.force_full,
            )
            if state.is_step_completed("dividends"):
                print("[RESUME] skipping completed step: dividends", flush=True)
            else:
                progress.begin("dividends")
                run_dividend_refresh(args, stock_codes, dividend_window, client, state)
                state.complete_step("dividends", dividend_window)
                progress.done("dividends")
        if "consensus" in targets:
            if state.is_step_completed("consensus"):
                print("[RESUME] skipping completed step: consensus", flush=True)
            else:
                progress.begin("consensus")
                run_consensus_refresh(args, end_date, client, state)
                state.complete_step("consensus")
                progress.done("consensus")
        if "benchmarks-wacc" in targets:
            if state.is_step_completed("benchmarks-wacc"):
                print("[RESUME] skipping completed step: benchmarks-wacc", flush=True)
            else:
                progress.begin("benchmarks-wacc")
                run_benchmark_wacc_refresh(args, end_date, client, state)
                state.complete_step("benchmarks-wacc")
                progress.done("benchmarks-wacc")
        if "operating-metrics" in targets:
            if state.is_step_completed("operating-metrics"):
                print("[RESUME] skipping completed step: operating-metrics", flush=True)
            else:
                progress.begin("operating-metrics")
                run_operating_metrics_refresh(args, stock_codes)
                state.complete_step("operating-metrics")
                progress.done("operating-metrics")
        if "factors" in targets:
            if state.is_step_completed("factors"):
                print("[RESUME] skipping completed step: factors", flush=True)
            else:
                progress.begin("factors")
                run_factor_refresh(args, market_window, client, state)
                state.complete_step("factors", market_window)
                progress.done("factors")
        if "snapshots" in targets:
            if state.is_step_completed("snapshots"):
                print("[RESUME] skipping completed step: snapshots", flush=True)
            else:
                progress.begin("snapshots")
                run_factor_snapshot_refresh(args, client)
                state.complete_step("snapshots")
                progress.done("snapshots")
    finally:
        if client is not None:
            client.close()


def run_us_refresh(args: argparse.Namespace) -> None:
    targets = expand_targets(args.targets, market="us")
    validate_refresh_options(args, targets)
    end_date = parse_date_arg(args.end_date)
    symbols = parse_symbols_arg(getattr(args, "symbols", None))
    state = RefreshState.open(
        resume_state_path(args),
        signature=resume_signature(args, end_date, targets),
        resume=args.resume,
        enabled=not args.dry_run,
    )
    symbols = resolve_us_refresh_symbols(
        symbols,
        targets=targets,
        state=state,
        dry_run=args.dry_run,
    )
    progress = ProgressTracker(refresh_step_names(targets))
    client = None
    if not args.skip_clickhouse and not args.dry_run:
        client = get_clickhouse_client()

    try:
        if "filings" in targets:
            if state.is_step_completed("filings"):
                print("[RESUME] skipping completed step: filings", flush=True)
            else:
                progress.begin("filings")
                run_us_filing_refresh(args, symbols, end_date, client)
                state.complete_step("filings")
                progress.done("filings")

        if "market-data" in targets:
            if state.is_step_completed("market-data"):
                print("[RESUME] skipping completed step: market-data", flush=True)
                market_window = build_refresh_window(
                    latest_us_bronze_date(symbols=symbols),
                    end_date=end_date,
                    force_full=args.force_full,
                )
            else:
                progress.begin("market-data")
                market_window = run_us_market_data_refresh(
                    args,
                    symbols,
                    end_date,
                    client,
                )
                state.complete_step("market-data", market_window)
                progress.done("market-data")
        else:
            market_window = build_refresh_window(
                latest_us_bronze_date(symbols=symbols),
                end_date=end_date,
                force_full=args.force_full,
            )

        if "dividends" in targets:
            if state.is_step_completed("dividends"):
                print("[RESUME] skipping completed step: dividends", flush=True)
            else:
                progress.begin("dividends")
                run_us_dividend_refresh(args, client)
                state.complete_step("dividends")
                progress.done("dividends")

        if "benchmarks-wacc" in targets:
            if state.is_step_completed("benchmarks-wacc"):
                print("[RESUME] skipping completed step: benchmarks-wacc", flush=True)
            else:
                progress.begin("benchmarks-wacc")
                run_benchmark_wacc_refresh(args, end_date, client, state)
                state.complete_step("benchmarks-wacc")
                progress.done("benchmarks-wacc")

        if "factors" in targets:
            if state.is_step_completed("factors"):
                print("[RESUME] skipping completed step: factors", flush=True)
            else:
                progress.begin("factors")
                run_factor_refresh(args, market_window, client, state)
                state.complete_step("factors", market_window)
                progress.done("factors")

        if "snapshots" in targets:
            if state.is_step_completed("snapshots"):
                print("[RESUME] skipping completed step: snapshots", flush=True)
            else:
                progress.begin("snapshots")
                run_factor_snapshot_refresh(args, client)
                state.complete_step("snapshots")
                progress.done("snapshots")
    finally:
        if client is not None:
            client.close()


def run_us_filing_refresh(
    args: argparse.Namespace,
    symbols: list[str] | None,
    end_date: str,
    client: Any,
) -> None:
    if args.dry_run:
        print(
            f"[DRY-RUN] US filings symbols={len(symbols) if symbols else 'ALL'}, "
            f"end={_to_iso_date(end_date)}"
        )
        return

    from engine.extractors.sec_filings import (
        download_sec_company_tickers,
        download_us_companyfacts,
    )
    from engine.transformers.sec_filings import normalize_us_sec_filings

    download_sec_company_tickers()
    download_us_companyfacts(
        symbols=symbols,
        force=True,
        sleep_seconds=max(0.1, float(args.sleep_seconds or 0.0)),
    )
    end_year = int(end_date[:4])
    written = normalize_us_sec_filings(
        symbols=symbols,
        start_year=end_year - 10,
        end_year=end_year,
        workers=args.workers,
        progress_interval=args.progress_interval,
    )
    if not written:
        raise RuntimeError("US filing normalization produced no statement files")
    if not args.skip_clickhouse:
        filing_loader.insert_report_metadata(market="us", client=client)


def run_us_market_data_refresh(
    args: argparse.Namespace,
    symbols: list[str] | None,
    end_date: str,
    client: Any,
) -> RefreshWindow:
    latest = latest_us_bronze_date(symbols=symbols)
    window = build_refresh_window(
        latest,
        end_date=end_date,
        force_full=args.force_full,
    )
    if args.dry_run:
        print(
            f"[DRY-RUN] US market-data symbols={len(symbols) if symbols else 'ALL'}, "
            f"start={window.start_date or '-'}, end={window.end_date}"
        )
        return window

    from engine.extractors.market_prices import download_us_price_histories

    download_us_price_histories(
        symbols=symbols,
        force=args.force_full,
        sleep_seconds=args.sleep_seconds,
        start_date=DEFAULT_START_DATE,
        end_date=end_date,
    )
    price_frame = market_loader.create_price_dataframe(
        market="us",
        source="bronze",
        progress_interval=args.progress_interval,
    )
    shares_frame = market_loader.create_shares_dataframe(
        market="us",
        source="bronze",
    )
    if price_frame.empty:
        raise RuntimeError("US market-data normalization produced no price rows")

    if not args.skip_clickhouse:
        load_securities(args, client)
        load_market_table(
            args,
            client,
            table_name=market_loader.PRICE_TABLE,
            create_frame=lambda: price_frame,
            insert_frame=lambda frame: market_loader._insert_partitioned(
                client,
                market_loader.PRICE_TABLE,
                frame,
            ),
            window=window,
        )
        if not shares_frame.empty:
            load_market_table(
                args,
                client,
                table_name=market_loader.SHARES_TABLE,
                create_frame=lambda: shares_frame,
                insert_frame=lambda frame: market_loader._insert_partitioned(
                    client,
                    market_loader.SHARES_TABLE,
                    frame,
                ),
                window=window,
            )
    return window


def run_us_dividend_refresh(args: argparse.Namespace, client: Any) -> None:
    if args.dry_run:
        print("[DRY-RUN] US dividends normalize/load")
        return
    frame = dividend_loader.refresh_silver_dividend_files(market="us")
    insert_frame = dividend_loader.prepare_stock_dividend_for_clickhouse(frame)
    if insert_frame.empty:
        raise RuntimeError("US dividend normalization produced no rows")
    if not args.skip_clickhouse:
        market_scoped_delete(
            client,
            dividend_loader.STOCK_DIVIDEND_TABLE,
            market="us",
            start_date=min(insert_frame["trade_date"]),
            end_date=max(insert_frame["trade_date"]),
        )
        insert_partitioned_frame(
            client,
            dividend_loader.STOCK_DIVIDEND_TABLE,
            insert_frame,
            dividend_loader.STOCK_DIVIDEND_COLUMNS,
        )


def run_consensus_refresh(
    args: argparse.Namespace,
    end_date: str,
    client: Any,
    state: RefreshState,
) -> None:
    if args.dry_run:
        print(f"[DRY-RUN] KR consensus end={_to_iso_date(end_date)}")
        return
    end_year = int(end_date[:4])
    namespace = argparse.Namespace(
        market="kr",
        start_date=f"{end_year}0101",
        end_date=end_date,
        consensus_sources=getattr(
            args,
            "consensus_sources",
            "hankyung,valuefinder,equity",
        ),
        consensus_html_pages=getattr(args, "consensus_html_pages", 1),
        hankyung_token=getattr(args, "hankyung_token", None),
        valuefinder_cookie=getattr(args, "valuefinder_cookie", None),
        equity_cookie=getattr(args, "equity_cookie", None),
        force=True,
        sleep_seconds=args.sleep_seconds,
    )
    download_workflow.download_kr_consensus(namespace)
    normalize_namespace = argparse.Namespace(
        market="kr",
        consensus_stale_days=getattr(args, "consensus_stale_days", 180),
    )
    result = get_normalize_workflow().normalize_consensus(normalize_namespace)
    if not result:
        raise RuntimeError("KR consensus normalization produced no outputs")
    if not args.skip_clickhouse:
        from engine.loaders.consensus import load_hankyung_consensus

        counts = load_hankyung_consensus(market="kr", client=client)
        if not any(counts.values()):
            raise RuntimeError("KR consensus load prepared no rows")


def run_benchmark_wacc_refresh(
    args: argparse.Namespace,
    end_date: str,
    client: Any,
    state: RefreshState,
) -> None:
    if args.dry_run:
        print(
            f"[DRY-RUN] benchmark/WACC market={args.market}, "
            f"end={_to_iso_date(end_date)}"
        )
        return

    from engine.extractors.erp import (
        download_damodaran_country_erp,
        download_fred_series,
        download_us_sp500_benchmark,
        FRED_SERIES_IDS,
    )
    from engine.loaders import benchmarks as benchmark_loader
    from engine.transformers.erp import (
        normalize_country_erp,
        normalize_fred_risk_free_rates,
    )
    from engine.transformers.wacc import (
        create_default_wacc_assumptions,
        normalize_market_benchmark_weekly_returns,
    )

    benchmark_loader.download_benchmark_prices(
        market=args.market,
        start_date=_to_iso_date(DEFAULT_START_DATE),
        end_date=_to_iso_date(end_date),
    )
    download_damodaran_country_erp()
    fred_paths = [
        download_fred_series(series_id)
        for series_id in FRED_SERIES_IDS.values()
    ]
    if args.market == "us":
        download_us_sp500_benchmark(
            start_date=_to_iso_date(DEFAULT_START_DATE),
            end_date=_to_iso_date(end_date),
        )

    benchmark_frame = benchmark_loader.normalize_downloaded_benchmark_prices(
        market=args.market,
    )
    normalize_country_erp()
    normalize_fred_risk_free_rates(fred_paths)
    create_default_wacc_assumptions()
    normalize_market_benchmark_weekly_returns(args.market)
    if benchmark_frame.empty:
        raise RuntimeError(
            f"benchmark normalization produced no rows for market={args.market}"
        )
    if not args.skip_clickhouse:
        benchmark_loader.insert_benchmark_prices(
            market=args.market,
            source="bronze",
            start_date=_to_iso_date(DEFAULT_START_DATE),
            end_date=_to_iso_date(end_date),
            client=client,
        )


def run_operating_metrics_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
) -> None:
    if args.dry_run:
        print(
            f"[DRY-RUN] KR operating metrics stocks={len(stock_codes):,}, "
            f"load={not args.skip_clickhouse}"
        )
        return
    from engine.workflows.operating_metrics import run_operating_metrics_workflow

    output = run_operating_metrics_workflow(
        stock_codes,
        load=not args.skip_clickhouse,
        dry_run=False,
        progress_interval=args.progress_interval,
        fail_fast=True,
    )
    if not output.get("transform_results"):
        raise RuntimeError("KR operating metric workflow produced no outputs")

def resume_state_path(args: argparse.Namespace) -> Path:
    if getattr(args, "resume_state_path", None):
        return Path(args.resume_state_path)
    return DATA_LAKE.meta("refresh_state", f"{args.market}_refresh_state.json")


def resume_signature(args: argparse.Namespace, end_date: str, targets: set[str]) -> dict[str, Any]:
    return {
        "market": args.market,
        "targets": refresh_step_names(targets),
        "end_date": end_date,
        "force_full": bool(args.force_full),
        "skip_clickhouse": bool(args.skip_clickhouse),
        "clickhouse_mode": args.clickhouse_mode,
        "financial_basis": args.financial_basis,
        "symbols": sorted(parse_symbols_arg(getattr(args, "symbols", None)) or []),
        "complete_universe_ratio": float(
            getattr(args, "complete_universe_ratio", 0.99)
        ),
        "consensus_sources": getattr(args, "consensus_sources", None),
    }


def refresh_window_to_state(window: RefreshWindow) -> dict[str, str | None]:
    return {
        "start_date": window.start_date,
        "end_date": window.end_date,
        "latest_date": window.latest_date.isoformat() if window.latest_date else None,
    }


def refresh_window_from_state(raw: dict[str, Any]) -> RefreshWindow:
    latest = raw.get("latest_date")
    return RefreshWindow(
        raw.get("start_date"),
        raw["end_date"],
        date.fromisoformat(latest) if latest else None,
    )

def expand_targets(target: str, *, market: str = "kr") -> set[str]:
    if target == "all":
        if market == "kr":
            return {
                "market-data",
                "filings",
                "business-info",
                "dividends",
                "consensus",
                "benchmarks-wacc",
                "operating-metrics",
                "factors",
                "snapshots",
            }
        return {
            "market-data",
            "filings",
            "dividends",
            "benchmarks-wacc",
            "factors",
            "snapshots",
        }
    if market == "us" and target in {
        "business-info",
        "consensus",
        "operating-metrics",
    }:
        raise ValueError(f"target={target} is not supported for market=us")
    return {target}


def refresh_step_names(targets: set[str]) -> list[str]:
    ordered = [
        "market-data",
        "filings",
        "business-info",
        "dividends",
        "consensus",
        "benchmarks-wacc",
        "operating-metrics",
        "factors",
        "snapshots",
    ]
    return [name for name in ordered if name in targets]

def run_market_data_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
    end_date: str,
    client: Any,
    state: RefreshState,
    *,
    effective_end_date: str | None = None,
) -> RefreshWindow:
    effective_end_date = (
        parse_date_arg(effective_end_date)
        if effective_end_date is not None
        else resolve_krx_effective_end_date(end_date)
    )
    print(
        f"[INFO] KRX trading calendar requested_end_date={parse_date_arg(end_date)} "
        f"effective_end_date={effective_end_date}",
        flush=True,
    )
    price_window = download_incremental_krx_dataset(
        "price",
        stock_codes,
        end_date=effective_end_date,
        force_full=args.force_full,
        dry_run=args.dry_run,
        progress_interval=args.progress_interval,
        workers=args.workers,
        state=state,
    )
    share_window = download_incremental_krx_dataset(
        "shares",
        stock_codes,
        end_date=effective_end_date,
        force_full=args.force_full,
        dry_run=args.dry_run,
        progress_interval=args.progress_interval,
        workers=args.workers,
        state=state,
    )
    window = earliest_window(price_window, share_window)

    if not args.dry_run:
        normalize_price(str(DATA_LAKE.bronze("krx", "price", "*")))
        normalize_shares(str(DATA_LAKE.bronze("krx", "shares", "*")))

    if not args.skip_clickhouse:
        load_securities(args, client)
        load_market_table(
            args,
            client,
            table_name=market_loader.PRICE_TABLE,
            create_frame=lambda: market_loader.create_price_dataframe(market="kr", source="silver"),
            insert_frame=lambda frame: market_loader._insert_partitioned(client, market_loader.PRICE_TABLE, frame),
            window=price_window,
        )
        load_market_table(
            args,
            client,
            table_name=market_loader.SHARES_TABLE,
            create_frame=lambda: market_loader.create_shares_dataframe(market="kr", source="silver"),
            insert_frame=lambda frame: market_loader._insert_partitioned(client, market_loader.SHARES_TABLE, frame),
            window=share_window,
        )

    return window


def run_resumable_stock_tasks(
    dataset: str,
    stock_codes: list[str],
    *,
    state: RefreshState,
    dry_run: bool,
    workers: int,
    progress_interval: int,
    action: Callable[[str, int], None],
) -> None:
    sorted_codes = sorted(str(code) for code in stock_codes)
    total = len(sorted_codes)
    planned = 0
    completed = 0
    skipped = 0
    started_at = time.monotonic()
    tasks: list[tuple[str, int]] = []
    resumed_symbols = state.completed_symbols(dataset)

    for original_offset, stock_code in enumerate(sorted_codes):
        if stock_code in resumed_symbols:
            skipped += 1
            completed += 1
            continue
        planned += 1
        tasks.append((stock_code, original_offset))

    if skipped:
        print(
            f"[RESUME] {dataset} completed_symbols={skipped:,}/{total:,}, "
            f"remaining_symbols={len(tasks):,}",
            flush=True,
        )

    if dry_run:
        print(f"[DRY-RUN] {dataset} planned_symbols={planned:,}, skipped_symbols={skipped:,}")
        return

    if not tasks:
        print(f"[RESUME] {dataset} all symbols completed", flush=True)
        return

    worker_count = max(1, int(workers or 1))
    downloaded = 0
    print(
        f"[INFO] {dataset} processing planned_symbols={len(tasks):,}, workers={worker_count}",
        flush=True,
    )
    if worker_count == 1:
        for stock_code, original_offset in tasks:
            action(stock_code, original_offset)
            state.complete_symbol(dataset, stock_code)
            downloaded += 1
            completed += 1
            maybe_print_symbol_progress(dataset, completed, total, planned, downloaded, skipped, progress_interval, started_at)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_symbol = {
                executor.submit(action, stock_code, original_offset): stock_code
                for stock_code, original_offset in tasks
            }
            for future in as_completed(future_to_symbol):
                stock_code = future_to_symbol[future]
                future.result()
                state.complete_symbol(dataset, stock_code)
                downloaded += 1
                completed += 1
                maybe_print_symbol_progress(dataset, completed, total, planned, downloaded, skipped, progress_interval, started_at)

    print(
        f"[DONE] {dataset} planned_symbols={planned:,}, "
        f"completed_symbols={downloaded:,}, skipped_symbols={skipped:,}",
        flush=True,
    )


def run_once_with_state(
    state: RefreshState,
    step: str,
    window: RefreshWindow,
    *,
    dry_run: bool,
    action: Callable[[], Any],
) -> Any:
    if state.is_step_completed(step):
        print(f"[RESUME] skipping completed substep: {step}", flush=True)
        return None
    result = action()
    if not dry_run:
        state.complete_step(step, window)
    return result


def run_filing_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
    window: RefreshWindow,
    client: Any,
    state: RefreshState,
) -> None:
    if not window.has_work:
        print(f"[SKIP] filings already fresh through {window.end_iso}")
        return

    if args.dry_run:
        print(f"[DRY-RUN] filings start={window.start_date}, end={window.end_date}")
    else:
        run_resumable_stock_tasks(
            "filings-statements",
            stock_codes,
            state=state,
            dry_run=args.dry_run,
            workers=args.workers,
            progress_interval=args.progress_interval,
            action=lambda stock_code, original_offset: download_statements(
                [stock_code],
                0,
                start_date=window.start_date,
                end_date=window.end_date,
                display_offset_base=original_offset,
            ),
        )
        run_resumable_stock_tasks(
            "filings-comments",
            stock_codes,
            state=state,
            dry_run=args.dry_run,
            workers=args.workers,
            progress_interval=args.progress_interval,
            action=lambda stock_code, original_offset: download_statement_comments(
                [stock_code],
                0,
                start_date=window.start_date,
                end_date=window.end_date,
                display_offset_base=original_offset,
            ),
        )
        run_once_with_state(
            state,
            "filings-metadata",
            window,
            dry_run=args.dry_run,
            action=lambda: download_and_merge_report_metadata(
                stock_codes,
                start_date=window.start_date,
                end_date=window.end_date,
            ),
        )
        run_once_with_state(
            state,
            "filings-normalize",
            window,
            dry_run=args.dry_run,
            action=lambda: normalize_statements_for_years(
                int(window.start_date[:4]),
                int(window.end_date[:4]),
            ),
        )

    if not args.skip_clickhouse:
        run_once_with_state(
            state,
            "filings-clickhouse",
            window,
            dry_run=args.dry_run,
            action=lambda: load_report_metadata(args, client, window),
        )


def run_business_info_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
    window: RefreshWindow,
    state: RefreshState,
) -> None:
    if not window.has_work:
        print(f"[SKIP] business-info already fresh through {window.end_iso}")
        return

    if args.dry_run:
        print(f"[DRY-RUN] business-info start={window.start_date}, end={window.end_date}")
        return

    run_resumable_stock_tasks(
        "business-info",
        stock_codes,
        state=state,
        dry_run=args.dry_run,
        workers=args.workers,
        progress_interval=args.progress_interval,
        action=lambda stock_code, original_offset: download_business_infos(
            [stock_code],
            0,
            start_date=window.start_date,
            end_date=window.end_date,
            max_workers=1,
            force=False,
            sleep_seconds=args.sleep_seconds,
            stock_retries=args.stock_retries,
            stock_retry_backoff=args.stock_retry_backoff,
            display_offset_base=original_offset,
            fail_fast=True,
        ),
    )
    run_once_with_state(
        state,
        "business-info-normalize",
        window,
        dry_run=args.dry_run,
        action=lambda: get_normalize_workflow().normalize_business_infos(
            start_year=int(window.start_date[:4]),
            end_year=int(window.end_date[:4]),
            workers=args.workers,
        ),
    )


def run_dividend_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
    window: RefreshWindow,
    client: Any,
    state: RefreshState,
) -> None:
    if window.has_work:
        if args.dry_run:
            print(f"[DRY-RUN] dividends start={window.start_date}, end={window.end_date}")
        else:
            run_resumable_stock_tasks(
                "dividends",
                stock_codes,
                state=state,
                dry_run=args.dry_run,
                workers=args.workers,
                progress_interval=args.progress_interval,
                action=lambda stock_code, original_offset: download_dividend_histories(
                    [stock_code],
                    0,
                    start_date=window.start_date,
                    end_date=window.end_date,
                    force=bool(getattr(args, "force_full", False)),
                    display_offset_base=original_offset,
                ),
            )
    else:
        print(f"[SKIP] dividends already fresh through {window.end_iso}")

    full_frame = pd.DataFrame()
    if not args.dry_run:
        output_path = dividend_loader.dividend_output_path("kr")
        if state.is_step_completed("dividends-silver") and output_path.exists():
            print("[RESUME] skipping completed substep: dividends-silver", flush=True)
            full_frame = pd.read_csv(output_path)
        else:
            full_frame = dividend_loader.refresh_silver_dividend_files(market="kr")
            state.complete_step("dividends-silver", window)

    if args.skip_clickhouse:
        return

    insert_frame = dividend_loader.prepare_stock_dividend_for_clickhouse(full_frame)
    candidate = filter_frame_by_window(insert_frame, "trade_date", window)
    run_once_with_state(
        state,
        "dividends-clickhouse",
        window,
        dry_run=args.dry_run,
        action=lambda: replace_market_frame(
            args,
            client,
            table_name=dividend_loader.STOCK_DIVIDEND_TABLE,
            full_frame=insert_frame,
            candidate_frame=candidate,
            column_names=dividend_loader.STOCK_DIVIDEND_COLUMNS,
            date_column="trade_date",
        ),
    )


def run_factor_refresh(args: argparse.Namespace, window: RefreshWindow, client: Any, state: RefreshState) -> None:
    if args.skip_clickhouse:
        print("[SKIP] factors require ClickHouse")
        return

    if state.is_step_completed("factors-insert"):
        print("[RESUME] skipping completed substep: factors-insert", flush=True)
        return

    market = str(getattr(args, "market", "kr")).lower()
    if market == "kr" and not args.dry_run:
        ensure_krx_silver_market_data_current()
    as_of_date = resolve_latest_complete_trade_date(
        market,
        ratio=float(getattr(args, "complete_universe_ratio", 0.99)),
    )
    if args.dry_run:
        print(
            f"[DRY-RUN] factors market={market}, "
            f"latest_complete_date={as_of_date.isoformat()}"
        )
        return

    run_once_with_state(
        state,
        "factors-catalog",
        window,
        dry_run=False,
        action=lambda: load_factor_catalog(args, client),
    )
    latest_factor_date = latest_market_table_date(
        client,
        "fact_daily_factors",
        market=market,
        financial_basis=args.financial_basis,
    )
    if bool(getattr(args, "force_full", False)):
        start_date = _to_iso_date(DEFAULT_START_DATE)
    elif latest_factor_date is None or latest_factor_date >= as_of_date:
        start_date = as_of_date.isoformat()
    else:
        start_date = (latest_factor_date + timedelta(days=1)).isoformat()
    if window.has_work and window.start_iso and latest_factor_date is not None:
        start_date = min(start_date, window.start_iso)

    market_scoped_delete(
        client,
        "fact_daily_factors",
        market=market,
        start_date=start_date,
        end_date=as_of_date,
        financial_basis=args.financial_basis,
        symbols=parse_symbols_arg(getattr(args, "symbols", None)),
    )
    factor_result = factor_loader.insert_daily_factors(
        stock_codes=parse_symbols_arg(getattr(args, "symbols", None)),
        financial_basis=args.financial_basis,
        start_date=start_date,
        end_date=as_of_date.isoformat(),
        market=market,
        insert_catalog=False,
        client=client,
        parallel_workers=args.workers,
    )
    inserted_rows = int(factor_result.attrs.get("inserted_rows", 0))
    if inserted_rows <= 0:
        raise RuntimeError(
            f"factor refresh produced no rows for market={market}, "
            f"date={start_date}..{as_of_date.isoformat()}"
        )
    state.complete_step(
        "factors-insert",
        RefreshWindow(
            start_date.replace("-", ""),
            as_of_date.strftime("%Y%m%d"),
            latest_factor_date,
        ),
    )


def run_factor_snapshot_refresh(args: argparse.Namespace, client: Any) -> None:
    if args.skip_clickhouse:
        print("[SKIP] factor snapshots require ClickHouse")
        return
    market = str(args.market).lower()
    as_of_date = resolve_latest_complete_trade_date(
        market,
        ratio=float(getattr(args, "complete_universe_ratio", 0.99)),
    )
    if args.dry_run:
        print(
            f"[DRY-RUN] factor snapshots market={market}, "
            f"latest_complete_date={as_of_date.isoformat()}"
        )
        return

    latest_snapshot_date = latest_market_table_date(
        client,
        factor_snapshot_loader.FACTOR_SNAPSHOT_TABLE,
        market=market,
        financial_basis=args.financial_basis,
    )
    start_date = (
        as_of_date
        if latest_snapshot_date is None or latest_snapshot_date >= as_of_date
        else latest_snapshot_date + timedelta(days=1)
    )
    market_scoped_delete(
        client,
        factor_snapshot_loader.FACTOR_SNAPSHOT_TABLE,
        market=market,
        start_date=start_date,
        end_date=as_of_date,
        financial_basis=args.financial_basis,
    )
    row_count = factor_snapshot_loader.insert_factor_snapshots(
        market=market,
        start_date=start_date,
        end_date=as_of_date,
        financial_basis=args.financial_basis,
        max_threads=min(max(1, int(args.workers or 1)), 2),
        client=client,
    )
    if row_count <= 0:
        raise RuntimeError(
            f"factor snapshot refresh produced no rows for market={market}, "
            f"date={as_of_date.isoformat()}"
        )


def ensure_krx_silver_market_data_current() -> bool:
    bronze_latest = latest_krx_bronze_date("price")
    silver_price_path = DATA_LAKE.silver(
        "krx",
        "price",
        market_csv_name("normalized_price"),
    )
    silver_latest = latest_date_in_csv(silver_price_path)
    if bronze_latest is None or (
        silver_latest is not None and silver_latest >= bronze_latest
    ):
        return False

    print(
        "[INFO] KRX Silver market data is stale; "
        f"bronze_latest={bronze_latest.isoformat()}, "
        f"silver_latest={silver_latest.isoformat() if silver_latest else '-'}",
        flush=True,
    )
    normalize_price(str(DATA_LAKE.bronze("krx", "price", "*")))
    normalize_shares(str(DATA_LAKE.bronze("krx", "shares", "*")))
    return True

def load_securities(args: argparse.Namespace, client: Any) -> None:
    for table_name, target in SECURITY_TABLES.items():
        if args.dry_run:
            print(f"[DRY-RUN] securities table={table_name}, mode=market-upsert")
            continue
        security_loader.insert_securities(
            market=args.market,
            target=target,
            client=client,
        )


def load_market_table(
    args: argparse.Namespace,
    client: Any,
    *,
    table_name: str,
    create_frame: Callable[[], pd.DataFrame],
    insert_frame: Callable[[pd.DataFrame], int],
    window: RefreshWindow,
) -> None:
    if args.dry_run:
        print(
            f"[DRY-RUN] market table={table_name}, "
            f"mode={args.clickhouse_mode}, window={window}"
        )
        return

    full_frame = filter_frame_by_symbols(
        create_frame(),
        market=args.market,
        symbols=parse_symbols_arg(getattr(args, "symbols", None)),
    )
    candidate = filter_frame_by_window(full_frame, TABLE_DATE_COLUMNS[table_name], window)
    frame = full_frame if args.clickhouse_mode == "always-truncate" else candidate
    if frame.empty:
        return
    if args.clickhouse_mode != "append-only":
        dates = pd.to_datetime(
            frame[TABLE_DATE_COLUMNS[table_name]],
            errors="coerce",
        ).dropna()
        if not dates.empty:
            market_scoped_delete(
                client,
                table_name,
                market=args.market,
                start_date=dates.min().date(),
                end_date=dates.max().date(),
                symbols=parse_symbols_arg(getattr(args, "symbols", None)),
            )
    insert_frame(frame)


def load_report_metadata(args: argparse.Namespace, client: Any, window: RefreshWindow) -> None:
    full_frame = filter_frame_by_symbols(
        filing_loader.read_report_metadata(market=args.market),
        market=args.market,
        symbols=parse_symbols_arg(getattr(args, "symbols", None)),
    )
    candidate = filter_frame_by_window(full_frame, "report_date", window)
    frame = full_frame if args.clickhouse_mode == "always-truncate" else candidate
    if frame.empty:
        return
    if args.clickhouse_mode != "append-only":
        dates = pd.to_datetime(frame["report_date"], errors="coerce").dropna()
        if not dates.empty:
            market_scoped_delete(
                client,
                "dart_report_metadata",
                market=args.market,
                start_date=dates.min().date(),
                end_date=dates.max().date(),
                symbols=parse_symbols_arg(getattr(args, "symbols", None)),
            )
    client.insert_df(
        "dart_report_metadata",
        frame,
        column_names=list(frame.columns),
    )


def load_factor_catalog(args: argparse.Namespace, client: Any) -> None:
    if args.dry_run:
        print("[DRY-RUN] factor_catalog upsert")
        return
    factor_loader.insert_factor_catalog(client, factor_ids=factor_loader.preferred_factor_columns())


def replace_market_frame(
    args: argparse.Namespace,
    client: Any,
    *,
    table_name: str,
    full_frame: pd.DataFrame,
    candidate_frame: pd.DataFrame,
    column_names: list[str],
    date_column: str,
) -> int:
    symbols = parse_symbols_arg(getattr(args, "symbols", None))
    full_frame = filter_frame_by_symbols(
        full_frame,
        market=args.market,
        symbols=symbols,
    )
    candidate_frame = filter_frame_by_symbols(
        candidate_frame,
        market=args.market,
        symbols=symbols,
    )
    frame = full_frame if args.clickhouse_mode == "always-truncate" else candidate_frame
    if frame.empty:
        return 0
    if args.clickhouse_mode != "append-only":
        dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not dates.empty:
            market_scoped_delete(
                client,
                table_name,
                market=args.market,
                start_date=dates.min().date(),
                end_date=dates.max().date(),
                symbols=parse_symbols_arg(getattr(args, "symbols", None)),
            )
    client.insert_df(table_name, frame, column_names=column_names)
    return len(frame)


def insert_partitioned_frame(
    client: Any,
    table_name: str,
    frame: pd.DataFrame,
    column_names: list[str],
) -> int:
    if frame.empty:
        return 0
    working = frame.copy()
    working["_partition"] = pd.to_datetime(
        working["trade_date"],
        errors="coerce",
    ).dt.strftime("%Y%m")
    inserted = 0
    for _, chunk in working.dropna(subset=["_partition"]).groupby(
        "_partition",
        sort=True,
    ):
        chunk = chunk.drop(columns=["_partition"]).copy()
        client.insert_df(table_name, chunk, column_names=column_names)
        inserted += len(chunk)
    return inserted


def market_scoped_delete(
    client: Any,
    table_name: str,
    *,
    market: str,
    start_date: str | date,
    end_date: str | date,
    financial_basis: str | None = None,
    symbols: list[str] | None = None,
) -> None:
    validate_table_name(table_name)
    normalized_market = str(market).strip().lower()
    if normalized_market not in {"kr", "us"}:
        raise ValueError("market must be 'kr' or 'us'")
    prefix = "SEC_KR_" if normalized_market == "kr" else "SEC_US_"
    date_column = TABLE_DATE_COLUMNS.get(table_name, "trade_date")
    parameters: dict[str, Any] = {
        "security_prefix": prefix,
        "start_date": _to_iso_date(start_date),
        "end_date": _to_iso_date(end_date),
    }
    filters = [
        "startsWith(security_id, {security_prefix:String})",
        f"{date_column} >= {{start_date:Date}}",
        f"{date_column} <= {{end_date:Date}}",
    ]
    if financial_basis:
        parameters["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    if symbols:
        security_ids = normalized_security_ids(normalized_market, symbols)
        parameters["security_ids"] = sorted(set(security_ids))
        filters.append("has({security_ids:Array(String)}, security_id)")
    sql = (
        f"ALTER TABLE {table_name} DELETE WHERE "
        f"{' AND '.join(filters)} SETTINGS mutations_sync = 2"
    )
    command = getattr(client, "command", None)
    if callable(command):
        try:
            command(sql, parameters=parameters)
        except TypeError:
            command(sql)
        return
    client.query(sql, parameters=parameters)


def latest_market_table_date(
    client: Any,
    table_name: str,
    *,
    market: str,
    financial_basis: str | None = None,
) -> date | None:
    validate_table_name(table_name)
    prefix = "SEC_KR_" if str(market).lower() == "kr" else "SEC_US_"
    params: dict[str, Any] = {"security_prefix": prefix}
    filters = ["startsWith(security_id, {security_prefix:String})"]
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    query = (
        f"SELECT max(trade_date) AS latest_date FROM {table_name} "
        f"WHERE {' AND '.join(filters)}"
    )
    if hasattr(client, "query_df"):
        frame = client.query_df(query, parameters=params)
        value = None if frame.empty else frame.iloc[0, 0]
    else:
        result = client.query(query, parameters=params)
        rows = getattr(result, "result_rows", None) or []
        value = rows[0][0] if rows else None
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def resolve_latest_complete_trade_date(
    market: str,
    *,
    ratio: float = 0.99,
    recent_sessions: int = 20,
) -> date:
    if not 0 < float(ratio) <= 1:
        raise ValueError("complete_universe_ratio must satisfy 0 < ratio <= 1")
    frame = market_loader.create_price_dataframe(
        market=market,
        source="silver",
    )
    if frame.empty:
        raise RuntimeError(f"no normalized price rows for market={market}")
    working = frame[["security_id", "trade_date"]].copy()
    working["trade_date"] = pd.to_datetime(
        working["trade_date"],
        errors="coerce",
    )
    counts = (
        working.dropna(subset=["trade_date"])
        .groupby("trade_date")["security_id"]
        .nunique()
        .sort_index()
        .tail(max(1, int(recent_sessions)))
    )
    if counts.empty:
        raise RuntimeError(f"no valid normalized price dates for market={market}")
    threshold = max(1, int(counts.max() * float(ratio) + 0.999999))
    candidates = counts.loc[counts >= threshold]
    if candidates.empty:
        raise RuntimeError(
            f"no complete price cross-section for market={market}, ratio={ratio}"
        )
    return candidates.index.max().date()


def latest_us_bronze_date(symbols: list[str] | None = None) -> date | None:
    root = DATA_LAKE.bronze("yfinance", "price")
    if symbols:
        from engine.extractors._internal.yfinance_market_prices import (
            normalize_yfinance_ticker,
        )

        paths = [
            root / f"{normalize_yfinance_ticker(symbol)}.csv"
            for symbol in symbols
        ]
        if any(not path.exists() for path in paths):
            return None
    else:
        paths = sorted(root.glob("*.csv"))
    dates = [latest_date_in_csv(path) for path in paths if path.exists()]
    dates = [value for value in dates if value is not None]
    return min(dates) if dates else None


def resolve_us_refresh_symbols(
    symbols: list[str] | None,
    *,
    targets: set[str],
    state: RefreshState,
    dry_run: bool,
) -> list[str] | None:
    if symbols is not None or dry_run:
        return symbols
    source_targets = {"filings", "market-data"} & targets
    if not any(not state.is_step_completed(target) for target in source_targets):
        return None

    from engine.extractors.market_prices import download_us_equity_universe

    universe = download_us_equity_universe()
    if universe.empty or "ticker" not in universe.columns:
        raise RuntimeError("US equity universe download produced no symbols")
    resolved = sorted(
        {
            str(symbol).strip().upper()
            for symbol in universe["ticker"].dropna()
            if str(symbol).strip()
        }
    )
    if not resolved:
        raise RuntimeError("US equity universe download produced no symbols")
    print(f"[INFO] US refresh universe symbols={len(resolved):,}", flush=True)
    return resolved


def parse_symbols_arg(value: str | None) -> list[str] | None:
    if value is None or not str(value).strip():
        return None
    return sorted(
        {
            item.strip().upper()
            for item in str(value).split(",")
            if item.strip()
        }
    )


def normalized_security_ids(market: str, symbols: list[str]) -> list[str]:
    normalized_market = str(market).strip().lower()
    if normalized_market not in {"kr", "us"}:
        raise ValueError("market must be 'kr' or 'us'")
    prefix = "SEC_KR_" if normalized_market == "kr" else "SEC_US_"
    if normalized_market == "us":
        from engine.extractors._internal.yfinance_market_prices import (
            normalize_yfinance_ticker,
        )

        normalized_symbols = [
            normalize_yfinance_ticker(symbol)
            for symbol in symbols
        ]
    else:
        normalized_symbols = [
            str(symbol).strip().upper().zfill(6)
            if str(symbol).strip().isdigit()
            else str(symbol).strip().upper()
            for symbol in symbols
        ]
    return sorted(
        {
            f"{prefix}{symbol}"
            for symbol in normalized_symbols
            if symbol
        }
    )


def filter_frame_by_symbols(
    frame: pd.DataFrame,
    *,
    market: str,
    symbols: list[str] | None,
) -> pd.DataFrame:
    if frame.empty or not symbols or "security_id" not in frame.columns:
        return frame
    security_ids = set(normalized_security_ids(market, symbols))
    return frame.loc[frame["security_id"].astype(str).isin(security_ids)].copy()


def validate_refresh_options(
    args: argparse.Namespace,
    targets: set[str],
) -> None:
    ratio = float(getattr(args, "complete_universe_ratio", 0.99))
    if not 0 < ratio <= 1:
        raise ValueError("complete_universe_ratio must satisfy 0 < ratio <= 1")
    if (
        bool(getattr(args, "skip_clickhouse", False))
        and not bool(getattr(args, "dry_run", False))
        and {"factors", "snapshots"} & targets
    ):
        raise ValueError(
            "--skip-clickhouse cannot be combined with factors or snapshots"
        )


def load_dataframe_with_policy(
    args: argparse.Namespace,
    client: Any,
    *,
    table_name: str,
    full_frame: pd.DataFrame,
    candidate_frame: pd.DataFrame,
    column_names: list[str],
    window: RefreshWindow | None = None,
) -> int:
    should_reload = should_truncate_table(
        args,
        client,
        table_name,
        full_frame=full_frame,
        candidate_frame=candidate_frame,
        window=window,
    )
    if args.dry_run:
        print(
            f"[DRY-RUN] load table={table_name}, reload={should_reload}, "
            f"candidate_rows={len(candidate_frame):,}, full_rows={len(full_frame):,}"
        )
        return len(full_frame if should_reload else candidate_frame)
    if should_reload:
        truncate_table(client, table_name)
        frame = full_frame
    else:
        frame = candidate_frame
    if frame.empty:
        return 0
    client.insert_df(table_name, frame, column_names=column_names)
    return len(frame)


def should_truncate_table(
    args: argparse.Namespace,
    client: Any,
    table_name: str,
    *,
    full_frame: pd.DataFrame | None = None,
    candidate_frame: pd.DataFrame | None = None,
    window: RefreshWindow | None = None,
) -> bool:
    if args.clickhouse_mode == "append-only":
        return False
    if args.clickhouse_mode == "always-truncate":
        return True
    if args.dry_run and client is None:
        return False
    return table_has_overlap(
        client,
        table_name,
        frame=candidate_frame if candidate_frame is not None else full_frame,
        window=window,
    )


def table_has_overlap(
    client: Any,
    table_name: str,
    *,
    frame: pd.DataFrame | None = None,
    window: RefreshWindow | None = None,
) -> bool:
    validate_table_name(table_name)
    date_column = TABLE_DATE_COLUMNS.get(table_name)
    if date_column:
        start, end = overlap_date_range(frame, date_column=date_column, window=window)
        if start is None or end is None:
            return False
        query = (
            f"SELECT count() AS rows FROM {table_name} "
            f"WHERE {date_column} >= %(start)s AND {date_column} <= %(end)s"
        )
        return query_count(client, query, {"start": start, "end": end}) > 0

    return query_count(client, f"SELECT count() AS rows FROM {table_name}") > 0


def overlap_date_range(
    frame: pd.DataFrame | None,
    *,
    date_column: str,
    window: RefreshWindow | None,
) -> tuple[date | None, date | None]:
    if frame is not None and not frame.empty and date_column in frame.columns:
        dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not dates.empty:
            return dates.min().date(), dates.max().date()

    if window is not None and window.start_date is not None:
        return _parse_yyyymmdd(window.start_date), _parse_yyyymmdd(window.end_date)

    return None, None


def query_count(client: Any, query: str, parameters: dict[str, Any] | None = None) -> int:
    if hasattr(client, "query_df"):
        result = client.query_df(query, parameters=parameters or {})
        if result.empty:
            return 0
        return int(result.iloc[0, 0])
    result = client.query(query, parameters=parameters or {})
    rows = getattr(result, "result_rows", None) or []
    if not rows:
        return 0
    return int(rows[0][0])


def truncate_table(client: Any, table_name: str) -> None:
    validate_table_name(table_name)
    sql = f"TRUNCATE TABLE {table_name}"
    if hasattr(client, "command"):
        client.command(sql)
    else:
        client.query(sql)


def validate_table_name(table_name: str) -> str:
    if table_name not in KR_REFRESH_TABLES:
        raise ValueError(f"unsupported refresh table: {table_name}")
    return table_name


def download_incremental_krx_dataset(
    dataset: str,
    stock_codes: list[str],
    *,
    end_date: str,
    force_full: bool = False,
    dry_run: bool = False,
    progress_interval: int = 100,
    workers: int = 1,
    state: RefreshState | None = None,
) -> RefreshWindow:
    output_dir = DATA_LAKE.bronze("krx", dataset)
    latest_dates: list[date] = []
    saw_missing = False
    planned = 0
    downloaded = 0
    skipped = 0
    processed = 0
    total = len(stock_codes)
    started_at = time.monotonic()
    tasks: list[tuple[str, Path, RefreshWindow]] = []
    end = _parse_yyyymmdd(end_date)
    resumed_symbols = state.completed_symbols(dataset) if state is not None else set()
    state_window_key = f"bronze-{dataset}"
    resumed_window = state.step_window(state_window_key) if state is not None else None
    if resumed_window is not None and resumed_window.end_date != parse_date_arg(end_date):
        print(
            f"[RESUME] bronze {dataset} effective end date changed "
            f"from={resumed_window.end_date} to={parse_date_arg(end_date)}; "
            "revalidating symbols",
            flush=True,
        )
        resumed_window = None
        resumed_symbols = set()
        if state is not None:
            state.reset_symbols(dataset)
    resumed = 0

    for stock_code in sorted(stock_codes):
        path = output_dir / market_symbol_csv_name(stock_code)
        if stock_code in resumed_symbols and resumed_window is not None:
            resumed += 1
            skipped += 1
            processed += 1
            continue

        latest = None if force_full else latest_date_in_csv(path)
        if latest is not None:
            latest_dates.append(latest)
        else:
            saw_missing = True

        if (
            stock_code in resumed_symbols
            and latest is not None
            and latest >= end
        ):
            resumed += 1
            skipped += 1
            processed += 1
            continue

        window = build_refresh_window(latest, end_date=end_date, force_full=force_full)
        if not window.has_work:
            skipped += 1
            processed += 1
            continue

        planned += 1
        tasks.append((stock_code, path, window))

    if resumed:
        print(
            f"[RESUME] bronze {dataset} completed_symbols={resumed:,}/{total:,}, "
            f"remaining_symbols={len(tasks):,}",
            flush=True,
        )

    current_window = build_refresh_window(
        None if saw_missing else (min(latest_dates) if latest_dates else None),
        end_date=end_date,
        force_full=force_full,
    )
    if state is not None and resumed_window is None:
        state.record_window(state_window_key, current_window)
        resumed_window = current_window

    if not dry_run and tasks:
        worker_count = max(1, int(workers or 1))
        print(
            f"[INFO] bronze {dataset} downloading planned_symbols={len(tasks):,}, workers={worker_count}",
            flush=True,
        )
        if worker_count == 1:
            for stock_code, path, window in tasks:
                _download_and_merge_krx_symbol(dataset, stock_code, path, window)
                if state is not None:
                    state.complete_symbol(dataset, stock_code)
                downloaded += 1
                processed += 1
                maybe_print_symbol_progress(dataset, processed, total, planned, downloaded, skipped, progress_interval, started_at)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_task = {
                    executor.submit(_download_and_merge_krx_symbol, dataset, stock_code, path, window): stock_code
                    for stock_code, path, window in tasks
                }
                for future in as_completed(future_to_task):
                    stock_code = future_to_task[future]
                    future.result()
                    if state is not None:
                        state.complete_symbol(dataset, stock_code)
                    downloaded += 1
                    processed += 1
                    maybe_print_symbol_progress(dataset, processed, total, planned, downloaded, skipped, progress_interval, started_at)

    overall = resumed_window or current_window
    print(
        f"[DONE] bronze {dataset} planned_symbols={planned:,}, "
        f"downloaded_symbols={downloaded:,}, skipped_symbols={skipped:,}, "
        f"start={overall.start_date or '-'}, end={overall.end_date}"
    )
    return overall


def _download_and_merge_krx_symbol(
    dataset: str,
    stock_code: str,
    path: Path,
    window: RefreshWindow,
) -> None:
    new_frame = fetch_krx_dataset_frame(dataset, stock_code, window.start_date, window.end_date)
    if new_frame is None or new_frame.empty:
        raise KRXEmptyResponseError(
            f"KRX returned no {dataset} rows for trading-day window; "
            f"symbol={stock_code}, start={window.start_date}, end={window.end_date}; "
            "resume state was not advanced"
        )
    merge_krx_symbol_csv(path, new_frame)

def maybe_print_symbol_progress(
    dataset: str,
    processed: int,
    total: int,
    planned: int,
    downloaded: int,
    skipped: int,
    progress_interval: int,
    started_at: float,
) -> None:
    if total <= 0:
        return
    should_print = processed in {1, total} or (progress_interval > 0 and processed % progress_interval == 0)
    if not should_print:
        return
    print(
        f"[PROGRESS] bronze {dataset} processed={processed:,}/{total:,}, "
        f"planned={planned:,}, downloaded={downloaded:,}, skipped={skipped:,}, "
        f"elapsed={format_elapsed(started_at)}",
        flush=True,
    )


def format_elapsed(started_at: float) -> str:
    elapsed = max(0.0, time.monotonic() - started_at)
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    return f"{elapsed / 60:.1f}m"

def fetch_krx_dataset_frame(dataset: str, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    from engine.extractors._internal.krx_market_prices import _with_date_column, fetch_price, fetch_share

    fetcher = fetch_price if dataset == "price" else fetch_share
    return _with_date_column(fetcher(stock_code, start_date, end_date))


def resolve_krx_effective_end_date(end_date: str | date) -> str:
    requested = parse_date_arg(end_date)
    try:
        from pykrx import stock

        effective = parse_date_arg(
            stock.get_nearest_business_day_in_a_week(requested, prev=True)
        )
    except Exception as exc:
        raise RuntimeError(
            f"failed to resolve the latest KRX trading day on or before {requested}; "
            "check KRX authentication and API availability"
        ) from exc

    if effective > requested:
        raise RuntimeError(
            f"KRX trading calendar returned a future date: "
            f"requested={requested}, effective={effective}"
        )
    return effective

def merge_krx_symbol_csv(path: str | Path, new_frame: pd.DataFrame) -> pd.DataFrame:
    path = Path(path)
    frames = []
    if path.exists():
        existing = pd.read_csv(path)
        if not existing.empty:
            frames.append(existing)
    if new_frame is not None and not new_frame.empty:
        frames.append(new_frame)

    if not frames:
        return pd.DataFrame()

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
    merged = (
        merged.sort_values("_parsed_date", kind="stable")
        .drop_duplicates(date_column, keep="last")
        .drop(columns=["_parsed_date"])
        .reset_index(drop=True)
    )
    write_source_dataframe(
        path,
        merged,
        source=f"krx-{path.parent.name}",
        encoding="utf-8-sig",
    )
    return merged


def detect_date_column(frame: pd.DataFrame) -> str:
    if frame.empty:
        if len(frame.columns) == 0:
            raise ValueError("cannot detect date column from an empty dataframe without columns")
        return str(frame.columns[0])

    for column in frame.columns:
        if str(column).strip() == "?좎쭨":
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


def latest_krx_bronze_date(dataset: str) -> date | None:
    root = DATA_LAKE.bronze("krx", dataset)
    if not root.exists():
        return None
    dates = [latest_date_in_csv(path) for path in root.glob("*.csv")]
    dates = [item for item in dates if item is not None]
    return min(dates) if dates else None


def latest_date_in_csv(path: str | Path) -> date | None:
    path = Path(path)
    if not path.exists():
        return None
    tail_date = latest_date_from_csv_tail(path)
    if tail_date is not None:
        return tail_date
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError):
        return None
    if frame.empty:
        return None
    date_column = detect_date_column(frame)
    dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.max().date()


def latest_date_from_csv_tail(path: str | Path, *, tail_bytes: int = 65536) -> date | None:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max(1, int(tail_bytes))))
            payload = handle.read()
    except OSError:
        return None

    for raw_line in reversed(payload.splitlines()):
        if not raw_line.strip():
            continue
        try:
            row = next(csv.reader([raw_line.decode("utf-8-sig")]))
        except (UnicodeDecodeError, csv.Error, StopIteration):
            continue
        if not row:
            continue
        candidate = row[0].strip().lstrip("\ufeff")
        try:
            return datetime.strptime(candidate[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
    return None


def latest_dart_metadata_report_date(path: str | Path | None = None) -> date | None:
    metadata_path = Path(path) if path is not None else DATA_LAKE.silver("dart", market_csv_name("report_metadata"))
    if not metadata_path.exists():
        legacy = DATA_LAKE.silver("dart", "report_metadata.csv")
        metadata_path = legacy if legacy.exists() else metadata_path
    if not metadata_path.exists():
        return None
    frame = pd.read_csv(metadata_path, dtype={"stock_code": str}, low_memory=False)
    if frame.empty or "report_date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["report_date"], errors="coerce").dropna()
    return dates.max().date() if not dates.empty else None


def latest_dividend_date() -> date | None:
    silver_path = dividend_loader.dividend_output_path("kr")
    if silver_path.exists():
        latest = latest_date_in_csv(silver_path)
        if latest is not None:
            return latest
    root = DATA_LAKE.bronze("dart", "dividend")
    if not root.exists():
        return None
    dates = []
    for path in root.rglob("finance_statement_dividend_*.json"):
        match = re.search(r"finance_statement_dividend_(\d{4}-\d{2}-\d{2})", path.stem)
        if not match:
            continue
        try:
            dates.append(datetime.strptime(match.group(1), "%Y-%m-%d").date())
        except ValueError:
            continue
    return max(dates) if dates else None


def download_and_merge_report_metadata(
    stock_codes: list[str],
    *,
    start_date: str,
    end_date: str,
    output_csv_path: str | Path | None = None,
) -> pd.DataFrame:
    output_path = Path(output_csv_path) if output_csv_path is not None else DATA_LAKE.silver("dart", market_csv_name("report_metadata"))
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir) / "kr_report_metadata_incremental.csv"
        incremental = collect_dart_report_metadata(
            stock_codes,
            0,
            output_csv_path=temp_path,
            start_date=start_date,
            end_date=end_date,
            fail_fast=True,
        )

    frames = []
    if output_path.exists():
        frames.append(pd.read_csv(output_path, dtype={"stock_code": str, "rcept_no": str}))
    if not incremental.empty:
        frames.append(incremental)

    merged = (
        deduplicate_report_metadata(pd.concat(frames, ignore_index=True))
        if frames
        else pd.DataFrame()
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[SAVED] merged report metadata {output_path} rows={len(merged):,}")
    return merged


def normalize_statements_for_years(start_year: int, end_year: int) -> None:
    normalize_workflow = get_normalize_workflow()
    if start_year > end_year:
        print(f"[SKIP] statement normalization empty year range: {start_year}-{end_year}")
        return

    corps_list = normalize_workflow.kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    dependency_paths = normalize_workflow.normalization_dependency_paths()
    newest_dependency_mtime = max(path.stat().st_mtime for path in dependency_paths)
    tasks, skipped_count, missing_count = normalize_workflow.build_normalization_tasks(
        stock_codes,
        start_year=start_year,
        end_year=end_year + 1,
        dependency_paths=dependency_paths,
        save_debug=normalize_workflow.SAVE_DEBUG,
        newest_dependency_mtime=newest_dependency_mtime,
    )

    processed_count = 0
    failed_count = 0
    print(
        f"[INFO] refresh statement normalization pending={len(tasks)}, "
        f"skipped={skipped_count}, missing={missing_count}, years={start_year}-{end_year}"
    )

    normalize_workflow._init_normalize_worker()
    for task in tasks:
        status, path, message, stock_code = normalize_workflow._normalize_one_statement(task)
        processed_count, missing_count, failed_count = normalize_workflow._update_counts(
            status,
            path,
            message,
            stock_code,
            processed_count,
            missing_count,
            failed_count,
        )

    snapshot_dir = normalize_workflow.normalized_statement_snapshot_dir()
    normalized_dir = normalize_workflow.normalized_statement_output_dir()
    consolidated_count = 0
    for stock_code in stock_codes:
        if normalize_workflow.consolidate_statement_snapshots(
            stock_code,
            snapshot_dir,
            market="kr",
            columns=normalize_workflow.EXPECTED_HEADER,
            output_dir=normalized_dir,
        ):
            consolidated_count += 1
        if normalize_workflow.SAVE_DEBUG:
            normalize_workflow.consolidate_statement_debug_snapshots(
                stock_code,
                snapshot_dir,
                market="kr",
                output_dir=normalized_dir,
            )
    removed_count = normalize_workflow.remove_legacy_statement_snapshots(normalized_dir)
    print(
        f"[DONE] refresh statement normalization processed={processed_count}, "
        f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}, "
        f"consolidated={consolidated_count}, removed_legacy={removed_count}"
    )
    if failed_count:
        raise RuntimeError(
            f"KR statement normalization failed for {failed_count} task(s)"
        )

def build_refresh_window(
    latest: date | None,
    *,
    end_date: str,
    force_full: bool = False,
    default_start: str = DEFAULT_START_DATE,
) -> RefreshWindow:
    end_date = parse_date_arg(end_date)
    if force_full or latest is None:
        start = default_start
    else:
        start = (latest + timedelta(days=1)).strftime("%Y%m%d")
    if start > end_date:
        start = None
    return RefreshWindow(start, end_date, None if force_full else latest)


def earliest_window(*windows: RefreshWindow) -> RefreshWindow:
    active = [window for window in windows if window.start_date is not None]
    if not active:
        end_date = windows[0].end_date if windows else today_kst().strftime("%Y%m%d")
        latest = max((window.latest_date for window in windows if window.latest_date), default=None)
        return RefreshWindow(None, end_date, latest)
    start = min(window.start_date for window in active if window.start_date is not None)
    end = max(window.end_date for window in windows)
    latest = min((window.latest_date for window in windows if window.latest_date), default=None)
    return RefreshWindow(start, end, latest)


def filter_frame_by_window(frame: pd.DataFrame, date_column: str, window: RefreshWindow) -> pd.DataFrame:
    if frame.empty or window.start_date is None or date_column not in frame.columns:
        return frame.iloc[0:0].copy() if window.start_date is None else frame.copy()
    result = frame.copy()
    dates = pd.to_datetime(result[date_column], errors="coerce")
    start = pd.Timestamp(_parse_yyyymmdd(window.start_date))
    end = pd.Timestamp(_parse_yyyymmdd(window.end_date))
    return result.loc[dates.between(start, end)].copy()


def parse_date_arg(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    cleaned = str(value).strip().replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    try:
        datetime.strptime(cleaned, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a valid calendar date") from exc
    return cleaned


def today_kst() -> date:
    return datetime.now(KST).date()


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(parse_date_arg(value), "%Y%m%d").date()


def _to_iso_date(value: str) -> str:
    return _parse_yyyymmdd(value).isoformat()

























