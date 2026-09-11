"""Inspect debt availability through the public financial readers, without writes.

This scopes a calculation repair; it does not approve the underlying debt totals
or certify native/PIT data. Source files actually opened are pinned for replay.
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
from engine.transformers.factors import read_annual_financials, read_quarterly_financials, read_ttm_financials
from audit_kr_historical_share_dependencies import ReadEvidence

READERS = dict(annual=read_annual_financials, quarterly=read_quarterly_financials, ttm=read_ttm_financials)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Diagnostic output must remain in Silver")
    output.mkdir(parents=True, exist_ok=False)
    symbols = dict(kr=sorted(p.parent.name for p in DATA_LAKE.silver("dart", "normalized", "history").glob("*/manifest.json")),
                   us=["ALXN", "ATVI", "CELG", "TWTR"])
    assert len(symbols["kr"]) == 12, "Review changed; choose the intended scope explicitly"
    implementations = [Path(__file__), ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "engine/transformers/_internal/financial_history.py", ROOT / "engine/transformers/_internal/filing_periods.py",
        ROOT / "engine/transformers/_internal/statement_files.py"]
    pins = {str(p): sha256_file(p) for p in implementations}
    evidence = ReadEvidence(output)
    sys.addaudithook(evidence.hook)
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
        symbols=symbols, cases=[], implementation_pins=pins, input_pins={},
        production_changed=False, debt_source_totals_approved=False, native_or_snapshots_verified=False)
    export_json(output / "summary.json", report)
    try:
        for market, tickers in symbols.items():
            for symbol in tickers:
                for basis, reader in READERS.items():
                    options = dict(market=market, use_edgartools=False, require_report_metadata=True)
                    if market == "us":
                        options["report_metadata_path"] = DATA_LAKE.silver("sec", "us_report_metadata.csv")
                    frame = reader(symbol, **options)
                    folder = output / market / symbol
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{basis}.parquet"
                    frame.to_parquet(path, index=False)
                    values = frame.reindex(columns=["dltt", "dlc", "debt", "debt_to_equity"])
                    partial = values.dltt.notna() ^ values.dlc.notna()
                    record = dict(market=market, symbol=symbol, basis=basis, rows=len(frame),
                        both_components_known=int((values.dltt.notna() & values.dlc.notna()).sum()),
                        neither_component_known=int((values.dltt.isna() & values.dlc.isna()).sum()),
                        partial_debt_rows=int(partial.sum()),
                        partial_rows_with_total=int((partial & values.debt.notna()).sum()),
                        partial_rows_with_leverage=int((partial & values.debt_to_equity.notna()).sum()),
                        path=str(path), sha256=sha256_file(path))
                    report["cases"].append(record)
                    export_json(output / "summary.json", report)
                    print(market, symbol, basis, "rows", len(frame), "partial", record["partial_debt_rows"],
                          "partial with total", record["partial_rows_with_total"], flush=True)
        for path, stat in evidence.snapshot().items():
            current = Path(path).stat()
            assert dict(size=current.st_size, mtime_ns=current.st_mtime_ns) == stat, path
            report["input_pins"][path] = sha256_file(path)
        for path, digest in pins.items():
            assert sha256_file(path) == digest, path
        assert len(report["cases"]) == 48
        report.update(status="inspected_not_source_approved", finished_at=datetime.now(timezone.utc).isoformat(),
            rows=sum(r["rows"] for r in report["cases"]),
            partial_debt_rows=sum(r["partial_debt_rows"] for r in report["cases"]),
            partial_rows_with_total=sum(r["partial_rows_with_total"] for r in report["cases"]))
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
    print(report["status"], "partial", report["partial_debt_rows"], flush=True)


if __name__ == "__main__":
    main()
