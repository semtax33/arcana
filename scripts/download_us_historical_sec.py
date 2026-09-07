from __future__ import annotations

"""Resume historical SEC filing downloads for the frozen price universe."""

import argparse
import csv
import json
from pathlib import Path

from engine.core.paths import DATA_LAKE


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
DEFAULT_TICKER_ALIASES_PATH = DATA_LAKE.meta("sec_ticker_aliases.csv")


def read_target_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        symbols = {
            str(row.get("security_id") or "").strip().removeprefix("SEC_US_")
            for row in rows
        }
    return sorted(symbol for symbol in symbols if symbol)


def read_known_sec_symbols(*paths: Path) -> set[str]:
    known: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            known.update(
                str(row.get("ticker") or "").strip().upper()
                for row in csv.DictReader(stream)
            )
    return {symbol for symbol in known if symbol}


def partition_resolvable_symbols(
    symbols: list[str], *, known_symbols: set[str]
) -> tuple[list[str], list[str]]:
    resolved = [symbol for symbol in symbols if symbol in known_symbols]
    unresolved = [symbol for symbol in symbols if symbol not in known_symbols]
    return resolved, unresolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download local 10-K/10-Q bundles for a frozen US target universe."
    )
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--start-date", default="2006-01-01")
    parser.add_argument("--end-date", default="2016-12-31")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=10.0)
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Zero-based offset after ticker-to-CIK resolution and CIK deduplication.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of deduplicated CIKs to process after --offset.",
    )
    parser.add_argument(
        "--trust-complete-checkpoints",
        action="store_true",
        help=(
            "Skip per-file stat checks for matching complete checkpoints. "
            "Use only when a full download audit will run afterward."
        ),
    )
    parser.add_argument(
        "--disable-http-cache",
        action="store_true",
        help=(
            "Disable edgartools' duplicate on-disk HTTP response cache. "
            "Downloaded filing artifacts and checkpoints remain persistent."
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    from engine.extractors.sec_filings import download_us_filing_htmls
    from engine.transformers._internal.edgar_identity import configure_edgar_http_cache

    if args.disable_http_cache:
        configure_edgar_http_cache(enabled=False)

    target_symbols = read_target_symbols(args.target_path)
    known_symbols = read_known_sec_symbols(
        DEFAULT_TICKER_MAP_PATH,
        DEFAULT_TICKER_ALIASES_PATH,
    )
    symbols, unresolved = partition_resolvable_symbols(
        target_symbols,
        known_symbols=known_symbols,
    )
    if unresolved:
        print(
            json.dumps(
                {
                    "event": "unresolved_sec_identities",
                    "count": len(unresolved),
                    "symbols": unresolved,
                    "policy": "abstain_without_verified_ticker-to-CIK mapping",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    if not symbols:
        raise RuntimeError(f"no target symbols found: {args.target_path}")
    summary = download_us_filing_htmls(
        symbols=symbols,
        start_date=args.start_date,
        end_date=args.end_date,
        forms=["10-K", "10-Q"],
        verify_resume_files=not args.trust_complete_checkpoints,
        workers=max(1, args.workers),
        sleep_seconds=max(0.0, args.sleep_seconds),
        retries=max(0, args.retries),
        retry_backoff_seconds=max(0.0, args.retry_backoff),
        offset=max(0, args.offset),
        limit=None if args.limit is None else max(0, args.limit),
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
