"""Compare the public turnover calculation with a pinned original-source case.

Read-only diagnostic: this neither publishes factors nor clears rebuild journals.
The actual FactorLab RED case is required before this calculation probe runs.
"""
import argparse
import json
import math
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
from engine.transformers.factors import FactorMarketDataCache, create_stock_factor_dataframe
from audit_kr_historical_share_dependencies import ReadEvidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--red", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Diagnostic evidence must stay in silver")
    red = json.loads(args.red.read_text("utf-8"))
    assert red["status"] == "failed" and red["security_id"] == "SEC_KR_005930"
    assert red["trade_date"] == "2014-04-01" and red["factor_id"] == "adturn_pct_12_1"
    assert len(red["results"]) == 6 and all(r["actual"] is None for r in red["results"])
    window_path = args.red.parent / "original_window.parquet"
    original = pd.read_parquet(window_path)
    original.Date = pd.to_datetime(original.Date)
    assert len(original) == red["source_window_observations"] == 231
    expected = math.fsum(float(r.Volume) / float(r.Stocks) * 100 for r in original.itertuples()) / 231
    assert expected == red["expected"]
    pins = {str(p.resolve()): sha256_file(p) for p in (args.red, window_path, Path(__file__),
        ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "scripts/research/audit_kr_historical_share_dependencies.py")}
    for source in red["sources"]:
        path = Path(source["path"])
        assert sha256_file(path) == source["sha256"]
        pins[str(path.resolve())] = source["sha256"]
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = ReadEvidence(args.output)
    sys.addaudithook(evidence.hook)
    report = dict(status="running", production_changed=False, factorlab_repaired=False,
        red_path=str(args.red.resolve()), red_sha256=sha256_file(args.red),
        original_window_sha256=sha256_file(window_path), expected=expected,
        security_id=red["security_id"], trade_date=red["trade_date"], bases=[], input_hashes=pins)
    export_json(args.output / "summary.json", report)
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        cache = FactorMarketDataCache(market="kr", start_date="2013-01-01", end_date=red["trade_date"], start_warmup_days=0)
        prices = cache.prices(red["security_id"], stock_code="005930")
        shares = cache.shares(red["security_id"])
        joined = pd.merge_asof(prices.sort_values("trade_date"),
            shares[["trade_date", "shares"]].sort_values("trade_date"), on="trade_date", direction="backward")
        observed_window = joined.loc[joined.trade_date.le(red["trade_date"])].iloc[-252:-21].copy()
        pd.testing.assert_index_equal(pd.DatetimeIndex(observed_window.trade_date), pd.DatetimeIndex(original.Date), check_names=False)
        assert observed_window.volume.tolist() == original.Volume.tolist()
        assert observed_window.shares.tolist() == original.Stocks.tolist()
        observed_window.to_parquet(args.output / "public_input_window.parquet", index=False)
        report["window_matches_original"] = True
        for basis in ("annual", "quarterly", "ttm"):
            frame = create_stock_factor_dataframe("005930", start_date=red["trade_date"], end_date=red["trade_date"],
                financial_basis=basis, market="kr", market_data_cache=cache,
                use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
            target = frame.loc[frame.security_id.eq(red["security_id"]) & frame.trade_date.eq(pd.Timestamp(red["trade_date"]))]
            assert len(target) == 1
            actual = float(target.iloc[0][red["factor_id"]])
            frame.to_parquet(args.output / f"{basis}_public_result.parquet", index=False)
            result = dict(basis=basis, actual=actual, expected=expected,
                matches_original=math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0))
            report["bases"].append(result)
            export_json(args.output / "summary.json", report)
            print(json.dumps(result), flush=True)
        assert all(r["matches_original"] for r in report["bases"])
        for path, stat in evidence.snapshot().items():
            current = Path(path).stat()
            assert dict(size=current.st_size, mtime_ns=current.st_mtime_ns) == stat, path
            pins[path] = sha256_file(path)
        for path, digest in pins.items():
            assert sha256_file(path) == digest, path
        report.update(status="public_calculation_matches_original_republication_pending",
            conclusion="Restored dated shares and raw-volume window are correct in all three public calculation bases. Native and PIT FactorLab remain missing until the dependent factor is rebuilt.",
            artifacts={p.name: sha256_file(p) for p in args.output.glob("*.parquet")})
    except BaseException as error:
        report.update(status="failed_check_evidence", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        evidence.enabled = False
        implementation = args.output / "implementation"
        implementation.mkdir()
        for path in pins:
            if Path(path).suffix == ".py":
                shutil.copy2(path, implementation / Path(path).name)
        export_json(args.output / "summary.json", report)
    print(report["status"], flush=True)


if __name__ == "__main__":
    main()
