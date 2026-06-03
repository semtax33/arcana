from __future__ import annotations

import argparse
import os
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from engine.core.paths import (
    DATA_LAKE,
    PROJECT_ROOT,
    first_existing_path,
    parse_statement_snapshot_filename,
    statement_snapshot_name,
    statement_symbol_name,
)
from engine.transformers.filings import (
    ContextEngine,
    EXPECTED_HEADER,
    RuleEngine,
    infer_comment_html_path,
    load_canonical_accounts,
    normalize_financial_statement_rule_based,
)
from engine.transformers._internal.statement_files import (
    consolidate_statement_debug_snapshots,
    consolidate_statement_snapshots,
)
from engine.extractors.market_universe import kospi_kosdaq_corp_list


CANONICAL_CSV_PATH = DATA_LAKE.canonical_accounts()
CONTEXT_RULE_PATH = (
    DATA_LAKE.rules("context_kr.yaml")
    if DATA_LAKE.rules("context_kr.yaml").exists()
    else DATA_LAKE.rules("context_common.yaml")
)
MAPPING_RULE_PATH = first_existing_path(
    DATA_LAKE.rules("kr_mapping.yaml"),
    DATA_LAKE.rules("mapping_kr.yaml"),
    DATA_LAKE.rules("mapping_common.yaml"),
)
COMMENT_RULE_PATH = (
    DATA_LAKE.rules("comment_kr.yaml")
    if DATA_LAKE.rules("comment_kr.yaml").exists()
    else DATA_LAKE.rules("comment_common.yaml")
)
SIGN_POLICY_PATH = DATA_LAKE.rules("sign_policy_common.yaml")
US_MAPPING_RULE_PATH = first_existing_path(
    DATA_LAKE.rules("us_mapping.yaml"),
    DATA_LAKE.rules("mapping_us.yaml"),
)
SAVE_DEBUG = True
FORCE_REBUILD = False
MAX_WORKERS = int(os.environ.get("NORMALIZE_MAX_WORKERS") or "0")
if MAX_WORKERS <= 0:
    MAX_WORKERS = max(1, min((os.cpu_count() or 2) - 1, 8))

StatementTask = tuple[Path, str, str, Path, Path | None]
NormalizeResult = tuple[str, str, str, str]

_WORKER_CANONICAL_DF: Any | None = None
_WORKER_CONTEXT_ENGINE: ContextEngine | None = None
_WORKER_MAPPING_ENGINE: RuleEngine | None = None


def normalized_statement_output_dir() -> Path:
    return DATA_LAKE.silver("dart", "normalized")


def normalized_statement_snapshot_dir() -> Path:
    return DATA_LAKE.silver("dart", "normalized-snapshots")


def business_info_output_dir() -> Path:
    return DATA_LAKE.silver("dart", "business-info")


def normalization_dependency_paths() -> list[Path]:
    paths = [
        Path(__file__).resolve(),
        PROJECT_ROOT / "engine" / "transformers" / "filings.py",
        PROJECT_ROOT / "engine" / "transformers" / "_internal" / "dart_filings.py",
        PROJECT_ROOT / "engine" / "transformers" / "_internal" / "statement_files.py",
        PROJECT_ROOT / "engine" / "core" / "paths.py",
        CANONICAL_CSV_PATH,
        CONTEXT_RULE_PATH,
        MAPPING_RULE_PATH,
        COMMENT_RULE_PATH,
        SIGN_POLICY_PATH,
    ]
    missing_paths = [path for path in paths if not path.exists()]
    if missing_paths:
        missing_text = ", ".join(str(path) for path in missing_paths)
        raise FileNotFoundError(f"Normalization dependency not found: {missing_text}")
    return paths


def output_is_fresh(
    input_path: Path,
    output_path: Path,
    dependency_paths: list[Path],
    save_debug: bool,
    newest_dependency_mtime: float | None = None,
    extra_input_paths: list[Path] | None = None,
) -> bool:
    if FORCE_REBUILD or not output_path.exists():
        return False

    debug_path = output_path.with_suffix(".debug.csv")
    if save_debug and not debug_path.exists():
        return False

    if newest_dependency_mtime is None:
        newest_dependency_mtime = max(p.stat().st_mtime for p in dependency_paths)

    input_paths = [input_path, *(extra_input_paths or [])]
    newest_input_mtime = max(
        max(p.stat().st_mtime for p in input_paths if p.exists()),
        newest_dependency_mtime,
    )
    if output_path.stat().st_mtime < newest_input_mtime:
        return False

    return not save_debug or debug_path.stat().st_mtime >= newest_input_mtime


