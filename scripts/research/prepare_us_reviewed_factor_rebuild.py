"""Prepare the full factor contract for the four restored US issuers.

Retain explicit abstentions and full available Alpha price history. This is
calculation evidence only; no native/PIT writes or rebuild-marker completion.
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
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.loaders.factors import insert_daily_factors
from engine.transformers.factors import preferred_factor_columns
from audit_kr_historical_share_dependencies import ReadEvidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--normalize-retained", action="store_true",
                        help="Stage retained SEC accessions and duration facts before preparing factors")
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.is_relative_to((DATA_LAKE.root / "silver").resolve())
    output.mkdir(parents=True, exist_ok=False)
    symbols = ["ALXN", "ATVI", "CELG", "TWTR"]
    contract = preferred_factor_columns()
    assert not any(f.startswith("lab_") for f in contract)
    implementations = [Path(__file__), ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "engine/transformers/_internal/financial_history.py", ROOT / "engine/transformers/_internal/filing_periods.py",
        ROOT / "engine/transformers/_internal/sec_filings.py", ROOT / "engine/transformers/_internal/sec_accession_history.py",
        ROOT / "engine/loaders/_internal/clickhouse_factors.py"]
    pins = {str(p): sha256_file(p) for p in implementations}
    windows = {}
    for symbol in symbols:
        path = DATA_LAKE.silver("corporate_actions", "prices", "us", f"us_{symbol}.parquet")
        metadata = path.with_suffix(".metadata.json")
        info = json.loads(metadata.read_bytes())
        assert info["status"] == "ready" and info["price_provider"] == "ALPHA_VANTAGE"
        assert info["price_basis"] == "split_only" and info["symbol"] == symbol
        original = Path(info["source_path"])
        assert original.resolve().is_relative_to((DATA_LAKE.root / "bronze").resolve())
        assert sha256_file(original) == info["source_sha256"]
        for p in (path, metadata, original):
            pins[str(p.resolve())] = sha256_file(p)
        dates = pd.to_datetime(pd.read_parquet(path, columns=["trade_date"]).trade_date)
        dates = dates.loc[dates.le("2026-09-10")]
        assert len(dates) and not dates.duplicated().any()
        windows[symbol] = dict(start=str(dates.min().date()), end=str(dates.max().date()), price_dates=len(dates))
    evidence = ReadEvidence(output)
    sys.addaudithook(evidence.hook)
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
        factor_contract=contract, symbols=symbols, windows=windows, cases=[], input_pins=pins,
        native_published=False, snapshots_published=False, rebuild_markers_completed=False,
        coverage_complete=False)
    export_json(output / "summary.json", report)
    try:
        financial_dir = DATA_LAKE.silver("sec", "normalized")
        metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
        if args.normalize_retained:
            from engine.transformers.sec_filings import normalize_us_sec_filings
            financial_dir = output / "normalized"
            metadata_path = output / "us_report_metadata.csv"
            normalize_us_sec_filings(symbols=symbols, start_year=1999, end_year=2026,
                output_dir=financial_dir, report_metadata_path=metadata_path,
                use_notes=False, use_edgartools=False, workers=1, save_debug=False)
            report["normalized_input_pins"] = {str(p.resolve()): sha256_file(p)
                for p in [metadata_path, *financial_dir.rglob("*")] if p.is_file()}
        report.update(financial_dir=str(financial_dir.resolve()), report_metadata_path=str(metadata_path.resolve()),
                      retained_normalization_staged=args.normalize_retained)
        export_json(output / "summary.json", report)
        for basis in ("annual", "quarterly", "ttm"):
            for symbol in symbols:
                window = windows[symbol]
                frame = insert_daily_factors(stock_codes=[symbol], market="us", financial_basis=basis,
                    start_date=window["start"], end_date=window["end"], factor_ids=contract,
                    dry_run=True, insert_catalog=False, include_abstentions=True,
                    financial_dir=financial_dir, report_metadata_path=metadata_path,
                    use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
                assert not frame.duplicated(["security_id", "trade_date", "financial_basis", "factor_id"]).any()
                assert set(frame.security_id) == {f"SEC_US_{symbol}"}
                assert set(frame.financial_basis) == {basis}
                assert set(frame.factor_id) <= set(contract)
                folder = output / symbol / basis
                folder.mkdir(parents=True)
                path = folder / "prepared.parquet"
                frame.to_parquet(path, index=False)
                counts = frame.groupby("factor_id").factor_value.agg(["size", "count"]).reset_index()
                counts.rename(columns={"size": "native_events", "count": "finite_cells"}).to_parquet(folder / "coverage.parquet", index=False)
                case = dict(symbol=symbol, basis=basis, **window, rows=len(frame),
                    finite_cells=int(frame.factor_value.notna().sum()), abstention_events=int(frame.factor_value.isna().sum()),
                    path=str(path), sha256=sha256_file(path))
                report["cases"].append(case)
                export_json(output / "summary.json", report)
                print(symbol, basis, "rows", len(frame), "abstentions", case["abstention_events"], flush=True)
        for path, stat in evidence.snapshot().items():
            current = Path(path).stat()
            assert dict(size=current.st_size, mtime_ns=current.st_mtime_ns) == stat, path
            pins[path] = sha256_file(path)
        for path, digest in pins.items():
            assert sha256_file(path) == digest, path
        for path, digest in report.get("normalized_input_pins", {}).items():
            assert sha256_file(path) == digest, path
        assert len(report["cases"]) == 12
        report.update(status="prepared_not_independently_validated", finished_at=datetime.now(timezone.utc).isoformat(),
            rows=sum(r["rows"] for r in report["cases"]),
            limitations="Complete factor source semantics, daily availability, native/PIT reconciliation and FactorLab consumer checks remain required before publication.")
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        evidence.enabled = False
        for path in implementations:
            destination = output / "implementation" / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        export_json(output / "summary.json", report)
    print(report["status"], report["rows"], flush=True)


if __name__ == "__main__":
    main()
