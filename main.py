from datetime import date
from pathlib import Path

from canonical_rule_normalizer import (
    ContextEngine,
    RuleEngine,
    extract_rows_from_dart_comment_html,
    load_canonical_accounts,
    normalize_financial_statement_rule_based,
)
from company import kospi_kosdaq_corp_list
from statements import download_recent_statement_comments, download_statement_comments, download_statements


CANONICAL_CSV_PATH = Path("./data-lake/meta/CanonicalAccount.csv")
CONTEXT_RULE_PATH = Path("./data-lake/meta/rules/context_common.yaml")
MAPPING_RULE_PATH = Path("./data-lake/meta/rules/mapping_common.yaml")
SIGN_POLICY_PATH = Path("./data-lake/meta/rules/sign_policy_common.yaml")
SAVE_DEBUG = True
FORCE_REBUILD = False


def output_is_fresh(
    input_path: Path,
    output_path: Path,
    dependency_paths: list[Path],
    save_debug: bool,
) -> bool:
    if FORCE_REBUILD or not output_path.exists():
        return False

    debug_path = output_path.with_suffix(".debug.csv")
    if save_debug and not debug_path.exists():
        return False

    newest_input_mtime = max([input_path.stat().st_mtime, *(p.stat().st_mtime for p in dependency_paths)])
    if output_path.stat().st_mtime < newest_input_mtime:
        return False

    return not save_debug or debug_path.stat().st_mtime >= newest_input_mtime


def main() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    today = date.today()
    end_year = today.year
    start_year = end_year - 10

    canonical_df = load_canonical_accounts(CANONICAL_CSV_PATH)
    context_engine = ContextEngine.from_yaml(CONTEXT_RULE_PATH)
    mapping_engine = RuleEngine.from_files(
        canonical_csv_path=CANONICAL_CSV_PATH,
        rule_paths=[MAPPING_RULE_PATH],
        sign_policy_path=SIGN_POLICY_PATH,
    )
    dependency_paths = [
        Path("./canonical_rule_normalizer.py"),
        CANONICAL_CSV_PATH,
        CONTEXT_RULE_PATH,
        MAPPING_RULE_PATH,
        SIGN_POLICY_PATH,
    ]

    processed_count = 0
    skipped_count = 0
    missing_count = 0

    for stock_code in stock_codes:
        for year in range(start_year, end_year):
            for month in [3, 6, 9, 12]:
                input_html_path = Path(f"./data-lake/bronze/dart/finance-statement/{stock_code}/finance_statement_({year}.{str(month).zfill(2)}).html")
                company_name = str(stock_code)
                period = f"{year}.{month}"
                output_csv_path = Path(f"./data-lake/silver/dart/normalized/normalized_{stock_code}_{year}.{str(month).zfill(2)}.csv")

                if not input_html_path.exists():
                    missing_count += 1
                    continue

                if output_is_fresh(input_html_path, output_csv_path, dependency_paths, SAVE_DEBUG):
                    skipped_count += 1
                    continue

                try:
                    print(f"[START PROCESS] {input_html_path}")
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
                        context_engine=context_engine,
                        mapping_engine=mapping_engine,
                        canonical_df=canonical_df,
                        verbose=False,
                    )
                    processed_count += 1
                    if processed_count % 100 == 0:
                        print(
                            f"[PROGRESS] processed={processed_count}, "
                            f"skipped={skipped_count}, missing={missing_count}"
                        )
                except FileNotFoundError as e:
                    print("File not found : ", e)
                    continue
                except KeyError as e:
                    print("[WARNING] Unknown Key error exception is occured: ", e)
                    continue

    print(
        f"[DONE] processed={processed_count}, "
        f"skipped={skipped_count}, missing={missing_count}"
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

if __name__ == "__main__":
    #download_all_statements()
    #download_all_statement_comments()
    main()

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
