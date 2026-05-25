from __future__ import annotations

import argparse
from datetime import date

from engine.extractors.market_prices import (
    download_us_price_histories,
    fetch_all_prices,
    fetch_all_shares,
)


def _stock_codes() -> list[str]:
    from engine.extractors.market_universe import kospi_kosdaq_corp_list

    corps_list = kospi_kosdaq_corp_list()
    stock_codes = corps_list["stock_code"].tolist()
    print(f"Total Length : {len(stock_codes)}")
    return stock_codes


def download_all_statements() -> None:
    from engine.extractors.filings import download_statements

    download_statements(_stock_codes(), 0)


def download_all_statement_comments() -> None:
    from engine.extractors.filings import download_statement_comments

    download_statement_comments(_stock_codes(), 0)


def download_all_report_metadata() -> None:
    from engine.extractors.filings import collect_dart_report_metadata

    collect_dart_report_metadata(_stock_codes(), 0)


def download_all_prices() -> None:
    fetch_all_prices(_stock_codes(), 0, "20100101", date.today().strftime("%Y%m%d"))


def download_all_shares() -> None:
    fetch_all_shares(_stock_codes(), 0, "20100101", date.today().strftime("%Y%m%d"))


def download_all_dividend() -> None:
    from engine.extractors.filings import download_dividend_histories

    download_dividend_histories(_stock_codes(), 0)


def download_sec_company_tickers() -> None:
    from engine.extractors.sec_filings import download_sec_company_tickers as _download_sec_company_tickers

    _download_sec_company_tickers()


def download_all_us_prices(args: argparse.Namespace) -> None:
    download_us_price_histories(
        symbols=_parse_symbols(args.symbols),
        offset=args.offset,
        limit=args.limit,
        force=args.force,
        sleep_seconds=args.sleep_seconds,
    )


DOWNLOAD_ACTIONS = {
    "statements": download_all_statements,
    "comments": download_all_statement_comments,
    "metadata": download_all_report_metadata,
    "prices": download_all_prices,
    "shares": download_all_shares,
    "dividend": download_all_dividend,
}

US_DOWNLOAD_ACTIONS = {
    "prices": download_all_us_prices,
    "sec-tickers": download_sec_company_tickers,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download bronze/source market and DART data.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--symbols", help="Comma-separated symbols for US price downloads.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("target")
    args = parser.parse_args()
    actions = US_DOWNLOAD_ACTIONS if args.market == "us" else DOWNLOAD_ACTIONS
    if args.target not in actions:
        choices = ", ".join(sorted(actions))
        raise SystemExit(f"unknown target for market={args.market}: {args.target}; choices: {choices}")
    action = actions[args.target]
    if args.market == "us" and args.target == "prices":
        action(args)
    else:
        action()


def _parse_symbols(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    main()
