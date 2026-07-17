from __future__ import annotations

import argparse
from datetime import date, datetime

from engine.extractors.benchmarks import fetch_benchmark_prices, fetch_yfinance_benchmark_prices
from engine.extractors.erp import (
    BRONZE_DAMODARAN_COUNTRY_ERP_PATH,
    BRONZE_FRED_RATE_DIR,
    BRONZE_US_SP500_BENCHMARK_PATH,
    FRED_OUTPUT_NAMES,
    FRED_SERIES_IDS,
    download_damodaran_country_erp,
    download_fred_series,
    download_us_sp500_benchmark,
)
from engine.extractors.market_prices import (
    download_us_price_histories,
    fetch_all_prices,
    fetch_all_shares,
)
from engine.extractors.sec_filings import download_us_companyfacts
from engine.transformers.benchmarks import (
    DEFAULT_BENCHMARK_INDEX_CODES,
    DEFAULT_YFINANCE_BENCHMARK_TICKERS,
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


def download_kr_benchmarks(args: argparse.Namespace) -> None:
    frame = fetch_benchmark_prices(
        args.start_date or DEFAULT_MARKET_START_DATE,
        args.end_date or date.today().strftime("%Y%m%d"),
        benchmark_ids=_parse_benchmark_ids(args.benchmark_ids),
    )
    _print_benchmark_download_result(frame)


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


def download_us_benchmarks(args: argparse.Namespace) -> None:
    frame = fetch_yfinance_benchmark_prices(
        args.start_date or DEFAULT_MARKET_START_DATE,
        args.end_date or date.today().strftime("%Y%m%d"),
        benchmark_ids=_parse_benchmark_ids(args.benchmark_ids),
    )
    _print_benchmark_download_result(frame)


def download_all_us_statements(args: argparse.Namespace) -> None:
    download_us_companyfacts(
        symbols=_parse_symbols(args.symbols),
        offset=args.offset,
        limit=args.limit,
        force=args.force,
        sleep_seconds=args.sleep_seconds if args.sleep_seconds > 0 else 0.1,
    )


def download_erp_inputs(args: argparse.Namespace) -> None:
    path = download_damodaran_country_erp()
    print(f"downloaded ERP input [damodaran_country_erp]: {path}")


def download_wacc_inputs(args: argparse.Namespace) -> None:
    market = str(args.market or "kr").strip().lower()
    downloads = [
        (
            "damodaran_country_erp",
            download_damodaran_country_erp,
            BRONZE_DAMODARAN_COUNTRY_ERP_PATH,
        ),
    ]
    downloads.extend(
        (
            f"fred_{series_id.lower()}",
            lambda series_id=series_id: download_fred_series(series_id),
            BRONZE_FRED_RATE_DIR / FRED_OUTPUT_NAMES.get(series_id, f"{series_id.lower()}.csv"),
        )
        for series_id in FRED_SERIES_IDS.values()
    )
    if market == "us":
        downloads.append(
            (
                "yfinance_us_sp500_benchmark",
                lambda: download_us_sp500_benchmark(start_date=args.start_date, end_date=args.end_date),
                BRONZE_US_SP500_BENCHMARK_PATH,
            )
        )

    failures = []
    for label, download, cached_path in downloads:
        if not args.force and _is_nonempty_file(cached_path):
            print(f"using cached WACC input [{label}]: {cached_path}")
            continue
        try:
            path = download()
        except Exception as exc:  # pragma: no cover - exact network errors vary by environment.
            failures.append((label, exc))
            print(f"failed WACC input [{label}]: {type(exc).__name__}: {exc}")
            continue
        print(f"downloaded WACC input [{label}]: {path}")

    if failures:
        labels = ", ".join(label for label, _ in failures)
        raise RuntimeError(f"failed to download required WACC inputs: {labels}")


def _is_nonempty_file(path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def download_kr_consensus(args: argparse.Namespace) -> None:
    from engine.extractors.consensus import (
        download_equity_consensus_reports,
        download_hankyung_consensus_reports,
        download_valuefinder_consensus_reports,
    )

    sources = _parse_consensus_sources(args.consensus_sources)
    if "hankyung" in sources:
        counts = download_hankyung_consensus_reports(
            start_date=args.start_date,
            end_date=args.end_date,
            token=args.hankyung_token,
            force=args.force,
            sleep_seconds=args.sleep_seconds if args.sleep_seconds > 0 else None,
        )
        print(
            "[DONE] hankyung consensus download "
            f"years={counts.get('years', 0):,}, pages={counts.get('pages', 0):,}, "
            f"rows={counts.get('rows', 0):,}, written={counts.get('written', 0):,}, "
            f"skipped={counts.get('skipped', 0):,}, invalid={counts.get('invalid', 0):,}",
            flush=True,
        )
    if "valuefinder" in sources:
        counts = download_valuefinder_consensus_reports(
            pages=args.consensus_html_pages,
            cookie=args.valuefinder_cookie,
            force=args.force,
            sleep_seconds=args.sleep_seconds if args.sleep_seconds > 0 else None,
        )
        print(
            "[DONE] valuefinder consensus download "
            f"pages={counts.get('pages', 0):,}, rows={counts.get('rows', 0):,}, "
            f"written={counts.get('written', 0):,}, skipped={counts.get('skipped', 0):,}, "
            f"invalid={counts.get('invalid', 0):,}",
            flush=True,
        )
    if "equity" in sources:
        counts = download_equity_consensus_reports(
            pages=args.consensus_html_pages,
            cookie=args.equity_cookie,
            force=args.force,
            sleep_seconds=args.sleep_seconds if args.sleep_seconds > 0 else None,
        )
        print(
            "[DONE] equity consensus download "
            f"pages={counts.get('pages', 0):,}, rows={counts.get('rows', 0):,}, "
            f"written={counts.get('written', 0):,}, skipped={counts.get('skipped', 0):,}, "
            f"invalid={counts.get('invalid', 0):,}",
            flush=True,
        )


def download_us_consensus(args: argparse.Namespace) -> None:
    raise NotImplementedError("US consensus not supported yet")


DOWNLOAD_ACTIONS = {
    "statements": download_all_statements,
    "comments": download_all_statement_comments,
    "business-info": download_all_business_infos,
    "metadata": download_all_report_metadata,
    "prices": download_all_prices,
    "shares": download_all_shares,
    "dividend": download_all_dividend,
    "benchmarks": download_kr_benchmarks,
    "consensus": download_kr_consensus,
    "erp": download_erp_inputs,
    "wacc-inputs": download_wacc_inputs,
}

US_DOWNLOAD_ACTIONS = {
    "statements": download_all_us_statements,
    "prices": download_all_us_prices,
    "benchmarks": download_us_benchmarks,
    "sec-tickers": download_sec_company_tickers,
    "consensus": download_us_consensus,
    "erp": download_erp_inputs,
    "wacc-inputs": download_wacc_inputs,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download bronze/source market and DART data.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--symbols", help="Comma-separated symbols for US price downloads.")
    parser.add_argument(
        "--benchmark-ids",
        help=(
            "Comma-separated benchmark ids. Defaults: "
            f"kr={','.join(sorted(DEFAULT_BENCHMARK_INDEX_CODES))}; "
            f"us={','.join(sorted(DEFAULT_YFINANCE_BENCHMARK_TICKERS))}."
        ),
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker count for supported downloads.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--stock-retries", type=int, default=3, help="Per-symbol retry count for supported downloads.")
    parser.add_argument("--stock-retry-backoff", type=float, default=30.0, help="Base seconds for per-symbol retry backoff.")
    parser.add_argument("--hankyung-token", help="JWT bearer token for Hankyung consensus downloads.")
    parser.add_argument(
        "--consensus-sources",
        default="hankyung,valuefinder,equity",
        help="Comma-separated KR consensus sources: hankyung,valuefinder,equity,html,all.",
    )
    parser.add_argument(
        "--consensus-html-pages",
        type=int,
        default=1,
        help="Number of ValueFinder/EQUITY list pages to download. Defaults to the first page.",
    )
    parser.add_argument(
        "--valuefinder-cookie",
        help="Cookie header for ValueFinder consensus downloads. Defaults to VALUEFINDER_CONSENSUS_COOKIE.",
    )
    parser.add_argument(
        "--equity-cookie",
        help="Cookie header for EQUITY consensus downloads. Defaults to EQUITY_CONSENSUS_COOKIE.",
    )
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
    parser.add_argument("--start-year", type=_parse_year_arg, help="Inclusive start year. Expands to YYYY0101.")
    parser.add_argument("--end-year", type=_parse_year_arg, help="Inclusive end year. Expands to YYYY1231.")
    parser.add_argument("target")
    args = parser.parse_args()
    _apply_year_range(args)
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


def _parse_benchmark_ids(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_consensus_sources(value: str | None) -> set[str]:
    aliases = {
        "all": {"hankyung", "valuefinder", "equity"},
        "html": {"valuefinder", "equity"},
    }
    if value is None or not str(value).strip():
        return set(aliases["all"])
    sources: set[str] = set()
    for item in str(value).split(","):
        source = item.strip().lower()
        if not source:
            continue
        if source in aliases:
            sources.update(aliases[source])
            continue
        if source not in aliases["all"]:
            choices = ", ".join(sorted([*aliases["all"], *aliases]))
            raise SystemExit(f"unknown consensus source: {source}; choices: {choices}")
        sources.add(source)
    return sources


def _print_benchmark_download_result(frame) -> None:
    benchmark_ids = []
    if frame is not None and not frame.empty and "benchmark_id" in frame.columns:
        benchmark_ids = sorted(frame["benchmark_id"].dropna().astype(str).unique())
    print(
        "[DONE] benchmark download "
        f"rows={0 if frame is None else len(frame):,}, "
        f"benchmark_ids={','.join(benchmark_ids) if benchmark_ids else '-'}"
    )


def _parse_date_arg(value: str) -> str:
    cleaned = value.strip().replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    try:
        datetime.strptime(cleaned, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a valid calendar date") from exc
    return cleaned


def _parse_year_arg(value: str) -> int:
    cleaned = str(value).strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        raise argparse.ArgumentTypeError("year must be YYYY")
    year = int(cleaned)
    if year < 1900 or year > 2199:
        raise argparse.ArgumentTypeError("year must be between 1900 and 2199")
    return year


def _apply_year_range(args: argparse.Namespace) -> None:
    if args.start_year is not None:
        if args.start_date is not None:
            raise SystemExit("--start-year cannot be used with --start-date")
        args.start_date = f"{args.start_year}0101"
    if args.end_year is not None:
        if args.end_date is not None:
            raise SystemExit("--end-year cannot be used with --end-date")
        args.end_date = f"{args.end_year}1231"


def _validate_date_range(start_date: str | None, end_date: str | None) -> None:
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must be earlier than or equal to --end-date")


if __name__ == "__main__":
    main()

