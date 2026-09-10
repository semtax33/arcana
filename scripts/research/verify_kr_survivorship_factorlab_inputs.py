"""Verify historical FactorLab inputs without changing any strategy recipe."""
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
from engine.core.paths import DATA_LAKE
from validate_kr_survivorship_financial_factors import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    args = parser.parse_args()
    base = DATA_LAKE.silver("survivorship", "financial_research", "kr_v7_financial_publication")
    target = base / f"factorlab_{args.phase}"
    assert not target.exists(), "Preserve the existing FactorLab verification"
    graph = {"version": 1, "experiment": {"name": "reviewed_historical_npm_input", "market": "KR",
        "start_date": "2017-01-03", "end_date": "2017-01-03", "factor_data_mode": "raw",
        "universe": {"size_percentile": {"side": "top", "percent": 70}}},
        "nodes": [{"id": "npm_input", "type": "factor_input", "version": 1,
                   "config": {"factor_id": "npm", "financial_basis": "annual", "missing_policy": "drop"}}],
        "edges": [], "outputs": {"final_node_id": "npm_input", "evaluation_node_ids": []}}
    compiled = compile_factor_lab_graph(graph, known_factor_ids={"npm"}, trade_dates=["2017-01-03"],
                                        listing_table="security_listing_episodes")
    prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_npm_input")
    assert marker, "Inspect the current compiler output before changing the verification"
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=45)
    try:
        values = client.query_df(compiled.query + "\nSETTINGS max_execution_time=30, max_threads=2", parameters=compiled.parameters)
        caps = client.query_df(prefix + "\nSELECT * FROM uv_ranked_caps\nSETTINGS max_execution_time=30, max_threads=2", parameters=compiled.parameters)
    finally:
        client.close()
    eligible = caps.loc[caps.size_rank_high <= math.ceil(len(caps)*0.7)]
    assert set(values.security_id) <= set(eligible.security_id)
    target.mkdir(parents=True)
    values.to_parquet(target / "values.parquet", index=False)
    caps.to_parquet(target / "capitalization_ranks.parquet", index=False)
    (target / "query.sql").write_text(compiled.query, encoding="utf-8")
    result = dict(phase=args.phase, graph=graph, query_sha256=sha256(compiled.query.encode()).hexdigest(),
                  capitalization_rows=len(caps), top70_before_factor_missingness=len(eligible), valid_npm_rows=len(values),
                  coverage_complete=False, recipe_or_holdout_changed=False)
    if args.phase == "after":
        publication = json.loads((base / "publication.json").read_text("utf-8"))
        assert publication["status"] == "published_and_verified"
        expected = pd.read_parquet(base / "prepared_factors.parquet")
        expected = expected.loc[pd.to_datetime(expected.trade_date).eq("2017-01-03") & expected.factor_id.eq("npm")
                                & expected.security_id.isin(eligible.security_id)].set_index("security_id")
        observed = values.set_index("security_id").loc[expected.index]
        np.testing.assert_allclose(observed.value, expected.factor_value, rtol=1e-12, atol=1e-12)
        assert len(expected) > 0
        result.update(restored_input_count=len(expected), restored_security_ids=expected.index.tolist())
    save(target / "summary.json", result)
    print({k: v for k, v in result.items() if k != "graph"}, flush=True)


if __name__ == "__main__":
    main()
