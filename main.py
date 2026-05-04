from datetime import date

from canonical_rule_normalizer import normalize_financial_statement_rule_based, extract_rows_from_dart_comment_html
from company import kospi_kosdaq_corp_list
from statements import download_recent_statement_comments, download_statement_comments, download_statements


def main() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(corps_list["stock_code"].tolist())
    today = date.today()
    end_year = today.year
    start_year = end_year - 10


    for stock_code in stock_codes:
        for year in range(start_year, end_year):
            for month in [3, 6, 9, 12]:
                input_html_path = f"./data-lake/bronze/dart/finance-statement/{stock_code}/finance_statement_({year}.{str(month).zfill(2)}).html"
                company_name = str(stock_code)
                period = f"{year}.{month}"
                output_csv_path = f"./data-lake/silver/dart/normalized/normalized_{stock_code}_{year}.{str(month).zfill(2)}.csv"
                canonical_csv_path = "./data-lake/meta/CanonicalAccount.csv"
                context_rule_path = "./data-lake/meta/rules/context_common.yaml"
                mapping_rule_path = "./data-lake/meta/rules/mapping_common.yaml"
                sign_policy_path = "./data-lake/meta/rules/sign_policy_common.yaml"
                try:
                    print(f"[START PROCESS] {input_html_path}")
                    normalize_financial_statement_rule_based(
                        input_html_path=input_html_path,
                        company_name=stock_code,
                        period=period,
                        output_csv_path=output_csv_path,
                        canonical_csv_path=canonical_csv_path,
                        context_rule_path=context_rule_path,
                        mapping_rule_paths=[mapping_rule_path],
                        sign_policy_path=sign_policy_path,
                        save_debug=True,
                    )
                except FileNotFoundError as e:
                    print("File not found : ", e)
                    continue
                except KeyError as e:
                    print("[WARNING] Unknown Key error exception is occured: ", e)
                    continue



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