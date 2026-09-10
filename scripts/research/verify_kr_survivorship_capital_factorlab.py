"""Check capital availability transitions through actual FactorLab and top-70 universes."""
import argparse
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
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from validate_kr_survivorship_financial_factors import digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    publication = json.loads(args.snapshot_publication.read_text("utf-8"))
    assert publication["snapshots_published"] and publication["status"] == "published_and_verified"
    expected_path = args.snapshot_publication.parent / "expected_asof.parquet"
    assert digest(expected_path) == publication["expected_sha256"]
    expected = pd.read_parquet(expected_path)
    expected["trade_date"] = pd.to_datetime(expected.trade_date)
    listing_path = DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json")
    listing_hash = digest(listing_path)
    episodes = [r for r in json.loads(listing_path.read_text("utf-8"))["rows"] if r["status"] == "confirmed"]
    reviewed_ids = {r["security_id"] for r in episodes}
    source_ids = set(publication["bounds"])
    output = args.output or args.snapshot_publication.parent / "factorlab"
    assert not output.exists(), "Preserve prior consumer verification"
    output.mkdir()
    cases = []
    for basis in ("annual", "quarterly", "ttm"):
        series = expected.loc[expected.security_id.eq("SEC_KR_003410") & expected.financial_basis.eq(basis)
            & expected.factor_id.eq("ar_days")].sort_values("trade_date")
        valid = series.factor_value.notna()
        first_valid = series.loc[valid].trade_date.min()
        first_missing = series.loc[~valid & series.trade_date.gt(first_valid)].trade_date.min()
        assert pd.notna(first_valid) and pd.notna(first_missing)
        cases.extend([(basis, "ar_days", first_valid, "available"), (basis, "ar_days", first_missing, "abstention")])
        recovery = series.loc[valid & series.trade_date.gt(first_missing)].trade_date.min()
        if pd.notna(recovery):
            cases.append((basis, "ar_days", recovery, "recovery"))
        cases.append((basis, "inv_days", pd.Timestamp("2018-07-12"), "period_unit"))
    checks, universes = [], {}
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
    try:
        for basis, factor, day, kind in cases:
            day = day.date().isoformat()
            graph = {"version": 2, "experiment": {"name": "capital_availability", "market": "KR",
                "start_date": day, "end_date": day, "factor_data_mode": "point_in_time_snapshot",
                "universe": {"size_percentile": {"side": "top", "percent": 70}}},
                "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                    "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
            compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day],
                factor_table="fact_daily_factor_snapshot", listing_table="security_listing_episodes")
            prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
            assert marker
            settings = "\nSETTINGS max_execution_time=25,max_threads=2"
            values = client.query_df(compiled.query + settings, parameters=compiled.parameters)
            if values.empty and not len(values.columns):
                values = pd.DataFrame(columns=["trade_date", "security_id", "value", "is_valid", "invalid_reason"])
            if day not in universes:
                caps = client.query_df(prefix + "\nSELECT * FROM uv_ranked_caps" + settings, parameters=compiled.parameters)
                listed = client.query_df(prefix + "\nSELECT * FROM uv_dated_securities" + settings, parameters=compiled.parameters)
                active = {r["security_id"] for r in episodes if r["valid_from"] <= day and (r["valid_until"] is None or day < r["valid_until"])}
                assert set(listed.security_id) & reviewed_ids == active
                assert not set(caps.security_id) & (reviewed_ids - active)
                universes[day] = (caps, active)
                listed.to_parquet(output / f"listing_universe_{day}.parquet", index=False)
            caps, active = universes[day]
            eligible = set(caps.loc[caps.size_rank_high <= math.ceil(len(caps) * .7)].security_id)
            assert "SEC_KR_003410" in eligible, "Transition case is outside top-70 universe; select an eligible observation"
            assert set(values.security_id) <= eligible and not set(values.security_id) & (reviewed_ids - active)
            wanted = expected.loc[expected.trade_date.eq(day) & expected.factor_id.eq(factor)
                & expected.financial_basis.eq(basis) & expected.security_id.isin(eligible)].set_index("security_id")
            finite = wanted.loc[wanted.factor_value.notna()]
            missing = wanted.loc[wanted.factor_value.isna()]
            observed = values.set_index("security_id")
            assert set(observed.index) & source_ids == set(finite.index)
            assert not set(missing.index) & set(observed.index)
            np.testing.assert_allclose(observed.loc[finite.index, "value"].to_numpy(dtype=float, na_value=np.nan),
                finite.factor_value.to_numpy(dtype=float, na_value=np.nan), rtol=1e-12, atol=1e-12)
            raw_node = client.query_df(prefix + "\nSELECT * FROM node_input WHERE security_id IN {reviewed:Array(String)}" + settings,
                parameters=dict(compiled.parameters, reviewed=sorted(wanted.index)))
            raw_node = raw_node.set_index("security_id")
            assert set(raw_node.index) == set(wanted.index)
            assert raw_node.loc[finite.index, "is_valid"].all()
            if len(missing):
                assert not raw_node.loc[missing.index, "is_valid"].any()
                assert raw_node.loc[missing.index, "value"].isna().all()
                assert raw_node.loc[missing.index, "invalid_reason"].eq("source_null").all()
            if kind == "abstention":
                assert "SEC_KR_003410" in missing.index
            else:
                assert "SEC_KR_003410" in finite.index
            folder = output / f"{basis}_{factor}_{day}"
            folder.mkdir()
            values.to_parquet(folder / "values.parquet", index=False)
            caps.to_parquet(folder / "capitalization_ranks.parquet", index=False)
            wanted.reset_index().to_parquet(folder / "expected.parquet", index=False)
            raw_node.reset_index().to_parquet(folder / "input_states.parquet", index=False)
            (folder / "query.sql").write_text(compiled.query, "utf-8")
            save(folder / "graph.json", graph)
            check = dict(basis=basis, factor=factor, date=day, kind=kind, capitalization_rows=len(caps),
                top70_count=math.ceil(len(caps)*.7), reviewed_finite_rows=len(finite), reviewed_missing_rows=len(missing), status="verified")
            checks.append(check)
            save(folder / "verification.json", check)
            print(check, flush=True)
    finally:
        client.close()
    assert digest(listing_path) == listing_hash
    report = dict(status="verified", checks=checks, snapshot_publication_sha256=digest(args.snapshot_publication),
        expected_sha256=digest(expected_path), listing_episodes_sha256=listing_hash, verifier_sha256=digest(__file__),
        coverage_complete=False, recipe_or_holdout_changed=False)
    save(output / "summary.json", report)
    export_json(DATA_LAKE.gold("survivorship", "kr", "capital_factors", "20260910", "snapshot_consumer_verification.json"), report)


if __name__ == "__main__":
    main()
