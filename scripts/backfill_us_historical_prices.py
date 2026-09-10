from __future__ import annotations

"""Prepend missing Yahoo price history for the frozen US factor universe."""

import argparse
import csv
from pathlib import Path

from engine.core.paths import DATA_LAKE


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")


def read_target_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = {
            str(row.get("security_id") or "").strip().removeprefix("SEC_US_")
            for row in csv.DictReader(stream)
        }
    return sorted(symbol for symbol in symbols if symbol)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--start-date", default="2006-01-01")
    parser.add_argument("--end-date", default="2016-12-31")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=2.0)
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise ValueError("start-date must not be after end-date")

    symbols = read_target_symbols(args.target_path)
    if not symbols:
        raise RuntimeError(f"no target symbols found: {args.target_path}")

    from engine.extractors.market_prices import download_us_price_histories

    written = download_us_price_histories(
        symbols=symbols,
        offset=max(0, args.offset),
        limit=None if args.limit is None else max(0, args.limit),
        start_date=args.start_date,
        end_date=args.end_date,
        sleep_seconds=max(0.0, args.sleep_seconds),
        request_timeout=args.request_timeout,
        retries=max(0, args.retries),
        retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
        repair=args.repair,
    )
    print(
        f"[DONE] historical US prices updated={len(written):,}, "
        f"targets={len(symbols):,}, offset={max(0, args.offset):,}, "
        f"limit={args.limit if args.limit is not None else '-'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
