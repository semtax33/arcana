"""Recheck earlier source scopes after the remaining capitalization publication."""
from datetime import datetime, timezone
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
from verify_kr_historical_capitalization_snapshots_factorlab import canonical


def main():
    base = DATA_LAKE.silver("survivorship", "financial_research")
    output = base / "kr_previous_capitalization_scopes_recheck_20260911"
    output.mkdir(exist_ok=False)
    shutil.copy2(__file__, output / Path(__file__).name)
    pins = {}

    def check(path, expected=None):
        path = Path(path).resolve()
        actual = sha256_file(path)
        if expected is not None and actual != expected:
            raise ValueError(f"Changed source: {path}")
        pins[str(path)] = actual
        return actual

    def read(path, expected=None):
        check(path, expected)
        return json.loads(Path(path).read_bytes())

    latest_path = base / "kr_remaining_capitalization_snapshot_publication_20260911/publication.json"
    latest = read(latest_path)
    assert latest["status"] == "published_and_verified" and latest["snapshots_published"]
    for name, digest in latest["dependencies"].items():
        path = (ROOT / name).resolve()
        if digest is None:
            assert not path.exists(), path
            pins[str(path)] = None
        else:
            check(path, digest)
    remaining = read(base / "kr_remaining_full_snapshot_factorlab_20260911/summary.json")
    assert remaining["status"] == "verified"
    assert remaining["snapshot_publication_sha256"] == check(latest_path)
    episodes = DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json")
    check(episodes, remaining["listing_episodes_sha256"])
    for name in ("api/repository/factor_lab_query.py", "api/repository/listing_history.py",
                 "scripts/research/verify_kr_historical_capitalization_snapshots_factorlab.py"):
        check(ROOT / name)
    sources = []
    for scope in ("2013_2015", "2024"):
        gold = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", scope,
                              "snapshot_consumer_verification.json")
        previous = read(gold)
        assert previous["status"] == "verified"
        assert previous["listing_episodes_sha256"] == check(episodes)
        native_path = Path(previous["native_verification_path"])
        native = read(native_path, previous["native_verification_sha256"])
        assert native["status"] == "verified"
        read(native["native_publication_path"], native["native_publication_sha256"])
        records = {record["month"]: record for record in native["months"]}
        sources.append((scope, previous, native_path, records))
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
        reason="Later restored native observations may supersede carry values exported by earlier source scopes.",
        latest_snapshot_publication_path=str(latest_path),
        latest_snapshot_publication_sha256=check(latest_path), checks=[],
        production_database_changed=False, coverage_complete=False)
    target = output / "summary.json"
    export_json(target, report)
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
    try:
        for scope, previous, native_path, records in sources:
            for case in previous["checks"]:
                month, basis, factor, days = (case[key] for key in ("month", "basis", "factor_id", "dates"))
                path = native_path.parent / month / "original_source_oracle.parquet"
                check(path, records[month]["hashes"][path.name])
                oracle = pd.read_parquet(path)
                oracle.trade_date = pd.to_datetime(oracle.trade_date)
                wanted = oracle.loc[oracle.eligible & oracle.trade_date.isin(pd.to_datetime(days))].copy()
                wanted["expected_value"] = wanted.market_cap if factor == "mcap_mil" else wanted.shares
                expected = canonical(wanted)
                graph = {"version": 2, "experiment": {"name": "current_prior_scope_recheck", "market": "KR",
                    "start_date": days[0], "end_date": days[-1],
                    "universe": {"size_percentile": {"side": "top", "percent": 70}}},
                    "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                        "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                    "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
                for kind, table in (("native", "fact_daily_factors"), ("snapshot", "fact_daily_factor_snapshot")):
                    folder = output / f"{month}_{kind}_{basis}_{factor}"
                    folder.mkdir()
                    compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=days,
                        factor_table=table, listing_table="security_listing_episodes")
                    actual = client.query_df(compiled.query + "\nSETTINGS max_execution_time=30,max_threads=2",
                                             parameters=compiled.parameters)
                    observed = canonical(actual)
                    actual.to_parquet(folder / "actual.parquet", index=False)
                    wanted.to_parquet(folder / "original_observation_expected.parquet", index=False)
                    (folder / "query.sql").write_text(compiled.query, "utf-8")
                    export_json(folder / "graph.json", graph)
                    if not observed.index.equals(expected.index):
                        export_json(folder / "discrepancy.json", dict(expected_rows=len(expected), actual_rows=len(observed),
                            missing_keys=len(expected.index.difference(observed.index)),
                            extra_keys=len(observed.index.difference(expected.index))))
                        raise ValueError("Current prior-scope membership differs from original observations")
                    assert observed.is_valid.all()
                    np.testing.assert_allclose(observed.value.to_numpy(float), expected.expected_value.to_numpy(float),
                                               rtol=1e-10, atol=1e-8)
                    report["checks"].append(dict(scope=scope, month=month, kind=kind, basis=basis, factor_id=factor,
                        days=len(days), rows=len(actual), status="verified", folder=folder.name,
                        hashes={p.name: sha256_file(p) for p in folder.iterdir() if p.is_file()}))
                    export_json(target, report)
                    print(month, kind, basis, factor, len(days), "verified", flush=True)
        for path, digest in pins.items():
            if digest is None:
                assert not Path(path).exists(), path
            else:
                assert sha256_file(path) == digest, path
        old_days = sum(case["days"] for case in report["checks"]
            if case["kind"] == "snapshot" and case["basis"] == "annual" and case["factor_id"] == "mcap_mil")
        assert old_days == sum(source[1]["annual_mcap_verified_days"] for source in sources)
        report.update(status="verified", prior_scope_trading_days=old_days,
            remaining_scope_trading_days=remaining["annual_mcap_verified_days"],
            combined_annual_mcap_trading_days=old_days + remaining["annual_mcap_verified_days"],
            evidence_pins=pins, completed_at=datetime.now(timezone.utc).isoformat(),
            policy="Annual mcap checked on every source trading day; other two-factor/three-basis cases retain the earlier sampled dates. Updated full-scope Gold exports remain a separate step.")
        export_json(target, report)
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error), evidence_pins=pins)
        export_json(target, report)
        raise
    finally:
        client.close()
    print(report["status"], report["combined_annual_mcap_trading_days"], flush=True)


if __name__ == "__main__":
    main()