def build_normalization_tasks(
    stock_codes: list[str],
    *,
    start_year: int,
    end_year: int,
    dependency_paths: list[Path],
    save_debug: bool = SAVE_DEBUG,
    newest_dependency_mtime: float | None = None,
) -> tuple[list[StatementTask], int, int]:
    if newest_dependency_mtime is None:
        newest_dependency_mtime = max(p.stat().st_mtime for p in dependency_paths)

    tasks: list[StatementTask] = []
    skipped_count = 0
    missing_count = 0

    for stock_code in stock_codes:
        for year in range(start_year, end_year):
            for month in [3, 6, 9, 12]:
                month_text = str(month).zfill(2)
                input_html_path = (
                    DATA_LAKE.bronze(
                        "dart",
                        "finance-statement",
                        stock_code,
                        f"finance_statement_({year}.{month_text}).html",
                    )
                )
                period = f"{year}.{month}"
                output_csv_path = (
                    normalized_statement_snapshot_dir()
                    / statement_snapshot_name(stock_code, year, month)
                )
                comment_html_path = infer_comment_html_path(
                    input_html_path=input_html_path,
                    company_name=stock_code,
                    period=period,
                )
                existing_comment_paths = [comment_html_path] if comment_html_path.exists() else []

                if not input_html_path.exists():
                    missing_count += 1
                    continue

                if output_is_fresh(
                    input_html_path,
                    output_csv_path,
                    dependency_paths,
                    save_debug,
                    newest_dependency_mtime=newest_dependency_mtime,
                    extra_input_paths=existing_comment_paths,
                ):
                    skipped_count += 1
                    continue

                tasks.append(
                    (
                        input_html_path,
                        stock_code,
                        period,
                        output_csv_path,
                        comment_html_path if comment_html_path.exists() else None,
                    )
                )

    return tasks, skipped_count, missing_count


def iter_business_info_paths(
    symbols: list[str] | None = None,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    bronze_root: str | Path | None = None,
) -> list[Path]:
    root = Path(bronze_root) if bronze_root is not None else DATA_LAKE.bronze("dart", "business-info")
    if not root.exists():
        return []

    if symbols:
        stock_dirs = [root / _normalize_kr_symbol(symbol) for symbol in symbols]
    else:
        stock_dirs = sorted(path for path in root.iterdir() if path.is_dir())

    paths: list[Path] = []
    for stock_dir in stock_dirs:
        if not stock_dir.is_dir():
            continue
        for path in sorted(stock_dir.glob("business_info_(*).html")):
            period = _business_info_period(path)
            if period is None:
                continue
            year, _ = period
            if start_year is not None and year < start_year:
                continue
            if end_year is not None and year > end_year:
                continue
            paths.append(path)
    return paths


def normalize_business_infos(
    symbols: list[str] | None = None,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    workers: int | None = 1,
) -> list[tuple[Path, Path, Path, Path]] | tuple[Path, Path, Path, Path]:
    from engine.transformers._internal.kr_business_extractor import (
        document_to_cell_records,
        document_to_row_records,
        document_to_section_records,
        document_to_table_records,
        parse_business_info_files,
        write_business_info_csvs,
    )

    paths = iter_business_info_paths(
        symbols,
        start_year=start_year,
        end_year=end_year,
    )
    worker_count = workers if workers is not None else 1
    print(f"[INFO] business-info files={len(paths)}, workers={worker_count}")
    documents = parse_business_info_files(paths, max_workers=worker_count)
    section_rows = sum(len(document_to_section_records(document)) for document in documents)
    table_rows = sum(len(document_to_table_records(document)) for document in documents)
    cell_rows = sum(len(document_to_cell_records(document)) for document in documents)
    row_rows = sum(len(document_to_row_records(document)) for document in documents)
    written_paths = write_business_info_csvs(documents)
    print(
        f"[DONE] business-info documents={len(documents)}, "
        f"section_rows={section_rows}, table_rows={table_rows}, "
        f"cell_rows={cell_rows}, row_rows={row_rows}"
    )
    if isinstance(written_paths, list):
        print(f"[DONE] business-info stock_csv_groups={len(written_paths)}")
        for group in written_paths[:5]:
            print(f"[DONE] business-info stock_output={group[0].parent}")
        if len(written_paths) > 5:
            print(f"[DONE] business-info stock_output_more={len(written_paths) - 5}")
    else:
        section_path, table_path, cell_path, row_path = written_paths
        print(f"[DONE] business-info sections={section_path}")
        print(f"[DONE] business-info tables={table_path}")
        print(f"[DONE] business-info cells={cell_path}")
        print(f"[DONE] business-info rows={row_path}")
    return written_paths


