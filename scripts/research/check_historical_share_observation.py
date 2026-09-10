"""Compare an actual public-calculator audit result with one dated original quote."""
import argparse
import json
from pathlib import Path
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json, export_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", type=Path)
    mode.add_argument("--live-calculation", action="store_true")
    parser.add_argument("--symbol", default="010620")
    parser.add_argument("--date", default="2024-01-02")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Evidence must be in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    if args.audit:
        summary_path = args.audit / "summary.json"
        audit = json.loads(summary_path.read_text("utf-8"))
        checkpoint = next(r for r in audit["results"] if r["symbol"] == args.symbol)
        case_path = args.audit / "securities" / args.symbol / "summary.json"
        assert sha256_file(case_path) == checkpoint["summary_sha256"]
        case = json.loads(case_path.read_text("utf-8"))
        assert case["status"] == "compared"
    else:
        from engine.transformers.factors import FactorMarketDataCache, create_stock_factor_dataframe
        warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
        warnings.filterwarnings("ignore", category=FutureWarning, message="Downcasting behavior.*")
        cache = FactorMarketDataCache(market="kr", end_date=args.date)
        case = dict(bases={})
        for basis in ("annual", "quarterly", "ttm"):
            frame = create_stock_factor_dataframe(args.symbol, market="kr", financial_basis=basis,
                start_date=args.date, end_date=args.date, market_data_cache=cache,
                use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
            target = args.output / f"{basis}_actual.parquet"
            frame.to_parquet(target, index=False)
            case["bases"][basis] = dict(delta_path=str(target.resolve()), delta_sha256=sha256_file(target))
        case_path = args.output / "actual_calculation.json"
        export_json(case_path, dict(**case, default_input_sha256=sha256_file(DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv"))))
    source = DATA_LAKE.bronze("marcap", "data", f"marcap-{args.date[:4]}.parquet")
    raw = pd.read_parquet(source, columns=["Code", "Date", "Stocks", "Close", "Volume", "Marcap"])
    row = raw.loc[raw.Code.eq(args.symbol) & raw.Date.eq(args.date)]
    assert len(row) == 1 and row.Stocks.iloc[0] > 0
    original = export_frame(args.output / "original_observation.parquet", row)
    expected = dict(csho=float(row.Stocks.iloc[0]), mcap_mil=float(row.Marcap.iloc[0]) / 1_000_000)
    results = []
    for basis, record in case["bases"].items():
        path = Path(record["delta_path"])
        assert sha256_file(path) == record["delta_sha256"]
        frame = pd.read_parquet(path)
        for factor, value in expected.items():
            if args.live_calculation:
                actual = frame.loc[frame.trade_date.eq(args.date)]
                assert len(actual) == 1
                observed = float(actual[factor].iloc[0])
            else:
                actual = frame.loc[frame.factor_id.eq(factor) & frame.trade_date.eq(args.date)]
                assert len(actual) == 1, "The counterfactual delta must contain the diagnostic observation"
                observed = float(actual.after_value.iloc[0])
            results.append(dict(basis=basis, factor_id=factor, expected=value, actual=observed,
                passed=bool(np.isclose(observed, value, rtol=1e-12, atol=1e-8)),
                calculation_path=str(path), calculation_sha256=record["delta_sha256"]))
    report = dict(status="passed" if all(r["passed"] for r in results) else "failed",
        symbol=args.symbol, trade_date=args.date, source_path=str(source.resolve()), source_sha256=sha256_file(source),
        original_observation=original, audit_case_path=str(case_path.resolve()), audit_case_sha256=sha256_file(case_path),
        public_calculation="create_stock_factor_dataframe; same prices and actual restored-share precedence as the current default pipeline",
        results=results, native_published=False, policy="Counterfactual changes alone do not approve a dated input or publication.")
    export_json(args.output / "summary.json", report)
    print(json.dumps({k:report[k] for k in ("status", "symbol", "trade_date")}), flush=True)
    print(json.dumps([{k:r[k] for k in ("basis", "factor_id", "expected", "actual", "passed")} for r in results]), flush=True)
    assert report["status"] == "passed", "Historical share restoration produced values that differ from the dated original"


if __name__ == "__main__":
    main()
