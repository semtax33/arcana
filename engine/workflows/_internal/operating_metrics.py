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
    as_of_date: str | None = None,
    write_history: bool = False,
    load_history: bool = False,
    normalized_statement_dir: str | None = None,
    report_metadata_path: str | None = None,
    progress: bool = True,
    progress_interval: int = 25,
    fail_fast: bool = False,
) -> dict[str, object]:
    results = create_operating_metric_gold_for_stocks(
        stock_codes,
        estimate_start_period=start_period,
        estimate_end_period=end_period,
        estimate_all_periods=all_periods,
        as_of_date=as_of_date,
        write_history=write_history,
        **({"normalized_statement_dir": normalized_statement_dir} if normalized_statement_dir else {}),
        **({"report_metadata_path": report_metadata_path} if report_metadata_path else {}),
        progress=progress,
        progress_interval=progress_interval,
        continue_on_error=not fail_fast,
    )
    load_counts = (
        load_operating_metrics(
            stock_codes,
            dry_run=dry_run,
            load_history=load_history,
            as_of_date=as_of_date,
            progress=progress,
            progress_interval=progress_interval,
        )
        if load
        else {}
    )
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
    parser.add_argument("--as-of-date")
    parser.add_argument("--write-history", action="store_true")
    parser.add_argument("--load-history", action="store_true")
    parser.add_argument("--normalized-statement-dir")
    parser.add_argument("--report-metadata-path")
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
        as_of_date=args.as_of_date,
        write_history=args.write_history,
        load_history=args.load_history,
        normalized_statement_dir=args.normalized_statement_dir,
        report_metadata_path=args.report_metadata_path,
        progress=not args.no_progress,
        progress_interval=args.progress_interval,
        fail_fast=args.fail_fast,
    )
    print(f"transformed={len(output['transform_results'])}")
    for table_name, count in sorted(output["load_counts"].items()):
        print(f"{table_name}: {count:,}")


if __name__ == "__main__":
    main()