def normalize_all_statements() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    today = date.today()
    end_year = today.year
    start_year = end_year - 10

    dependency_paths = normalization_dependency_paths()
    newest_dependency_mtime = max(p.stat().st_mtime for p in dependency_paths)
    tasks, skipped_count, missing_count = build_normalization_tasks(
        stock_codes,
        start_year=start_year,
        end_year=end_year,
        dependency_paths=dependency_paths,
        save_debug=SAVE_DEBUG,
        newest_dependency_mtime=newest_dependency_mtime,
    )

    processed_count = 0
    failed_count = 0

    print(
        f"[INFO] pending={len(tasks)}, workers={MAX_WORKERS}, "
        f"skipped={skipped_count}, missing={missing_count}"
    )

    if MAX_WORKERS == 1:
        _init_normalize_worker()
        for task in tasks:
            print(f"[START PROCESS] {task[0]}")
            status, path, message, stock_code = _normalize_one_statement(task)
            processed_count, missing_count, failed_count = _update_counts(
                status,
                path,
                message,
                stock_code,
                processed_count,
                missing_count,
                failed_count,
            )

            _print_progress(processed_count, skipped_count, missing_count, failed_count)
    elif tasks:
        with ProcessPoolExecutor(
            max_workers=MAX_WORKERS,
            initializer=_init_normalize_worker,
        ) as executor:
            for status, path, message, stock_code in executor.map(
                _normalize_one_statement,
                tasks,
                chunksize=20,
            ):
                processed_count, missing_count, failed_count = _update_counts(
                    status,
                    path,
                    message,
                    stock_code,
                    processed_count,
                    missing_count,
                    failed_count,
                )

                _print_progress(processed_count, skipped_count, missing_count, failed_count)

    print(
        f"[DONE] processed={processed_count}, "
        f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}"
    )

    snapshot_dir = normalized_statement_snapshot_dir()
    normalized_dir = normalized_statement_output_dir()
    consolidated_count = 0
    for stock_code in stock_codes:
        if consolidate_statement_snapshots(
            stock_code,
            snapshot_dir,
            market="kr",
            columns=EXPECTED_HEADER,
            output_dir=normalized_dir,
        ):
            consolidated_count += 1
        if SAVE_DEBUG:
            consolidate_statement_debug_snapshots(
                stock_code,
                snapshot_dir,
                market="kr",
                output_dir=normalized_dir,
            )
    removed_count = remove_legacy_statement_snapshots(normalized_dir)
    print(f"[DONE] consolidated statement files={consolidated_count}")
    if removed_count:
        print(f"[DONE] removed legacy statement snapshot files={removed_count}")


def remove_legacy_statement_snapshots(normalized_dir: str | Path) -> int:
    normalized_dir = Path(normalized_dir)
    if not normalized_dir.exists():
        return 0

    removed_count = 0
    for path in normalized_dir.glob("*normalized_*_*.csv"):
        if ".debug" in path.name or ".validation" in path.name:
            continue

        meta = parse_statement_snapshot_filename(path)
        if meta is None:
            continue

        consolidated_path = normalized_dir / statement_symbol_name(
            meta["stock_code"],
            market=str(meta["market"]),
        )
        if not consolidated_path.exists():
            continue

        for candidate in [path, path.with_suffix(".debug.csv")]:
            if candidate.exists():
                candidate.unlink()
                removed_count += 1

    return removed_count


def _init_normalize_worker() -> None:
    global _WORKER_CANONICAL_DF, _WORKER_CONTEXT_ENGINE, _WORKER_MAPPING_ENGINE

    _WORKER_CANONICAL_DF = load_canonical_accounts(CANONICAL_CSV_PATH)
    _WORKER_CONTEXT_ENGINE = ContextEngine.from_yaml(CONTEXT_RULE_PATH)
    _WORKER_MAPPING_ENGINE = RuleEngine.from_files(
        canonical_csv_path=CANONICAL_CSV_PATH,
        rule_paths=[MAPPING_RULE_PATH],
        sign_policy_path=SIGN_POLICY_PATH,
    )


