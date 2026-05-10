import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from canonical_rule_normalizer import (
    ContextEngine,
    RuleEngine,
    extract_rows_from_dart_comment_html,
    infer_comment_html_path,
    load_canonical_accounts,
    normalize_financial_statement_rule_based,
)
from company import kospi_kosdaq_corp_list
from dividend import fetch_all_stock_dividends_async
from dividend_normalizer import normalize_dividends
from market_snapshot_normalizer import normalize_price, normalize_shares
from price import fetch_all_prices, fetch_all_shares
from statements import download_recent_statement_comments, download_statement_comments, download_statements


CANONICAL_CSV_PATH = Path("./data-lake/meta/CanonicalAccount.csv")
CONTEXT_RULE_PATH = Path("./data-lake/meta/rules/context_common.yaml")
MAPPING_RULE_PATH = Path("./data-lake/meta/rules/mapping_common.yaml")
COMMENT_RULE_PATH = Path("./data-lake/meta/rules/comment_common.yaml")
SIGN_POLICY_PATH = Path("./data-lake/meta/rules/sign_policy_common.yaml")
SAVE_DEBUG = True
FORCE_REBUILD = False
MAX_WORKERS = int(os.environ.get("NORMALIZE_MAX_WORKERS") or "0")
if MAX_WORKERS <= 0:
    MAX_WORKERS = max(1, min((os.cpu_count() or 2) - 1, 8))

_WORKER_CANONICAL_DF = None
_WORKER_CONTEXT_ENGINE = None
_WORKER_MAPPING_ENGINE = None


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


def _init_normalize_worker() -> None:
    global _WORKER_CANONICAL_DF, _WORKER_CONTEXT_ENGINE, _WORKER_MAPPING_ENGINE

    _WORKER_CANONICAL_DF = load_canonical_accounts(CANONICAL_CSV_PATH)
    _WORKER_CONTEXT_ENGINE = ContextEngine.from_yaml(CONTEXT_RULE_PATH)
    _WORKER_MAPPING_ENGINE = RuleEngine.from_files(
        canonical_csv_path=CANONICAL_CSV_PATH,
        rule_paths=[MAPPING_RULE_PATH],
        sign_policy_path=SIGN_POLICY_PATH,
    )


def _normalize_one_statement(task: tuple[Path, str, str, Path, Path | None]) -> tuple[str, str, str, str]:
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


def main() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    today = date.today()
    end_year = today.year
    start_year = end_year - 10

    dependency_paths = [
        Path("./canonical_rule_normalizer.py"),
        CANONICAL_CSV_PATH,
        CONTEXT_RULE_PATH,
        MAPPING_RULE_PATH,
        COMMENT_RULE_PATH,
        SIGN_POLICY_PATH,
    ]
    newest_dependency_mtime = max(p.stat().st_mtime for p in dependency_paths)

    tasks: list[tuple[Path, str, str, Path, Path | None]] = []
    skipped_count = 0
    missing_count = 0

    for stock_code in stock_codes:
        for year in range(start_year, end_year):
            for month in [3, 6, 9, 12]:
                input_html_path = Path(f"./data-lake/bronze/dart/finance-statement/{stock_code}/finance_statement_({year}.{str(month).zfill(2)}).html")
                period = f"{year}.{month}"
                output_csv_path = Path(f"./data-lake/silver/dart/normalized/normalized_{stock_code}_{year}.{str(month).zfill(2)}.csv")
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
                    SAVE_DEBUG,
                    newest_dependency_mtime=newest_dependency_mtime,
                    extra_input_paths=existing_comment_paths,
                ):
                    skipped_count += 1
                    continue

                tasks.append((
                    input_html_path,
                    stock_code,
                    period,
                    output_csv_path,
                    comment_html_path if comment_html_path.exists() else None,
                ))

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

            if processed_count and processed_count % 100 == 0:
                print(
                    f"[PROGRESS] processed={processed_count}, "
                    f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}"
                )
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

                if processed_count and processed_count % 100 == 0:
                    print(
                        f"[PROGRESS] processed={processed_count}, "
                        f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}"
                    )

    print(
        f"[DONE] processed={processed_count}, "
        f"skipped={skipped_count}, missing={missing_count}, failed={failed_count}"
    )



def download_all_statements():
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    download_statements(stock_codes, 0)

def download_all_statement_comments():
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    download_statement_comments(stock_codes, 0)

def download_all_prices():
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    fetch_all_prices(stock_codes, 0, "20100101", date.today().strftime("%Y%m%d"))

def download_all_shares():
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    fetch_all_shares(stock_codes, 0, "20100101", date.today().strftime("%Y%m%d"))

def download_all_dividend():
    stocks_df = kospi_kosdaq_corp_list()
    fetch_all_stock_dividends_async(
        stocks_df=stocks_df,
        download_offset=0,
        start_year=2015,
        end_year=2025,
        out_root="./data-lake/bronze/dart/dividend",
        skip_existing=True,
        sleep_sec=0.25,
        max_workers=4,

        # None이면 020 발생 key는 이번 실행 중 재사용 안 함
        key_cooldown_sec=None,
    )


if __name__ == "__main__":
    #download_all_dividend()
    #download_all_prices()
    #download_all_shares()
    #download_all_statements()
    #download_all_statement_comments()
    #main()
    normalize_price("./data-lake/bronze/krx/price/*")
    normalize_shares("./data-lake/bronze/krx/shares/*")
    #normalize_dividends()

'''
#download_all_statements()
#download_statements(stock_codes, 451)
#corps_list = kospi_kosdaq_corp_list()
#stock_codes = corps_list["stock_code"].tolist()
#download_recent_statement_comments(stock_codes, 0)

input_html_path = f"./data-lake/bronze/dart/finance-comment/{'011780'}/finance_statement_({2024}.{str(12).zfill(2)}).html"

print(extract_rows_from_dart_comment_html(input_html_path, "금호석유화학", "2024.12", "현금흐름", {
    "DEPRECIATION": r"^감가상각비$",
    "AMORTIZATION": r"^무형자산상각비$",
    "BAD_DEBT_EXPENSE": r"^대손상각비$",
    "AR": r"^매출채권$",
    "INTEREST_EXPENSE": r"^이자비용$",
}))
print(
extract_rows_from_dart_comment_html(input_html_path, "금호석유화학", "2024.12", "무형자산", {
    "RND": r"^연구와 개발 비용$",
}))
'''
