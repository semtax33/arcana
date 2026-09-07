from __future__ import annotations

"""Refresh missing Company Facts only for the frozen historical US universe."""

import argparse
import csv
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_PATH = (
    PROJECT_ROOT / "data-lake" / "meta" / "us_historical_2006_2016_factor_targets.csv"
)
DEFAULT_TICKER_MAP_PATH = PROJECT_ROOT / "data-lake" / "meta" / "sec_company_tickers.csv"
DEFAULT_TICKER_ALIASES_PATH = PROJECT_ROOT / "data-lake" / "meta" / "sec_ticker_aliases.csv"


def read_ticker_to_cik(*paths: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                ticker = str(row.get("ticker") or "").strip().upper()
                cik = "".join(character for character in str(row.get("cik") or "") if character.isdigit())
                if ticker and cik:
                    mapping.setdefault(ticker, str(int(cik)))
    return mapping


def resolve_target_ciks(
    target_path: Path,
    *,
    ticker_to_cik: dict[str, str],
) -> tuple[list[str], list[str]]:
    with target_path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = sorted(
            {
                str(row.get("security_id") or "").strip().removeprefix("SEC_US_")
                for row in csv.DictReader(stream)
            }
        )
    unresolved = [symbol for symbol in symbols if symbol and symbol not in ticker_to_cik]
    ciks = sorted(
        {ticker_to_cik[symbol] for symbol in symbols if symbol in ticker_to_cik},
        key=int,
    )
    return ciks, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download only missing SEC Company Facts for historical US targets."
    )
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    args = parser.parse_args()

    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if "@" not in identity:
        raise RuntimeError("EDGAR_IDENTITY must contain a contact email")

    ticker_to_cik = read_ticker_to_cik(
        DEFAULT_TICKER_ALIASES_PATH,
        DEFAULT_TICKER_MAP_PATH,
    )
    ciks, unresolved = resolve_target_ciks(
        args.target_path,
        ticker_to_cik=ticker_to_cik,
    )
    print(
        {
            "unique_ciks": len(ciks),
            "unresolved_count": len(unresolved),
            "unresolved_symbols": unresolved,
        },
        flush=True,
    )

    from engine.extractors.sec_filings import download_us_companyfacts

    written = download_us_companyfacts(
        symbols=ciks,
        sleep_seconds=max(0.0, args.sleep_seconds),
        user_agent=identity,
    )
    print(f"[DONE] SEC Company Facts written={len(written)}", flush=True)


if __name__ == "__main__":
    main()
