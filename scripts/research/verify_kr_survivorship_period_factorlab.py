"""Read actual FactorLab inputs after the reviewed period-factor publication."""
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
from validate_kr_survivorship_financial_factors import SILVER, digest, save


def main():
    latest = json.loads((SILVER / "kr_v7_period_publication/latest.json").read_text("utf-8"))
    publication = Path(latest["publication_path"])
    assert digest(publication) == latest["sha256"]
    report = json.loads(publication.read_text("utf-8"))
    assert report["status"] == "published_and_verified"
    prepared = pd.read_parquet(publication.parent / "prepared.parquet")
    target = publication.parent / "factorlab"
    assert not target.exists(), "Preserve earlier verification"
    target.mkdir()
    checks = []
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=45)
    try:
        for basis, factor, day in [("quarterly", "npm", "2017-01-03"), ("ttm", "npm", "2017-01-03"),
                                   ("annual", "roe", "2019-04-02")]:
            graph = {"version": 1, "experiment": {"name": "reviewed_period_input", "market": "KR",
                "start_date": day, "end_date": day, "factor_data_mode": "raw",
                "universe": {"size_percentile": {"side": "top", "percent": 70}}},
                "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                    "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
            compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day],
                                                listing_table="security_listing_episodes")
            prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
            assert marker
            settings = "\nSETTINGS max_execution_time=30, max_threads=2"
            values = client.query_df(compiled.query + settings, parameters=compiled.parameters)
            caps = client.query_df(prefix + "\nSELECT * FROM uv_ranked_caps" + settings, parameters=compiled.parameters)
            eligible = caps.loc[caps.size_rank_high <= math.ceil(len(caps) * .7)]
            assert set(values.security_id) <= set(eligible.security_id)
            expected = prepared.loc[pd.to_datetime(prepared.trade_date).eq(day)
                & prepared.financial_basis.eq(basis) & prepared.factor_id.eq(factor)
                & prepared.security_id.isin(eligible.security_id)].set_index("security_id")
            assert len(expected) > 0
            observed = values.set_index("security_id").loc[expected.index]
            np.testing.assert_allclose(observed.value, expected.factor_value, rtol=1e-12, atol=1e-12)
            if factor == "roe":
                assert "SEC_KR_008560" in expected.index
            folder = target / f"{basis}_{factor}_{day}"
            folder.mkdir()
            values.to_parquet(folder / "values.parquet", index=False)
            caps.to_parquet(folder / "capitalization_ranks.parquet", index=False)
            (folder / "query.sql").write_text(compiled.query, encoding="utf-8")
            check = dict(basis=basis, factor=factor, date=day, capitalization_rows=len(caps), top70_count=len(eligible),
                valid_input_rows=len(values), restored_rows=len(expected), restored_ids=expected.index.tolist(),
                query_sha256=sha256(compiled.query.encode()).hexdigest(), graph=graph)
            save(folder / "verification.json", check)
            checks.append(check)
            print({k: v for k, v in check.items() if k not in {"graph", "restored_ids", "query_sha256"}}, flush=True)
    finally:
        client.close()
    save(target / "summary.json", dict(status="verified", checks=checks, verifier_sha256=digest(__file__),
        publication_sha256=digest(publication), coverage_complete=False, recipe_or_holdout_changed=False))


if __name__ == "__main__":
    main()
