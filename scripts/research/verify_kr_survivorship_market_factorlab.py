"""Verify published market snapshots through the public FactorLab compiler.

These are historical input checks, not strategy or holdout evaluations.
"""
import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from validate_kr_survivorship_financial_factors import digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-publication", required=True, type=Path)
    args = parser.parse_args()
    publication = json.loads(args.snapshot_publication.read_text("utf-8"))
    assert publication["status"] == "published_and_verified"
    assert publication["snapshots_published"]
    expected_path = args.snapshot_publication.parent / "expected_asof.parquet"
    assert digest(expected_path) == publication["expected_sha256"]
    expected = pd.read_parquet(expected_path)
    listing_path = ROOT / "data-lake/silver/survivorship/kr/listing_episodes.json"
    listing_sha = digest(listing_path)
    episodes = json.loads(listing_path.read_text("utf-8"))["rows"]
    confirmed = [row for row in episodes if row["status"] == "confirmed"]
    reviewed_ids = {row["security_id"] for row in confirmed}
    target = args.snapshot_publication.parent / "factorlab"
    assert not target.exists(), "Preserve the earlier consumer verification"
    target.mkdir()
    cases = [("na_20", "2018-07-12"), ("bb_percent_b", "2019-11-04"),
             ("beta", "2019-04-02"), ("cost_of_debt_after_tax", "2019-04-02"),
             ("na_20", "2021-01-04")]
    checks = []
    date_universes = {}
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=45)
    try:
        for basis in ("annual", "quarterly", "ttm"):
            for factor, day in cases:
                graph = {"version": 2, "experiment": {"name": "reviewed_market_snapshot_input", "market": "KR",
                    "start_date": day, "end_date": day, "factor_data_mode": "point_in_time_snapshot",
                    "universe": {"size_percentile": {"side": "top", "percent": 70}}},
                    "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                        "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                    "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
                compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day],
                    factor_table="fact_daily_factor_snapshot", listing_table="security_listing_episodes")
                prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
                assert marker
                settings = "\nSETTINGS max_execution_time=30, max_threads=2"
                values = client.query_df(compiled.query + settings, parameters=compiled.parameters)
                if day not in date_universes:
                    caps = client.query_df(prefix + "\nSELECT * FROM uv_ranked_caps" + settings, parameters=compiled.parameters)
                    listed = client.query_df(prefix + "\nSELECT * FROM uv_dated_securities" + settings, parameters=compiled.parameters)
                    active = {row["security_id"] for row in confirmed if row["valid_from"] <= day
                        and (row["valid_until"] is None or day < row["valid_until"])}
                    assert set(listed.security_id) & reviewed_ids == active
                    assert not set(caps.security_id) & (reviewed_ids - active)
                    listed.to_parquet(target / f"listing_universe_{day}.parquet", index=False)
                    date_universes[day] = (caps, active)
                caps, active = date_universes[day]
                eligible = caps.loc[caps.size_rank_high <= math.ceil(len(caps) * .7)]
                assert set(values.security_id) <= set(eligible.security_id)
                assert not set(values.security_id) & (reviewed_ids - active)
                wanted = expected.loc[pd.to_datetime(expected.trade_date).eq(day)
                    & expected.financial_basis.eq(basis) & expected.factor_id.eq(factor)
                    & expected.security_id.isin(eligible.security_id)].set_index("security_id")
                assert len(wanted) > 0
                observed = values.set_index("security_id").loc[wanted.index]
                assert observed.is_valid.all()
                np.testing.assert_allclose(observed.value, wanted.factor_value, rtol=1e-12, atol=1e-12)
                folder = target / f"{basis}_{factor}_{day}"
                folder.mkdir()
                values.to_parquet(folder / "values.parquet", index=False)
                caps.to_parquet(folder / "capitalization_ranks.parquet", index=False)
                wanted.reset_index().to_parquet(folder / "expected_reviewed_inputs.parquet", index=False)
                (folder / "query.sql").write_text(compiled.query, "utf-8")
                check = dict(basis=basis, factor=factor, date=day, capitalization_rows=len(caps),
                    top70_count=len(eligible), valid_input_rows=int(values.is_valid.sum()),
                    confirmed_active_ids=sorted(active), confirmed_inactive_ids=sorted(reviewed_ids - active),
                    restored_rows=len(wanted), restored_ids=wanted.index.tolist(),
                    query_sha256=sha256(compiled.query.encode()).hexdigest(), graph=graph)
                save(folder / "verification.json", check)
                checks.append(check)
                print({k: v for k, v in check.items() if k not in {"graph", "restored_ids", "query_sha256", "confirmed_active_ids", "confirmed_inactive_ids"}}, flush=True)
    finally:
        client.close()
    assert digest(listing_path) == listing_sha
    save(target / "summary.json", dict(status="verified", checks=checks, verifier_sha256=digest(__file__),
        publication_sha256=digest(args.snapshot_publication), expected_sha256=digest(expected_path),
        listing_episodes_sha256=listing_sha,
        coverage_complete=False, recipe_or_holdout_changed=False))


if __name__ == "__main__":
    main()
