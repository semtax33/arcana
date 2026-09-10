"""Prepare the complete factor contract for reviewed historical KR issuers.

This collects calculation evidence only: it does not insert native values or
complete historical rebuild markers. Missing cells remain explicit in coverage.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import warnings

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.loaders.factors import insert_daily_factors
from engine.transformers.factors import preferred_factor_columns, market_applicable_factor_columns
from engine.workflows.financial_history_rebuild import pending_rebuilds
from engine.transformers._internal import factor_metrics, dividend_metrics
from publish_kr_survivorship_financial_factors import verify_existing_publication
from validate_kr_survivorship_financial_factors import SILVER, digest, save, canonical


def input_inventory(symbols):
    """Pin optional absence as well as every KR input used in this source scope."""
    cache = factor_metrics.FactorMarketDataCache(market="kr")
    paths = [getattr(cache, key) for key in ("wacc_risk_free_path", "wacc_erp_path", "wacc_assumptions_path", "wacc_benchmark_path")]
    paths += [factor_metrics.HANKYUNG_CONSENSUS_DAILY_PATH, factor_metrics.HANKYUNG_TARGET_PRICE_CONSENSUS_PATH,
        dividend_metrics.silver_dividend_by_stock_kind_path, dividend_metrics.legacy_silver_dividend_by_stock_kind_path,
        dividend_metrics.silver_dividend_company_summary_path, dividend_metrics.legacy_silver_dividend_company_summary_path,
        DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json")]
    for symbol in symbols:
        panel = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet")
        metadata = panel.with_suffix(".metadata.json")
        info = json.loads(metadata.read_text("utf-8"))
        assert info["status"] == "ready" and info["survivorship_restored"] and info["price_provider"] == "MARCAP"
        # This contract takes both prices and shares from the restored panel.
        paths.extend([panel, metadata])
        paths.extend(factor_metrics.ESTIMATE_GOLD_ROOT / symbol / name for name in
            ("arcana_estimate_consensus.csv", "arcana_estimate_component.csv"))
        manifest = DATA_LAKE.silver("dart", "normalized", "history", symbol, "manifest.json")
        paths.append(manifest)
        records = json.loads(manifest.read_text("utf-8"))["receipts"]
        paths.extend(manifest.parent / record["normalized_path"] for record in records)
    return {str(p): digest(p) if p.exists() else None for p in sorted(set(paths))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_full_factor_preparation")
    parser.add_argument("--symbols", nargs="+")
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve the earlier calculation evidence"
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    contract = preferred_factor_columns()
    applicable = set(market_applicable_factor_columns("kr"))
    assert len(contract) == len(set(contract)) and not any(f.startswith("lab_") for f in contract)
    implementations = [Path(__file__), ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "engine/transformers/_internal/financial_history.py", ROOT / "engine/transformers/_internal/filing_periods.py",
        ROOT / "engine/loaders/_internal/clickhouse_factors.py",
        ROOT / "engine/transformers/_internal/wacc_inputs.py", ROOT / "engine/transformers/_internal/dividend_metrics.py"]
    hashes = {}
    for path in implementations:
        target = args.output / "implementation" / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        hashes[str(path)] = digest(path)
    symbols = sorted(item["symbol"] for item in pending_rebuilds("kr", "annual", symbols=args.symbols).values())
    inventory = input_inventory(symbols)
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(), implementation_sha256=hashes,
        input_inventory=inventory,
        global_contract=contract, kr_applicable_contract=sorted(applicable), results=[], native_published=False)
    save(args.output / "summary.json", report)
    for basis in ("annual", "quarterly", "ttm"):
        pending = pending_rebuilds("kr", basis, symbols=args.symbols)
        for sid, item in pending.items():
            symbol, start = item["symbol"], item["from_date"]
            manifest = Path(item["manifest_path"])
            verify_existing_publication(dict(manifest_path=str(manifest), sha256=item["manifest_sha256"]))
            panel = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet")
            prices = pd.read_parquet(panel, columns=["trade_date"])
            days = pd.to_datetime(prices.trade_date).loc[lambda s: s.ge(start) & s.le("2026-09-10")]
            actual = insert_daily_factors(stock_codes=[symbol], financial_basis=basis, market="kr",
                start_date=start, end_date="2026-09-10", factor_ids=contract, dry_run=True, insert_catalog=False,
                financial_dir=DATA_LAKE.silver("dart", "normalized"), use_edgartools=False,
                require_report_metadata=True, wacc_online_backfill=False)
            canonical(actual)  # Reject duplicate cells before saving a candidate.
            assert set(actual.security_id) <= {sid} and set(actual.financial_basis) <= {basis}
            assert set(pd.to_datetime(actual.trade_date)) <= set(days)
            target = args.output / symbol / basis
            target.mkdir(parents=True)
            actual.to_parquet(target / "prepared.parquet", index=False)
            counts = actual.groupby("factor_id").size().to_dict()
            coverage = pd.DataFrame([dict(factor_id=f, market_applicable=f in applicable,
                available_cells=counts.get(f, 0), price_date_opportunities=len(days),
                missing_cells=len(days)-counts.get(f, 0)) for f in contract])
            coverage.to_parquet(target / "coverage.parquet", index=False)
            assert digest(manifest) == item["manifest_sha256"]
            result = dict(symbol=symbol, basis=basis, rows=len(actual), factor_ids=len(counts),
                price_dates=len(days), start=start, end="2026-09-10", source_version=item,
                price_sha256=digest(panel), prepared_sha256=digest(target / "prepared.parquet"),
                coverage_sha256=digest(target / "coverage.parquet"))
            report["results"].append(result)
            save(args.output / "summary.json", report)
            print({k: result[k] for k in ("symbol", "basis", "rows", "factor_ids", "price_dates")}, flush=True)
    for path, checksum in hashes.items():
        assert digest(path) == checksum, "Implementation changed during preparation"
    assert input_inventory(symbols) == inventory, "A calculation input or optional file presence changed during preparation"
    report.update(status="prepared_not_independently_validated", finished_at=datetime.now(timezone.utc).isoformat())
    save(args.output / "summary.json", report)


if __name__ == "__main__":
    main()
