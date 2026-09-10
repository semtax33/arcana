"""Prepare independently checked capital values and explicit missing-run events.

The finite-value oracle comes from original reviewed disclosure arithmetic.
No native values, snapshots, or historical rebuild completion markers are written.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.loaders.factors import insert_daily_factors
from prepare_kr_survivorship_full_factors import input_inventory
from validate_kr_survivorship_capital_factors import FACTORS
from validate_kr_survivorship_financial_factors import KEYS, canonical, digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", default=["003410", "035480"])
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve prior evidence"
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    prep = json.loads((args.preparation / "summary.json").read_text("utf-8"))
    validation = json.loads((args.validation / "summary.json").read_text("utf-8"))
    assert validation["status"] == "validated"
    assert validation["preparation_sha256"] == digest(args.preparation / "summary.json")
    cases = [r for r in prep["results"] if r["symbol"] in args.symbols]
    assert len(cases) == len(set(args.symbols)) * 3
    inputs = input_inventory(args.symbols)
    assert all(prep["input_inventory"][path] == checksum for path, checksum in inputs.items())
    implementations = [Path(__file__), ROOT / "engine/loaders/_internal/clickhouse_factors.py",
        ROOT / "engine/loaders/factor_snapshots.py", ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "engine/transformers/_internal/financial_history.py", ROOT / "engine/transformers/_internal/filing_periods.py",
        ROOT / "engine/workflows/_internal/refresh_workflow.py", ROOT / "engine/workflows/financial_history_rebuild.py"]
    hashes = {str(p): digest(p) for p in implementations}
    for source in implementations:
        target = args.output / "implementation" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    report = dict(status="running", native_published=False, snapshots_published=False,
        coverage_complete=False, factor_ids=FACTORS, cases=[], input_inventory=inputs,
        implementation_sha256=hashes, validation_sha256=digest(args.validation / "summary.json"),
        prior_preparation_sha256=digest(args.preparation / "summary.json"))
    save(args.output / "summary.json", report)
    for case in cases:
        symbol, basis = case["symbol"], case["basis"]
        oracle_case = next(r for r in validation["cases"] if r["symbol"] == symbol and r["basis"] == basis)
        oracle_path = args.validation / symbol / basis / "expected.parquet"
        assert digest(oracle_path) == oracle_case["expected_sha256"]
        oracle = canonical(pd.read_parquet(oracle_path))
        actual = insert_daily_factors(stock_codes=[symbol], financial_basis=basis, market="kr",
            start_date=case["start"], end_date=case["end"], factor_ids=FACTORS, dry_run=True,
            insert_catalog=False, financial_dir=DATA_LAKE.silver("dart", "normalized"),
            use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False,
            include_abstentions=True)
        a = canonical(actual)
        finite = a.loc[np.isfinite(a.factor_value)]
        assert finite.index.equals(oracle.index)
        np.testing.assert_allclose(finite.factor_value, oracle.factor_value, rtol=1e-10, atol=1e-10)
        panel = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet")
        assert digest(panel) == case["price_sha256"]
        days = pd.to_datetime(pd.read_parquet(panel, columns=["trade_date"]).trade_date)
        days = sorted(set(days.loc[days.between(case["start"], case["end"])]))
        # Enumerate the oracle's unavailable runs without using the loader's
        # grouping/shift implementation. Every finite day remains an event.
        boundaries = []
        for factor in FACTORS:
            previous_available = None
            for day in days:
                key = (f"SEC_KR_{symbol}", day, basis, factor)
                available = key in oracle.index
                if not available and previous_available is not False:
                    boundaries.append(dict(zip(KEYS, key)))
                previous_available = available
        expected_missing = canonical(pd.DataFrame(boundaries, columns=KEYS))
        missing = a.loc[a.factor_value.isna()]
        assert missing.index.equals(expected_missing.index), "Abstention boundaries differ from the source oracle"
        assert len(a) == len(finite) + len(missing)
        target = args.output / symbol / basis
        target.mkdir(parents=True)
        actual.to_parquet(target / "prepared.parquet", index=False)
        missing.reset_index().to_parquet(target / "abstentions.parquet", index=False)
        result = dict(symbol=symbol, basis=basis, finite_cells=len(finite), abstention_events=len(missing),
            prepared_rows=len(a), prepared_sha256=digest(target / "prepared.parquet"),
            abstentions_sha256=digest(target / "abstentions.parquet"), oracle_sha256=digest(oracle_path),
            source_version=case["source_version"], status="validated")
        report["cases"].append(result)
        save(args.output / "summary.json", report)
        print({k: result[k] for k in ("symbol", "basis", "finite_cells", "abstention_events", "status")}, flush=True)
    assert input_inventory(args.symbols) == inputs
    assert all(digest(path) == checksum for path, checksum in hashes.items())
    report.update(status="validated_not_published", finite_cells=sum(r["finite_cells"] for r in report["cases"]),
        abstention_events=sum(r["abstention_events"] for r in report["cases"]))
    save(args.output / "summary.json", report)


if __name__ == "__main__":
    main()
