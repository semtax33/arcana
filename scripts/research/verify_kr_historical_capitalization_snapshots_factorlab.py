"""Verify restored PIT capitalization inputs through the actual FactorLab compiler."""
import argparse
import json
from pathlib import Path
import shutil
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

KEYS = ["trade_date", "security_id"]


def canonical(frame):
    frame = frame.copy()
    frame.trade_date = pd.to_datetime(frame.trade_date).astype("datetime64[ns]")
    frame.security_id = frame.security_id.astype(object)
    assert not frame.duplicated(KEYS).any()
    return frame.set_index(KEYS).sort_index()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-publication", type=Path, required=True)
    parser.add_argument("--native-verification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Consumer verification belongs in silver")
    snapshot = json.loads(args.snapshot_publication.read_text("utf-8"))
    native = json.loads(args.native_verification.read_text("utf-8"))
    assert snapshot["snapshots_published"] and snapshot["status"] == "published_and_verified"
    expected_days = sum(row["days"] for row in native["months"])
    assert native["status"] == "verified" and native["verified_days"] == expected_days and expected_days > 0
    assert native["native_publication_sha256"] == snapshot["native_publication_sha256"]
    episodes = DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json")
    assert sha256_file(episodes) == native["listing_episodes_sha256"]
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, args.output / Path(__file__).name)
    checks = []
    months = [row["month"] for row in native["months"]]
    years = sorted({int(month[:4]) for month in months})
    scope_name = "_".join(map(str, years))
    if len(years) > 1 and years == list(range(years[0], years[-1] + 1)):
        scope_name = f"{years[0]}_{years[-1]}"
    sample_months = {months[0], months[len(months) // 2], months[-1]}
    legacy_samples = {"2013-03": "2013-03-28", "2014-04": "2014-04-01", "2015-04": "2015-04-01"}
    report = dict(status="running", coverage_complete=False,
        snapshot_publication_path=str(args.snapshot_publication.resolve()),
        snapshot_publication_sha256=sha256_file(args.snapshot_publication),
        native_verification_path=str(args.native_verification.resolve()),
        native_verification_sha256=sha256_file(args.native_verification),
        implementation_sha256=sha256_file(__file__), listing_episodes_sha256=sha256_file(episodes),
        factor_table="fact_daily_factor_snapshot", capitalization_table="fact_daily_factors", checks=checks,
        source_years=years, expected_trading_days=expected_days,
        policy=f"Top70 security count precedes missingness. All {expected_days} source trading days annual mcap and selected dates for other two-factor/three-basis combinations. Historical listing coverage is still incomplete.")
    report_path = args.output / "summary.json"
    export_json(report_path, report)
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
    try:
        for record in native["months"]:
            month = record["month"]
            path = args.native_verification.parent / month / "original_source_oracle.parquet"
            assert sha256_file(path) == record["hashes"][path.name]
            oracle = pd.read_parquet(path)
            oracle.trade_date = pd.to_datetime(oracle.trade_date)
            eligible = oracle.loc[oracle.eligible].copy()
            all_days = sorted(eligible.trade_date.dt.strftime("%Y-%m-%d").unique())
            cases = [("annual", "mcap_mil", all_days)]
            sample = legacy_samples.get(month) if years == [2013, 2014, 2015] else (all_days[0] if month in sample_months else None)
            if sample is not None:
                cases += [(basis, factor, [sample]) for basis in ("annual", "quarterly", "ttm")
                    for factor in ("mcap_mil", "csho") if (basis, factor) != ("annual", "mcap_mil")]
            for basis, factor, days in cases:
                folder = args.output / f"{month}_{basis}_{factor}"
                folder.mkdir()
                graph = {"version": 2, "experiment": {"name": "restored_capitalization_snapshot_check", "market": "KR",
                    "start_date": days[0], "end_date": days[-1], "universe": {"size_percentile": {"side": "top", "percent": 70}}},
                    "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                        "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                    "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
                compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=days,
                    factor_table="fact_daily_factor_snapshot", listing_table="security_listing_episodes")
                assert "source_after_snapshot" in compiled.query
                actual = client.query_df(compiled.query + "\nSETTINGS max_execution_time=30,max_threads=2", parameters=compiled.parameters)
                wanted = eligible.loc[eligible.trade_date.isin(pd.to_datetime(days))].copy()
                wanted["expected_value"] = wanted.market_cap if factor == "mcap_mil" else wanted.shares
                observed, expected = canonical(actual), canonical(wanted)
                actual.to_parquet(folder / "actual.parquet", index=False)
                wanted.to_parquet(folder / "original_observation_expected.parquet", index=False)
                if not observed.index.equals(expected.index):
                    export_json(folder / "discrepancy.json", dict(expected_rows=len(expected), actual_rows=len(observed),
                        missing_keys=len(expected.index.difference(observed.index)), extra_keys=len(observed.index.difference(expected.index))))
                    raise ValueError("PIT FactorLab membership differs from original observations")
                assert observed.is_valid.all()
                np.testing.assert_allclose(observed.value.to_numpy(float), expected.expected_value.to_numpy(float), rtol=1e-10, atol=1e-8)
                (folder / "query.sql").write_text(compiled.query, "utf-8")
                export_json(folder / "graph.json", graph)
                check = dict(month=month, basis=basis, factor_id=factor, dates=days, rows=len(actual), status="verified",
                    folder=folder.name, hashes={p.name: sha256_file(p) for p in folder.iterdir() if p.is_file()})
                checks.append(check)
                export_json(report_path, report)
                print(month, basis, factor, len(days), len(actual), "verified", flush=True)
        assert sha256_file(episodes) == report["listing_episodes_sha256"]
        assert sha256_file(args.snapshot_publication) == report["snapshot_publication_sha256"]
        assert sha256_file(args.native_verification) == report["native_verification_sha256"]
        for record in native["months"]:
            path = args.native_verification.parent / record["month"] / "original_source_oracle.parquet"
            assert sha256_file(path) == record["hashes"][path.name]
        report.update(status="verified", annual_mcap_verified_days=sum(len(r["dates"]) for r in checks if r["basis"] == "annual" and r["factor_id"] == "mcap_mil"),
            additional_basis_factor_checks=len(checks) - len(months), recipe_or_holdout_changed=False)
        assert report["annual_mcap_verified_days"] == expected_days
        export_json(report_path, report)
    except BaseException as error:
        report.update(status="failed_check_case_artifacts", error_type=type(error).__name__, error=str(error))
        export_json(report_path, report)
        raise
    finally:
        client.close()
    gold_path = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", scope_name, "snapshot_consumer_verification.json")
    if gold_path.exists():
        shutil.copy2(gold_path, args.output / "gold_consumer_verification_before.json")
    export_json(gold_path,
        {**report, "verification_path": str(report_path.resolve()), "verification_sha256": sha256_file(report_path)})
    print(report["status"], report["annual_mcap_verified_days"], report["additional_basis_factor_checks"], flush=True)


if __name__ == "__main__":
    main()
