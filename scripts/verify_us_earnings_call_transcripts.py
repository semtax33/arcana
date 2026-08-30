from __future__ import annotations

import argparse
import json

from engine.extractors.earnings_call_transcripts import (
    ALPHA_VANTAGE_FIRST_QUARTER,
    audit_us_earnings_call_transcripts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit US earnings-call transcript bronze quarter coverage."
    )
    parser.add_argument("--symbols", nargs="*", help="Optional tickers; omit for all US equities.")
    parser.add_argument("--start-quarter", default=ALPHA_VANTAGE_FIRST_QUARTER)
    parser.add_argument("--end-quarter")
    parser.add_argument("--sample-limit", type=int, default=100)
    parser.add_argument("--output", help="Optional JSON verification report path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [
        item.strip()
        for value in (args.symbols or [])
        for item in str(value).split(",")
        if item.strip()
    ] or None
    report = audit_us_earnings_call_transcripts(
        symbols=symbols,
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        verification_path=args.output,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
