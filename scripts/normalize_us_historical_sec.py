from __future__ import annotations

"""Minimal Windows-safe launcher for historical US SEC normalization."""

import argparse
import csv
from pathlib import Path


DEFAULT_TARGET_PATH = (
    Path(__file__).resolve().parents[1]
    / "data-lake"
    / "meta"
    / "us_historical_2006_2016_factor_targets.csv"
)


def read_target_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = {
            str(row.get("security_id") or "").strip().removeprefix("SEC_US_")
            for row in csv.DictReader(stream)
        }
    return sorted(symbol for symbol in symbols if symbol)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize local SEC filing bundles before lower-authority US sources."
    )
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--progress-interval", type=int, default=100)
    parser.add_argument("--symbols", help="Optional comma-separated ticker list")
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--no-notes", action="store_true")
    parser.add_argument("--no-debug", action="store_true")
    args = parser.parse_args()

    if args.start_year > args.end_year:
        raise ValueError("start-year must not be after end-year")
    symbols = None
    if args.symbols:
        symbols = [
            value.strip().upper()
            for value in args.symbols.split(",")
            if value.strip()
        ]
    else:
        symbols = read_target_symbols(args.target_path)
        if not symbols:
            raise RuntimeError(f"no target symbols found: {args.target_path}")

    from engine.transformers.sec_filings import normalize_us_sec_filings

    written = normalize_us_sec_filings(
        symbols=symbols,
        start_year=args.start_year,
        end_year=args.end_year,
        save_debug=not args.no_debug,
        use_filings=True,
        use_notes=not args.no_notes,
        use_edgartools=False,
        workers=max(1, args.workers),
        progress_interval=max(1, args.progress_interval),
    )
    print(f"[DONE] historical US SEC normalization written={len(written)}", flush=True)


if __name__ == "__main__":
    main()
