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
        request_timeout=args.yfinance_timeout,
        retries=args.yfinance_retries,
        retry_backoff_seconds=args.yfinance_retry_backoff,
        repair=args.yfinance_repair,
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
    from engine.extractors.consensus import download_us_consensus as _download_us_consensus

    sources = _parse_us_consensus_sources(getattr(args, "us_consensus_sources", None))
    counts = _download_us_consensus(
        symbols=_parse_symbols(args.symbols),
        sources=sources,
        snapshot_date=getattr(args, "consensus_snapshot_date", None),
        force=args.force,
        finnworlds_date_from=getattr(
            args,
            "finnworlds_date_from",
            "2000-01-01",
        ),
        finnworlds_date_to=getattr(args, "finnworlds_date_to", None),
        finnworlds_max_calls_per_minute=getattr(
            args,
            "finnworlds_max_calls_per_minute",
            120,
        ),
        finnworlds_retries=getattr(args, "finnworlds_retries", 3),
        fmp_max_calls_per_minute=getattr(args, "fmp_max_calls_per_minute", 720),
        fmp_retries=getattr(args, "fmp_retries", 3),
        alpha_max_calls_per_minute=getattr(args, "alpha_max_calls_per_minute", 75),
        alpha_retries=getattr(args, "consensus_retries", 3),
    )
    print(
        "[DONE] us consensus download "
        f"symbols={counts['symbols']:,}, written={counts['written']:,}, "
        f"skipped={counts['skipped']:,}, failed={counts['failed']:,}, "
        f"no_data={counts.get('no_data', 0):,}, "
        f"fallback_symbols={counts.get('fallback_symbols', 0):,}",
        flush=True,
    )
    for provider, provider_counts in counts.get("providers", {}).items():
        print(
            "[DONE] us consensus provider "
            f"provider={provider} written={provider_counts.get('written', 0):,} "
            f"skipped={provider_counts.get('skipped', 0):,} "
            f"failed={provider_counts.get('failed', 0):,} "
            f"no_data={provider_counts.get('no_data', 0):,} "
            f"auth_disabled={bool(provider_counts.get('auth_disabled'))}",
            flush=True,
        )
    finnworlds = counts.get("providers", {}).get("finnworlds", {})
    if (
        sources == {"finnworlds"}
        and finnworlds.get("auth_disabled")
        and counts.get("fallback_symbols", 0)
    ):
        raise RuntimeError(
            "Finnworlds-only backfill is incomplete because authentication is unavailable"
        )


