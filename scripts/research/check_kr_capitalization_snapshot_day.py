"""Compare one actual FactorLab PIT day with hash-pinned original observations."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-verification", type=Path, required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Diagnostic output must remain in Silver")
    verification = json.loads(args.native_verification.read_text("utf-8"))
    if verification["status"] != "verified":
        raise ValueError("Verified original-observation oracle is required")
    month = pd.Timestamp(args.day).strftime("%Y-%m")
    record, = [r for r in verification["months"] if r["month"] == month]
    path = args.native_verification.parent / month / "original_source_oracle.parquet"
    if sha256_file(path) != record["hashes"][path.name]:
        raise ValueError("Original observation oracle changed")
    episodes = DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json")
    if sha256_file(episodes) != verification["listing_episodes_sha256"]:
        raise ValueError("Dated listing universe changed")
    oracle = pd.read_parquet(path)
    expected = oracle.loc[pd.to_datetime(oracle.trade_date).eq(pd.Timestamp(args.day)) & oracle.eligible].copy()
    if expected.empty or expected.security_id.duplicated().any():
        raise ValueError("Expected original day must have unique observations")
    args.output.mkdir(parents=True, exist_ok=False)
    expected.to_parquet(args.output / "original_expected.parquet", index=False)
    expected = expected.set_index("security_id").sort_index()
    results = []
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
    try:
        for basis in ("annual", "quarterly", "ttm"):
            for factor in ("mcap_mil", "csho"):
                name = f"{basis}_{factor}"
                graph = dict(version=2, experiment=dict(name="original_capitalization_snapshot_day", market="KR",
                    start_date=args.day, end_date=args.day, universe=dict(size_percentile=dict(side="top", percent=70))),
                    nodes=[dict(id="input", type="factor_input", version=1,
                        config=dict(factor_id=factor, financial_basis=basis, missing_policy="drop"))],
                    edges=[], outputs=dict(final_node_id="input", evaluation_node_ids=[]))
                compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[args.day],
                    factor_table="fact_daily_factor_snapshot", listing_table="security_listing_episodes")
                actual = client.query_df(compiled.query + "\nSETTINGS max_execution_time=25,max_threads=2", parameters=compiled.parameters)
                actual.to_parquet(args.output / f"{name}.parquet", index=False)
                export_json(args.output / f"{name}_query.json", dict(query=compiled.query, parameters=compiled.parameters, graph=graph))
                observed = actual.set_index("security_id").sort_index()
                if observed.index.has_duplicates:
                    raise ValueError("Actual consumer returned duplicate identities")
                common = observed.index.intersection(expected.index)
                same = np.isclose(observed.loc[common, "value"].to_numpy(float),
                    expected.loc[common, "market_cap" if factor == "mcap_mil" else "shares"].to_numpy(float), rtol=1e-10, atol=1e-8)
                missing, extra = expected.index.difference(observed.index), observed.index.difference(expected.index)
                results.append(dict(basis=basis, factor_id=factor, expected_rows=len(expected), actual_rows=len(observed),
                    missing_ids=missing.tolist(), extra_ids=extra.tolist(), differing_value_ids=common[~same].tolist(),
                    passed=not len(missing) and not len(extra) and bool(same.all()) and bool(observed.is_valid.all())))
    finally:
        client.close()
    report = dict(status="passed" if all(r["passed"] for r in results) else "failed", day=args.day, results=results,
        native_verification_path=str(args.native_verification.resolve()), native_verification_sha256=sha256_file(args.native_verification),
        original_oracle_path=str(path.resolve()), original_oracle_sha256=sha256_file(path),
        implementation_sha256=sha256_file(__file__), factor_table="fact_daily_factor_snapshot", native_or_snapshot_published=False,
        hashes={p.name:sha256_file(p) for p in args.output.iterdir() if p.is_file()})
    export_json(args.output / "summary.json", report)
    print(json.dumps({"status":report["status"], "day":args.day,
        "checks":[{k:r[k] for k in ("basis","factor_id","expected_rows","actual_rows","passed")} for r in results]}), flush=True)
    if report["status"] != "passed":
        raise AssertionError("Actual PIT FactorLab differs from the original dated observations")


if __name__ == "__main__":
    main()
