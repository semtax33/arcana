from __future__ import annotations

import argparse
from datetime import date

from company import kospi_kosdaq_corp_list
from price import fetch_all_prices, fetch_all_shares
from statements import (
    collect_dart_report_metadata,
    download_dividend_histories,
    download_statement_comments,
    download_statements,
)


def _stock_codes() -> list[str]:
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    return stock_codes


def download_all_statements() -> None:
    download_statements(_stock_codes(), 0)


def download_all_statement_comments() -> None:
    download_statement_comments(_stock_codes(), 0)


def download_all_report_metadata() -> None:
    collect_dart_report_metadata(_stock_codes(), 0)


def download_all_prices() -> None:
    fetch_all_prices(_stock_codes(), 0, "20100101", date.today().strftime("%Y%m%d"))


def download_all_shares() -> None:
    fetch_all_shares(_stock_codes(), 0, "20100101", date.today().strftime("%Y%m%d"))


def download_all_dividend() -> None:
    download_dividend_histories(_stock_codes(), 0)


DOWNLOAD_ACTIONS = {
    "statements": download_all_statements,
    "comments": download_all_statement_comments,
    "metadata": download_all_report_metadata,
    "prices": download_all_prices,
    "shares": download_all_shares,
    "dividend": download_all_dividend,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download bronze/source market and DART data.")
    parser.add_argument("target", choices=sorted(DOWNLOAD_ACTIONS))
    args = parser.parse_args()
    DOWNLOAD_ACTIONS[args.target]()


if __name__ == "__main__":
    main()
