from __future__ import annotations

import argparse
from datetime import date, datetime

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


DEFAULT_MARKET_START_DATE = "20100101"


def download_all_statements(args: argparse.Namespace) -> None:
    from engine.extractors.filings import download_statements

    download_statements(
        _stock_codes(),
        args.offset,
        start_date=args.start_date,
        end_date=args.end_date,
    )


def download_all_statement_comments(args: argparse.Namespace) -> None:
    from engine.extractors.filings import download_statement_comments

    download_statement_comments(
        _stock_codes(),
        args.offset,
        start_date=args.start_date,
        end_date=args.end_date,
    )


def download_all_business_infos(args: argparse.Namespace) -> None:
    from engine.extractors.filings import download_business_infos

    download_business_infos(
        _stock_codes(),
        args.offset,
        start_date=args.start_date,
        end_date=args.end_date,
        max_workers=args.workers,
        force=args.force,
        sleep_seconds=args.sleep_seconds or 5.0,
        stock_retries=args.stock_retries,
        stock_retry_backoff=args.stock_retry_backoff,
    )


def download_all_report_metadata(args: argparse.Namespace) -> None:
    from engine.extractors.filings import collect_dart_report_metadata

    collect_dart_report_metadata(
        _stock_codes(),
        args.offset,
        start_date=args.start_date,
        end_date=args.end_date,
    )


def download_all_prices(args: argparse.Namespace) -> None:
    fetch_all_prices(
        _stock_codes(),
        args.offset,
        args.start_date or DEFAULT_MARKET_START_DATE,
        args.end_date or date.today().strftime("%Y%m%d"),
    )


def download_all_shares(args: argparse.Namespace) -> None:
    fetch_all_shares(
        _stock_codes(),
        args.offset,
        args.start_date or DEFAULT_MARKET_START_DATE,
        args.end_date or date.today().strftime("%Y%m%d"),
    )


def download_all_dividend(args: argparse.Namespace) -> None:
    from engine.extractors.filings import download_dividend_histories

    download_dividend_histories(
        _stock_codes(),
        args.offset,
        start_date=args.start_date,
        end_date=args.end_date,
    )


def download_sec_company_tickers(args: argparse.Namespace) -> None:
    from engine.extractors.sec_filings import download_sec_company_tickers as _download_sec_company_tickers

    _download_sec_company_tickers()


def download_all_us_prices(args: argparse.Namespace) -> None:
    download_us_price_histories(
        symbols=_parse_symbols(args.symbols),
        offset=args.offset,
        limit=args.limit,
        force=args.force,
        sleep_seconds=args.sleep_seconds,
        start_date=args.start_date,
        end_date=args.end_date,
    )


DOWNLOAD_ACTIONS = {
    "statements": download_all_statements,
    "comments": download_all_statement_comments,
    "business-info": download_all_business_infos,
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
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker count for supported downloads.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--stock-retries", type=int, default=3, help="Per-symbol retry count for supported downloads.")
    parser.add_argument("--stock-retry-backoff", type=float, default=30.0, help="Base seconds for per-symbol retry backoff.")
    parser.add_argument(
        "--start-date",
        type=_parse_date_arg,
        help="Inclusive start date for downloads. Accepts YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date_arg,
        help="Inclusive end date for downloads. Accepts YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument("target")
    args = parser.parse_args()
    _validate_date_range(args.start_date, args.end_date)
    actions = US_DOWNLOAD_ACTIONS if args.market == "us" else DOWNLOAD_ACTIONS
    if args.target not in actions:
        choices = ", ".join(sorted(actions))
        raise SystemExit(f"unknown target for market={args.market}: {args.target}; choices: {choices}")
    action = actions[args.target]
    action(args)


def _parse_symbols(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_date_arg(value: str) -> str:
    cleaned = value.strip().replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    try:
        datetime.strptime(cleaned, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a valid calendar date") from exc
    return cleaned


def _validate_date_range(start_date: str | None, end_date: str | None) -> None:
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must be earlier than or equal to --end-date")


if __name__ == "__main__":
    main()