def download_all_us_dividend(args: argparse.Namespace) -> None:
    from engine.extractors.us_dividends import download_us_dividends

    counts = download_us_dividends(
        symbols=_parse_symbols(args.symbols),
        sources=_parse_us_dividend_sources(getattr(args, "us_dividend_sources", None)),
        snapshot_date=getattr(args, "dividend_snapshot_date", None),
        force=args.force,
        alpha_max_calls_per_minute=getattr(args, "alpha_max_calls_per_minute", 75),
        alpha_retries=getattr(args, "dividend_retries", 3),
    )
    print(
        "[DONE] us dividend download "
        f"symbols={counts['symbols']:,}, written={counts['written']:,}, "
        f"skipped={counts['skipped']:,}, failed={counts['failed']:,}, "
        f"edgartools_not_implemented={counts.get('not_implemented', 0):,}",
        flush=True,
    )


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
    "dividend": download_all_us_dividend,
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
    parser.add_argument(
        "--yfinance-timeout",
        type=float,
        default=15.0,
        help="Maximum seconds to wait for each Yahoo Finance HTTP response.",
    )
    parser.add_argument(
        "--yfinance-retries",
        type=int,
        default=2,
        help="Retries for one Yahoo Finance ticker after an error or empty result.",
    )
    parser.add_argument(
        "--yfinance-retry-backoff",
        type=float,
        default=2.0,
        help="Initial Yahoo Finance retry delay in seconds; doubles per retry.",
    )
    parser.add_argument(
        "--yfinance-repair",
        action="store_true",
        help="Enable yfinance price-repair processing; disabled by default for batch stability.",
    )
    parser.add_argument("--hankyung-token", help="JWT bearer token for Hankyung consensus downloads.")
    parser.add_argument(
        "--consensus-sources",
        default="hankyung,valuefinder,equity",
        help="Comma-separated KR consensus sources: hankyung,valuefinder,equity,html,all.",
    )
    parser.add_argument(
        "--us-consensus-sources",
        default="finnworlds,fmp,alpha-vantage,yfinance",
        help="Comma-separated US consensus sources: finnworlds,fmp,alpha-vantage,yfinance,all.",
    )
    parser.add_argument(
        "--finnworlds-date-from",
        type=_parse_date_arg,
        default="2000-01-01",
        help="Inclusive Finnworlds ratings start date. Defaults to 2000-01-01.",
    )
    parser.add_argument(
        "--finnworlds-date-to",
        type=_parse_date_arg,
        help="Inclusive Finnworlds ratings end date. Defaults to the consensus snapshot date.",
    )
    parser.add_argument(
        "--finnworlds-max-calls-per-minute",
        type=int,
        default=120,
        choices=range(1, 121),
        metavar="1..120",
        help="Global Finnworlds rolling-window limit. Defaults to 120.",
    )
    parser.add_argument(
        "--finnworlds-retries",
        type=int,
        default=3,
        help="Retries per Finnworlds company-ratings request.",
    )
    parser.add_argument(
        "--fmp-max-calls-per-minute",
        type=int,
        default=720,
        choices=range(1, 751),
        metavar="1..750",
        help="Global FMP rolling-window limit. Defaults to 720.",
    )
    parser.add_argument("--fmp-retries", type=int, default=3, help="Retries per FMP consensus request.")
    parser.add_argument(
        "--alpha-max-calls-per-minute",
        type=int,
        default=75,
        choices=range(1, 76),
        metavar="1..75",
        help="Global Alpha Vantage rolling-window limit. Defaults to 75.",
    )
    parser.add_argument("--consensus-retries", type=int, default=3, help="Retries per Alpha Vantage consensus request.")
    parser.add_argument("--consensus-snapshot-date", type=_parse_date_arg, help="Override consensus bronze snapshot date.")
    parser.add_argument(
        "--us-dividend-sources",
        default="alpha-vantage,edgartools,yfinance",
        help="Comma-separated US dividend sources: alpha-vantage,edgartools,yfinance,all.",
    )
    parser.add_argument("--dividend-retries", type=int, default=3, help="Retries per Alpha Vantage dividend request.")
    parser.add_argument("--dividend-snapshot-date", type=_parse_date_arg, help="Override US dividend bronze snapshot date.")
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


def _parse_us_consensus_sources(value: str | None) -> set[str]:
    aliases = {
        "all": {"finnworlds", "fmp", "alpha-vantage", "yfinance"},
        "finnworld": {"finnworlds"},
        "finnworlds": {"finnworlds"},
        "fmp": {"fmp"},
        "alpha": {"alpha-vantage"},
        "yahoo": {"yfinance"},
        "yfinance": {"yfinance"},
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
        elif source in {"finnworlds", "fmp", "alpha-vantage", "yfinance"}:
            sources.add(source)
        else:
            raise SystemExit("unknown US consensus source: " + source)
    return sources


def _parse_us_dividend_sources(value: str | None) -> set[str]:
    aliases = {
        "all": {"alpha-vantage", "edgartools", "yfinance"},
        "alpha": {"alpha-vantage"},
        "edgar": {"edgartools"},
        "yahoo": {"yfinance"},
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
        elif source in {"alpha-vantage", "edgartools", "yfinance"}:
            sources.add(source)
        else:
            raise SystemExit("unknown US dividend source: " + source)
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

