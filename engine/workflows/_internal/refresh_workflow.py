from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, market_csv_name, market_symbol_csv_name
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
}
TABLE_DATE_COLUMNS = {
    "price_daily": "trade_date",
    "stock_shares": "trade_date",
    "dart_report_metadata": "report_date",
    "stock_dividend": "trade_date",
    "fact_daily_factors": "trade_date",
}
SECURITY_TABLES = {
    "issuers": "issuers",
    "security_master": "security-master",
    "identifiers": "identifiers",
}


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
        if resume and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if data and data.get("signature") != signature:
                print(f"[RESUME] ignoring state with different signature: {path}", flush=True)
                data = None
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
        return state

    def save(self) -> None:
        if not self.enabled:
            return
        self.data["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

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

    def is_symbol_completed(self, dataset: str, symbol: str) -> bool:
        symbols = self.data.get("completed_symbols", {}).get(dataset, [])
        return str(symbol) in symbols

    def complete_symbol(self, dataset: str, symbol: str) -> None:
        symbols = self.data.setdefault("completed_symbols", {}).setdefault(dataset, [])
        symbol = str(symbol)
        if symbol not in symbols:
            symbols.append(symbol)
        self.save()

    def reset_symbols(self, dataset: str) -> None:
        self.data.setdefault("completed_symbols", {})[dataset] = []
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
    run_refresh(args)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh KR bronze, silver, and ClickHouse layers.")
    parser.add_argument("--market", default="kr", choices=["kr"])
    parser.add_argument(
        "--targets",
        default="all",
        choices=["all", "market-data", "filings", "business-info", "dividends", "factors"],
    )
    parser.add_argument(
        "--end-date",
        type=parse_date_arg,
        default=today_kst().strftime("%Y%m%d"),
        help="Inclusive end date. Accepts YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument("--stock-retries", type=int, default=3)
    parser.add_argument("--stock-retry-backoff", type=float, default=30.0)
    parser.add_argument("--financial-basis", default="annual", choices=["annual", "quarterly", "ttm"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-clickhouse", action="store_true")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
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
    if args.market != "kr":
        raise ValueError("refresh currently supports market=kr only")

    targets = expand_targets(args.targets)
    end_date = parse_date_arg(args.end_date)
    stock_codes = download_workflow._stock_codes()
    state = RefreshState.open(
        resume_state_path(args),
        signature=resume_signature(args, end_date, targets),
        resume=args.resume,
        enabled=not args.dry_run,
    )
    progress = ProgressTracker(refresh_step_names(targets))
    client = None
    if not args.skip_clickhouse and not args.dry_run:
        client = get_clickhouse_client()

    try:
        if "market-data" in targets:
            if state.is_step_completed("market-data"):
                market_window = state.step_window("market-data") or build_refresh_window(
                    latest_krx_bronze_date("price"),
                    end_date=end_date,
                    force_full=args.force_full,
                )
                print("[RESUME] skipping completed step: market-data", flush=True)
            else:
                progress.begin("market-data")
                market_window = run_market_data_refresh(args, stock_codes, end_date, client, state)
                state.complete_step("market-data", market_window)
                progress.done("market-data")
        else:
            market_window = build_refresh_window(
                latest_krx_bronze_date("price"),
                end_date=end_date,
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
        if "factors" in targets:
            if state.is_step_completed("factors"):
                print("[RESUME] skipping completed step: factors", flush=True)
            else:
                progress.begin("factors")
                run_factor_refresh(args, market_window, client, state)
                state.complete_step("factors", market_window)
                progress.done("factors")
    finally:
        if client is not None:
            client.close()

def resume_state_path(args: argparse.Namespace) -> Path:
    if getattr(args, "resume_state_path", None):
        return Path(args.resume_state_path)
    return DATA_LAKE.meta("refresh_state", "kr_refresh_state.json")


def resume_signature(args: argparse.Namespace, end_date: str, targets: set[str]) -> dict[str, Any]:
    return {
        "market": args.market,
        "targets": refresh_step_names(targets),
        "end_date": end_date,
        "force_full": bool(args.force_full),
        "skip_clickhouse": bool(args.skip_clickhouse),
        "clickhouse_mode": args.clickhouse_mode,
        "financial_basis": args.financial_basis,
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

def expand_targets(target: str) -> set[str]:
    if target == "all":
        return {"market-data", "filings", "business-info", "dividends", "factors"}
    return {target}


def refresh_step_names(targets: set[str]) -> list[str]:
    ordered = ["market-data", "filings", "business-info", "dividends", "factors"]
    return [name for name in ordered if name in targets]

def run_market_data_refresh(
    args: argparse.Namespace,
    stock_codes: list[str],
    end_date: str,
    client: Any,
    state: RefreshState,
) -> RefreshWindow:
    price_window = download_incremental_krx_dataset(
        "price",
        stock_codes,
        end_date=end_date,
        force_full=args.force_full,
        dry_run=args.dry_run,
        progress_interval=args.progress_interval,
        workers=args.workers,
        state=state,
    )
    share_window = download_incremental_krx_dataset(
        "shares",
        stock_codes,
        end_date=end_date,
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

    for original_offset, stock_code in enumerate(sorted_codes):
        if state.is_symbol_completed(dataset, stock_code):
            skipped += 1
            completed += 1
            maybe_print_symbol_progress(dataset, completed, total, planned, 0, skipped, progress_interval, started_at)
            continue
        planned += 1
        tasks.append((stock_code, original_offset))

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
        action=lambda: load_dataframe_with_policy(
            args,
            client,
            table_name=dividend_loader.STOCK_DIVIDEND_TABLE,
            full_frame=insert_frame,
            candidate_frame=candidate,
            column_names=dividend_loader.STOCK_DIVIDEND_COLUMNS,
            window=window,
        ),
    )


def run_factor_refresh(args: argparse.Namespace, window: RefreshWindow, client: Any, state: RefreshState) -> None:
    if args.skip_clickhouse:
        return

    if not args.dry_run:
        run_once_with_state(
            state,
            "factors-catalog",
            window,
            dry_run=args.dry_run,
            action=lambda: load_factor_catalog(args, client),
        )

    factor_window = window if window.has_work else RefreshWindow(None, window.end_date, window.latest_date)
    if state.is_step_completed("factors-insert"):
        print("[RESUME] skipping completed substep: factors-insert", flush=True)
        return

    reload_pending = state.is_step_completed("factors-reload-required")
    should_reload = reload_pending or should_truncate_table(
        args,
        client,
        "fact_daily_factors",
        window=factor_window,
    )
    if should_reload and not args.dry_run:
        state.complete_step("factors-reload-required", factor_window)
        truncate_table(client, "fact_daily_factors")

    start_date = None if should_reload else factor_window.start_iso
    if args.dry_run:
        print(
            "[DRY-RUN] factors "
            f"mode={args.clickhouse_mode}, reload={should_reload}, start={start_date}, end={factor_window.end_iso}"
        )
        return

    factor_loader.insert_daily_factors(
        financial_basis=args.financial_basis,
        start_date=start_date,
        end_date=factor_window.end_iso,
        market="kr",
        insert_catalog=False,
        client=client,
        parallel_workers=args.workers,
    )
    state.complete_step("factors-insert", factor_window)

def load_securities(args: argparse.Namespace, client: Any) -> None:
    for table_name, target in SECURITY_TABLES.items():
        should_reload = should_truncate_table(args, client, table_name)
        if args.dry_run:
            print(f"[DRY-RUN] securities table={table_name}, reload={should_reload}")
            continue
        if should_reload:
            truncate_table(client, table_name)
        security_loader.insert_securities(market="kr", target=target, client=client)


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
        should_reload = should_truncate_table(args, client, table_name, window=window)
        print(f"[DRY-RUN] market table={table_name}, reload={should_reload}, window={window}")
        return

    full_frame = create_frame()
    candidate = filter_frame_by_window(full_frame, TABLE_DATE_COLUMNS[table_name], window)
    should_reload = should_truncate_table(
        args,
        client,
        table_name,
        full_frame=full_frame,
        candidate_frame=candidate,
        window=window,
    )
    if should_reload:
        truncate_table(client, table_name)
        insert_frame(full_frame)
    else:
        insert_frame(candidate)


def load_report_metadata(args: argparse.Namespace, client: Any, window: RefreshWindow) -> None:
    full_frame = filing_loader.read_report_metadata(market="kr")
    candidate = filter_frame_by_window(full_frame, "report_date", window)
    load_dataframe_with_policy(
        args,
        client,
        table_name="dart_report_metadata",
        full_frame=full_frame,
        candidate_frame=candidate,
        column_names=list(full_frame.columns),
        window=window,
    )


def load_factor_catalog(args: argparse.Namespace, client: Any) -> None:
    should_reload = should_truncate_table(args, client, "factor_catalog")
    if args.dry_run:
        print(f"[DRY-RUN] factor_catalog reload={should_reload}")
        return
    if should_reload:
        truncate_table(client, "factor_catalog")
    factor_loader.insert_factor_catalog(client, factor_ids=factor_loader.preferred_factor_columns())


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

    for stock_code in sorted(stock_codes):
        path = output_dir / market_symbol_csv_name(stock_code)
        latest = None if force_full else latest_date_in_csv(path)
        if latest is not None:
            latest_dates.append(latest)
        else:
            saw_missing = True

        if state is not None and state.is_symbol_completed(dataset, stock_code) and latest is not None and latest >= end:
            skipped += 1
            processed += 1
            maybe_print_symbol_progress(dataset, processed, total, planned, downloaded, skipped, progress_interval, started_at)
            continue

        window = build_refresh_window(latest, end_date=end_date, force_full=force_full)
        if not window.has_work:
            skipped += 1
            processed += 1
            maybe_print_symbol_progress(dataset, processed, total, planned, downloaded, skipped, progress_interval, started_at)
            continue

        planned += 1
        tasks.append((stock_code, path, window))
        if dry_run:
            processed += 1
            maybe_print_symbol_progress(dataset, processed, total, planned, downloaded, skipped, progress_interval, started_at)

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

    overall = build_refresh_window(
        None if saw_missing else (min(latest_dates) if latest_dates else None),
        end_date=end_date,
        force_full=force_full,
    )
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
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False, encoding="utf-8-sig")
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
        text = path.stem.rsplit("_", 1)[-1]
        try:
            dates.append(datetime.strptime(text, "%Y-%m-%d").date())
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

























