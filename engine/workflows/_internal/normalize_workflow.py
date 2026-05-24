from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from engine.transformers.filings import (
    ContextEngine,
    RuleEngine,
    infer_comment_html_path,
    load_canonical_accounts,
    normalize_financial_statement_rule_based,
)
from engine.extractors.market_universe import kospi_kosdaq_corp_list


ENGINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ENGINE_DIR.parents[2]
DATA_LAKE_DIR = PROJECT_ROOT / "data-lake"

CANONICAL_CSV_PATH = DATA_LAKE_DIR / "meta" / "CanonicalAccount.csv"
CONTEXT_RULE_PATH = DATA_LAKE_DIR / "meta" / "rules" / "context_common.yaml"
MAPPING_RULE_PATH = DATA_LAKE_DIR / "meta" / "rules" / "mapping_common.yaml"
COMMENT_RULE_PATH = DATA_LAKE_DIR / "meta" / "rules" / "comment_common.yaml"
SIGN_POLICY_PATH = DATA_LAKE_DIR / "meta" / "rules" / "sign_policy_common.yaml"
SAVE_DEBUG = True
FORCE_REBUILD = False
MAX_WORKERS = int(os.environ.get("NORMALIZE_MAX_WORKERS") or "0")
if MAX_WORKERS <= 0:
    MAX_WORKERS = max(1, min((os.cpu_count() or 2) - 1, 8))

StatementTask = tuple[Path, str, str, Path, Path | None]
NormalizeResult = tuple[str, str, str, str]

_WORKER_CANONICAL_DF: pd.DataFrame | None = None
_WORKER_CONTEXT_ENGINE: ContextEngine | None = None
_WORKER_MAPPING_ENGINE: RuleEngine | None = None


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
                    DATA_LAKE_DIR
                    / "bronze"
                    / "dart"
                    / "finance-statement"
                    / stock_code
                    / f"finance_statement_({year}.{month_text}).html"
                )
                period = f"{year}.{month}"
                output_csv_path = (
                    DATA_LAKE_DIR
                    / "silver"
                    / "dart"
                    / "normalized"
                    / f"normalized_{stock_code}_{year}.{month_text}.csv"
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


def normalize_all_statements() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    today = date.today()
    end_year = today.year
    start_year = end_year - 10

    dependency_paths = [
        ENGINE_DIR / "canonical_rule_normalizer.py",
        CANONICAL_CSV_PATH,
        CONTEXT_RULE_PATH,
        MAPPING_RULE_PATH,
        COMMENT_RULE_PATH,
        SIGN_POLICY_PATH,
    ]
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


def main() -> None:
    normalize_all_statements()


if __name__ == "__main__":
    main()