def _normalize_one_statement(task: StatementTask) -> NormalizeResult:
    global _WORKER_CANONICAL_DF, _WORKER_CONTEXT_ENGINE, _WORKER_MAPPING_ENGINE

    if (
        _WORKER_CANONICAL_DF is None
        or _WORKER_CONTEXT_ENGINE is None
        or _WORKER_MAPPING_ENGINE is None
    ):
        _init_normalize_worker()

    input_html_path, stock_code, period, output_csv_path, comment_html_path = task

    try:
        normalize_financial_statement_rule_based(
            input_html_path=input_html_path,
            company_name=stock_code,
            period=period,
            output_csv_path=output_csv_path,
            canonical_csv_path=CANONICAL_CSV_PATH,
            context_rule_path=CONTEXT_RULE_PATH,
            mapping_rule_paths=[MAPPING_RULE_PATH],
            sign_policy_path=SIGN_POLICY_PATH,
            save_debug=SAVE_DEBUG,
            context_engine=_WORKER_CONTEXT_ENGINE,
            mapping_engine=_WORKER_MAPPING_ENGINE,
            canonical_df=_WORKER_CANONICAL_DF,
            verbose=False,
            comment_rule_paths=[COMMENT_RULE_PATH],
            comment_html_path=comment_html_path,
        )
    except FileNotFoundError as e:
        return ("missing", str(input_html_path), str(e), stock_code)
    except KeyError as e:
        return ("warning", str(input_html_path), str(e), stock_code)
    except Exception as e:
        return ("failed", str(input_html_path), repr(e), stock_code)

    return ("processed", str(input_html_path), "", stock_code)


def _update_counts(
    status: str,
    path: str,
    message: str,
    stock_code: str,
    processed_count: int,
    missing_count: int,
    failed_count: int,
) -> tuple[int, int, int]:
    if status == "processed":
        processed_count += 1
    elif status == "missing":
        missing_count += 1
        print("File not found : ", message)
    elif status == "warning":
        failed_count += 1
        print("[WARNING] Unknown Key error exception is occured: ", message, stock_code)
    else:
        failed_count += 1
        print(f"[ERROR] {path}: {message}")

    return processed_count, missing_count, failed_count


def _print_progress(
    processed_count: int,
    skipped_count: int,
    missing_count: int,
    failed_count: int,
) -> None:
    if processed_count and processed_count % 100 == 0:
        print(
            f"[PROGRESS] processed={processed_count}, "
            f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}"
        )


def _parse_symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_kr_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


_BUSINESS_INFO_FILE_RE = re.compile(
    r"business_info_\((?P<year>\d{4})[._](?P<month>\d{1,2})\)\.html$",
    re.IGNORECASE,
)


def _business_info_period(path: str | Path) -> tuple[int, int] | None:
    match = _BUSINESS_INFO_FILE_RE.match(Path(path).name)
    if not match:
        return None
    return int(match.group("year")), int(match.group("month"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize financial statements and business-info HTML.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument(
        "--target",
        default="statements",
        choices=["statements", "business-info", "all"],
        help="Normalize financial statements, DART business-info HTML, or both. business-info is KR-only.",
    )
    parser.add_argument("--symbols", help="Comma-separated symbols. US examples: AAPL,MSFT")
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--no-notes", action="store_true")
    parser.add_argument("--no-edgartools", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="US companyfacts worker processes. Use 1 for single process, 0 for CPU count.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=100,
        help="US normalize progress log interval by processed symbols/files.",
    )
    args = parser.parse_args()
    symbols = _parse_symbols(args.symbols)

    if args.market == "kr":
        if args.target in {"statements", "all"}:
            normalize_all_statements()
        if args.target in {"business-info", "all"}:
            normalize_business_infos(
                symbols=symbols,
                start_year=args.start_year,
                end_year=args.end_year,
                workers=args.workers,
            )
        return

    if args.target != "statements":
        raise ValueError(f"--target {args.target!r} is only supported for --market kr")

    from engine.transformers.sec_filings import normalize_us_sec_filings

    today = date.today()
    end_year = args.end_year or today.year
    start_year = args.start_year or end_year - 10
    written = normalize_us_sec_filings(
        symbols=symbols,
        start_year=start_year,
        end_year=end_year,
        mapping_rule_path=US_MAPPING_RULE_PATH,
        save_debug=not args.no_debug,
        use_notes=not args.no_notes,
        use_edgartools=not args.no_edgartools,
        workers=args.workers,
        progress_interval=args.progress_interval,
    )
    print(f"[DONE] market=us written={len(written)}")


if __name__ == "__main__":
    main()
