from __future__ import annotations

import argparse
import json
from typing import Iterable

from engine.extractors.earnings_call_transcripts import (
    US_EARNINGS_CALL_SOURCE_PRIORITY,
    download_us_earnings_call_transcripts,
)


def _split_values(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [
        item.strip()
        for value in values
        for item in str(value).split(",")
        if item.strip()
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download US earnings-call transcripts to data-lake/bronze with "
            "FMP first and Alpha Vantage as fallback."
        )
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Optional comma- or space-separated tickers. Omit for all US common equities.",
    )
    parser.add_argument(
        "--sources",
        default=",".join(US_EARNINGS_CALL_SOURCE_PRIORITY),
        help="Comma-separated providers: fmp,alpha-vantage,all.",
    )
    parser.add_argument(
        "--start-quarter",
        help=(
            "Optional YYYYQ1..YYYYQ4 lower bound. When omitted, all FMP dates are "
            "used and Alpha Vantage starts at 2010Q1."
        ),
    )
    parser.add_argument(
        "--end-quarter",
        help="Optional YYYYQ1..YYYYQ4 upper bound; defaults to the current quarter.",
    )
    parser.add_argument("--offset", type=int, default=0, help="Universe start offset.")
    parser.add_argument("--limit", type=int, help="Maximum number of symbols in this batch.")
    parser.add_argument("--force", action="store_true", help="Redownload cached quarters.")
    parser.add_argument(
        "--no-refresh-universe",
        action="store_true",
        help="Use the cached US equity universe instead of refreshing Nasdaq Trader files.",
    )
    parser.add_argument(
        "--refresh-recent-quarters",
        type=int,
        default=2,
        help="Recheck cached no-data results in the latest N quarters. Defaults to 2.",
    )
    parser.add_argument("--fmp-max-calls-per-minute", type=int, default=720)
    parser.add_argument("--fmp-retries", type=int, default=3)
    parser.add_argument("--alpha-max-calls-per-minute", type=int, default=75)
    parser.add_argument("--alpha-retries", type=int, default=3)
    parser.add_argument(
        "--alpha-retry-passes",
        type=int,
        default=1,
        help="Retry still-missing Alpha Vantage quarters after the primary pass.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    counts = download_us_earnings_call_transcripts(
        symbols=_split_values(args.symbols),
        sources=_split_values([args.sources]),
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        offset=args.offset,
        limit=args.limit,
        force=args.force,
        refresh_universe=not args.no_refresh_universe,
        refresh_recent_quarters=args.refresh_recent_quarters,
        fmp_max_calls_per_minute=args.fmp_max_calls_per_minute,
        fmp_retries=args.fmp_retries,
        alpha_max_calls_per_minute=args.alpha_max_calls_per_minute,
        alpha_retries=args.alpha_retries,
        alpha_retry_passes=args.alpha_retry_passes,
    )
    print(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
