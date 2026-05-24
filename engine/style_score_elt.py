from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.style_score_pipeline import (
    build_factor_scores,
    build_style_score_range,
    build_style_scores,
    debug_single_security_score,
    validate_style_scores,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and inspect ClickHouse style scores.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    factor_parser = subparsers.add_parser("build-factor-scores")
    factor_parser.add_argument("--trade-date", required=True)
    factor_parser.add_argument("--factor-asof-mode", default="exact", choices=["exact", "asof"])
    factor_parser.add_argument("--financial-basis", default="annual")
    factor_parser.add_argument("--include-financials", action="store_true")

    style_parser = subparsers.add_parser("build-style-scores")
    style_parser.add_argument("--trade-date")
    style_parser.add_argument("--start-date")
    style_parser.add_argument("--end-date")
    style_parser.add_argument("--style-profile", default="DEFAULT")
    style_parser.add_argument("--factor-asof-mode", default="asof", choices=["exact", "asof"])
    style_parser.add_argument("--financial-basis", default="annual")
    style_parser.add_argument("--include-financials", action="store_true")
    style_parser.add_argument(
        "--style-only",
        action="store_true",
        help="Only build style scores from existing factor scores.",
    )
    style_parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip trade dates that already have style scores for the profile.",
    )

    validate_parser = subparsers.add_parser("validate-style-scores")
    validate_parser.add_argument("--trade-date", required=True)
    validate_parser.add_argument("--style-profile", default="DEFAULT")

    debug_parser = subparsers.add_parser("debug-single-security-score")
    debug_parser.add_argument("--trade-date", required=True)
    debug_parser.add_argument("--security-id", required=True)
    debug_parser.add_argument("--style-profile", default="DEFAULT")

    args = parser.parse_args()
    if args.command == "build-factor-scores":
        factor_scores, snapshot = build_factor_scores(
            args.trade_date,
            factor_asof_mode=args.factor_asof_mode,
            exclude_financials=not args.include_financials,
            financial_basis=args.financial_basis,
        )
        print(
            f"factor_score_rows={len(factor_scores):,}, "
            f"industry_snapshot_rows={len(snapshot):,}"
        )
    elif args.command == "build-style-scores":
        if args.start_date or args.end_date:
            if not args.start_date or not args.end_date:
                raise SystemExit("--start-date and --end-date must be provided together")
            result = build_style_score_range(
                args.start_date,
                args.end_date,
                style_profile=args.style_profile,
                factor_asof_mode=args.factor_asof_mode,
                build_factor_scores_first=not args.style_only,
                exclude_financials=not args.include_financials,
                financial_basis=args.financial_basis,
                skip_existing=args.skip_existing,
            )
            print(
                "trade_dates={trade_dates:,}, processed_dates={processed_dates:,}, "
                "skipped_dates={skipped_dates:,}, factor_score_rows={factor_score_rows:,}, "
                "industry_snapshot_rows={industry_snapshot_rows:,}, style_score_rows={style_score_rows:,}, "
                "empty_factor_score_dates={empty_factor_score_dates:,}, "
                "empty_style_score_dates={empty_style_score_dates:,}".format(
                    trade_dates=len(result.trade_dates),
                    processed_dates=len(result.processed_dates),
                    skipped_dates=len(result.skipped_dates),
                    factor_score_rows=result.factor_score_rows,
                    industry_snapshot_rows=result.industry_snapshot_rows,
                    style_score_rows=result.style_score_rows,
                    empty_factor_score_dates=len(result.empty_factor_score_dates),
                    empty_style_score_dates=len(result.empty_style_score_dates),
                )
            )
        else:
            if not args.trade_date:
                raise SystemExit("--trade-date or --start-date/--end-date is required")
            style_scores = build_style_scores(
                args.trade_date,
                style_profile=args.style_profile,
            )
            print(f"style_score_rows={len(style_scores):,}")
    elif args.command == "validate-style-scores":
        report = validate_style_scores(
            args.trade_date,
            style_profile=args.style_profile,
        )
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    elif args.command == "debug-single-security-score":
        report = debug_single_security_score(
            args.trade_date,
            args.security_id,
            style_profile=args.style_profile,
        )
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
