"""Compare every differing native market value with current public market inputs."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import SourceRefreshLock, sha256_file
from engine.core.serving_storage import export_json
from engine.transformers.factors import FactorMarketDataCache, add_daily_market_valuation_factors

KEYS = ["security_id", "trade_date"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Read-only diagnostic output must be in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    preparation = json.loads(args.preparation.read_bytes())
    observations = json.loads(args.observations.read_bytes())
    hashes = {str(p.resolve()): sha256_file(p) for p in (args.preparation, args.observations, Path(__file__),
        ROOT / "engine/transformers/_internal/factor_metrics.py")}
    changes = []
    for month in preparation["months"]:
        if not month["differing_native_values"]:
            continue
        path = args.preparation.parent / month["month"] / "native_value_discrepancies.parquet"
        hashes[str(path.resolve())] = month["hashes"][path.name]
        changes.append(pd.read_parquet(path))
    changes = pd.concat(changes, ignore_index=True)
    assert len(changes) == preparation["totals"]["differing_native_values"]
    full_keys = KEYS + ["factor_id", "financial_basis"]
    if changes.duplicated(full_keys).any():
        raise ValueError("Ambiguous correction keys")
    expected_years = set(changes.trade_date.dt.year)
    original = []
    for entry in observations["years"]:
        if entry["year"] in expected_years:
            path = Path(entry["path"])
            hashes[str(path.resolve())] = entry["sha256"]
            original.append(pd.read_parquet(path))
    original = pd.concat(original, ignore_index=True)
    original.trade_date = pd.to_datetime(original.trade_date).astype("datetime64[ns]")
    changes.trade_date = pd.to_datetime(changes.trade_date).astype("datetime64[ns]")
    oracle = original.loc[:, KEYS + ["shares", "market_cap", "raw_close", "raw_volume"]]
    if oracle.duplicated(KEYS).any():
        raise ValueError("Ambiguous original quote keys")
    codes = sorted(changes.security_id.str.removeprefix("SEC_KR_").unique())
    for path in (DATA_LAKE.silver("krx", "price", "kr_normalized_price.csv"),
                 DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv"),
                 DATA_LAKE.silver("krx", "shares", "historical_sources.json")):
        hashes[str(path.resolve())] = sha256_file(path)
    assert hashes[str(DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv").resolve())] == preparation["default_input_sha256"]
    assert hashes[str(DATA_LAKE.silver("krx", "shares", "historical_sources.json").resolve())] == preparation["registration_sha256"]
    for symbol in codes:
        for extension in ("parquet", "metadata.json"):
            path = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.{extension}")
            hashes[str(path.resolve())] = sha256_file(path) if path.exists() else None

    def verify():
        for name, expected in hashes.items():
            path = Path(name)
            actual = sha256_file(path) if path.exists() else None
            if actual != expected:
                raise ValueError(f"Diagnostic input changed: {name}")

    result = dict(status="running", production_changed=False, coverage_complete=False,
        native_discrepancy_rows=len(changes), securities=len(codes), input_hashes=hashes, results=[])
    report_path = args.output / "summary.json"
    export_json(report_path, result)
    archive = args.output / "implementation"
    archive.mkdir()
    for name in hashes:
        path = Path(name)
        if path.suffix == ".py":
            shutil.copyfile(path, archive / path.name)
    joined_frames = []
    with SourceRefreshLock("kr"):
        verify()
        cache = FactorMarketDataCache(market="kr", start_date=changes.trade_date.min(),
            end_date=changes.trade_date.max(), start_warmup_days=0)
        for index, symbol in enumerate(codes, 1):
            sid = "SEC_KR_" + symbol
            native = changes.loc[changes.security_id.eq(sid)].copy()
            prices = cache.prices(sid, stock_code=symbol)
            shares = cache.shares(sid)
            if prices.empty or shares.empty:
                raise ValueError(f"Default input absent for {sid}")
            # This is the public factor path's backward share join. Only the
            # dates with independently identified original quotes are compared.
            actual = pd.merge_asof(prices.sort_values("trade_date"),
                shares[["trade_date", "shares", "market_cap"]].sort_values("trade_date"),
                on="trade_date", direction="backward")
            actual = actual.loc[actual.trade_date.isin(native.trade_date)]
            actual = add_daily_market_valuation_factors(actual)
            observed = actual[KEYS + ["close", "volume", "shares", "market_cap", "csho", "mcap_mil"]]
            evidence = native.merge(observed, on=KEYS, how="left", validate="many_to_one")
            evidence = evidence.merge(oracle, on=KEYS, how="left", validate="many_to_one", suffixes=("", "_original"))
            evidence["public_market_value"] = np.where(evidence.factor_id.eq("csho"), evidence.csho, evidence.mcap_mil)
            evidence["original_value"] = np.where(evidence.factor_id.eq("csho"), evidence.shares_original, evidence.market_cap_original / 1e6)
            evidence["public_matches_original"] = np.isclose(evidence.public_market_value, evidence.original_value, rtol=1e-12, atol=1e-8)
            evidence["prepared_matches_original"] = np.isclose(evidence.factor_value_expected, evidence.original_value, rtol=1e-12, atol=1e-8)
            evidence["close_matches_original"] = np.isclose(pd.to_numeric(evidence.close), evidence.raw_close, rtol=1e-12, atol=1e-6)
            evidence["shares_match_original"] = evidence.shares.eq(evidence.shares_original)
            target = args.output / "securities" / f"{symbol}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            evidence.to_parquet(target, index=False)
            flags = ["public_matches_original", "prepared_matches_original", "close_matches_original", "shares_match_original"]
            record = dict(symbol=symbol, rows=len(evidence), passed=bool(evidence[flags].all().all()),
                mismatches=int((~evidence[flags].all(axis=1)).sum()), path=str(target), sha256=sha256_file(target))
            result["results"].append(record)
            joined_frames.append(evidence)
            if index % 100 == 0:
                export_json(report_path, result)
                print(dict(complete=index, total=len(codes), mismatches=sum(r["mismatches"] for r in result["results"])), flush=True)
        verify()
    frame = pd.concat(joined_frames, ignore_index=True)
    frame.to_parquet(args.output / "all_discrepancies.parquet", index=False)
    assert len(frame) == len(changes)
    result.update(status="current_public_market_inputs_verified" if all(r["passed"] for r in result["results"]) else "current_public_inputs_require_review",
        verified_rows=int(frame[["public_matches_original", "prepared_matches_original", "close_matches_original", "shares_match_original"]].all(axis=1).sum()),
        output_sha256=sha256_file(args.output / "all_discrepancies.parquet"),
        policy="Current public market-input calculation compared against dated original prices, shares and capitalization. Does not approve issuer identity, lifecycle rights, or other financial factors.")
    export_json(report_path, result)
    print(dict(status=result["status"], verified=result["verified_rows"], total=len(changes)), flush=True)


if __name__ == "__main__":
    main()
