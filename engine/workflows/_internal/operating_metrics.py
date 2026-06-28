from __future__ import annotations

import argparse

from engine.loaders.operating_metrics import load_operating_metrics
from engine.transformers.operating_metrics import create_operating_metric_gold_for_stocks, normalize_stock_code


def run_operating_metrics_workflow(
    stock_codes: list[str] | None = None,
    *,
    load: bool = False,
    dry_run: bool = False,
    start_period: str | None = None,
    end_period: str | None = None,
    all_periods: bool = False,
    progress: bool = True,
    progress_interval: int = 25,
    fail_fast: bool = False,
) -> dict[str, object]:
    results = create_operating_metric_gold_for_stocks(
        stock_codes,
        estimate_start_period=start_period,
        estimate_end_period=end_period,
        estimate_all_periods=all_periods,
        progress=progress,
        progress_interval=progress_interval,
        continue_on_error=not fail_fast,
    )
    load_counts = load_operating_metrics(stock_codes, dry_run=dry_run) if load else {}
    return {"transform_results": results, "load_counts": load_counts}


def _parse_stock_codes(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [normalize_stock_code(item) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build operating metric gold CSVs and optionally load them.")
    parser.add_argument("--stock-codes", help="Comma-separated stock codes. Defaults to all silver dirs.")
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-period", help="First source actual period for estimates, e.g. 2023.12.")
    parser.add_argument("--end-period", help="Last source actual period for estimates, e.g. 2026.03.")
    parser.add_argument("--all-periods", action="store_true")
    parser.add_argument("--progress-interval", type=int, default=25)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    output = run_operating_metrics_workflow(
        _parse_stock_codes(args.stock_codes),
        load=args.load,
        dry_run=args.dry_run,
        start_period=args.start_period,
        end_period=args.end_period,
        all_periods=args.all_periods,
        progress=not args.no_progress,
        progress_interval=args.progress_interval,
        fail_fast=args.fail_fast,
    )
    print(f"transformed={len(output['transform_results'])}")
    for table_name, count in sorted(output["load_counts"].items()):
        print(f"{table_name}: {count:,}")


if __name__ == "__main__":
    main()
