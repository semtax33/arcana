from datetime import date

from canonical_rule_normalizer import normalize_financial_statement_rule_based
from company import kospi_kosdaq_corp_list
from statements import download_recent_statement_comments, download_statements


def main() -> None:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    today = date.today()
    end_year = today.year - 1
    start_year = end_year - 10


    for stock_code in stock_codes:
        for year in (start_year, end_year):
            for month in [3, 6, 9, 12]:
                input_html_path = f"./data-lake/bronze/dart/finance-statement/{stock_code}/finance_statement_({year}.{str(month).zfill(2)}).html"
                company_name = str(stock_code)
                period = f"{year}.{month}"
                output_csv_path = f"./data-lake/silver/dart/normalized/normalized_{stock_code}_{year}.{str(month).zfill(2)}.csv"
                canonical_csv_path = "./data-lake/meta/CanonicalAccount.csv"
                context_rule_path = "./data-lake/meta/rules/context_common.yaml"
                mapping_rule_path = "./data-lake/meta/rules/mapping_common.yaml"
                try:
                    print(f"[START PROCESS] {input_html_path}")
                    normalize_financial_statement_rule_based(
                        input_html_path=input_html_path,
                        company_name=company_name,
                        period=period,
                        output_csv_path=output_csv_path,
                        canonical_csv_path=canonical_csv_path,
                        context_rule_path=context_rule_path,
                        mapping_rule_paths=[
                            mapping_rule_path
                        ]
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


if __name__ == "__main__":
    main()

#download_all_statements()
#download_statements(stock_codes, 451)
#corps_list = kospi_kosdaq_corp_list()
#stock_codes = corps_list["stock_code"].tolist()
#download_recent_statement_comments(stock_codes, 0)