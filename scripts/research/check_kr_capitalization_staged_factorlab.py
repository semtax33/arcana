"""Check sampled restored observations in actual FactorLab before native publication."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from publish_kr_survivorship_capital_factors import COLUMNS, clickhouse_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--observation-sources", type=Path, required=True)
    parser.add_argument("--months", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Isolated consumer evidence must stay in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    preparation_path = args.preparation / "summary.json"
    preparation = json.loads(preparation_path.read_bytes())
    review = json.loads(args.review.read_bytes())
    index = json.loads(args.observation_sources.read_bytes())
    if review["status"] != "native_dispositions_verified" or review["preparation_sha256"] != sha256_file(preparation_path):
        raise ValueError("The exact native dispositions must be verified first")
    if preparation["source_audits_sha256"].get(str(args.observation_sources.resolve())) != sha256_file(args.observation_sources):
        raise ValueError("Original observation index differs from native preparation")
    evidence = {str(p.resolve()): sha256_file(p) for p in [preparation_path, args.review, args.observation_sources]}
    outside_path = args.review.parent / "preserved_unpriced_native.parquet"
    if sha256_file(outside_path) != review["artifacts"]["preserved_unpriced_native"]["sha256"]:
        raise ValueError("Reviewed unpriced observations changed")
    evidence[str(outside_path.resolve())] = sha256_file(outside_path)
    outside = pd.read_parquet(outside_path)
    outside.trade_date = pd.to_datetime(outside.trade_date)
    months = {r["month"]: r for r in preparation["months"]}
    years = {r["year"]: r for r in index["years"]}
    checks, records = [], []
    report = dict(status="running", coverage_complete=False, production_changed=False, checks=checks, days=records,
        input_sha256=evidence, scope="Sampled dates in isolated native staging with actual production dated listing membership; incomplete issuer coverage remains explicit.")
    export_json(args.output / "summary.json", report)
    database = "arcana_test_capitalization_consumer_" + uuid4().hex
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=35)
    created = False
    try:
        client.command(f"CREATE DATABASE {database}")
        created = True
        client.command(f"CREATE TABLE {database}.source AS fact_daily_factors ENGINE=Memory")
        for month_name in args.months:
            month = pd.Period(month_name, freq="M")
            folder = args.output / month_name
            folder.mkdir()
            prepared_path = args.preparation / month_name / "prepared.parquet"
            if sha256_file(prepared_path) != months[month_name]["hashes"][prepared_path.name]:
                raise ValueError("Prepared observations changed")
            evidence[str(prepared_path.resolve())] = sha256_file(prepared_path)
            prepared = pd.read_parquet(prepared_path)
            prepared.trade_date = pd.to_datetime(prepared.trade_date)
            day = prepared.trade_date.min()
            if month_name == "2026-05":
                day = pd.Timestamp("2026-05-26")
            if month_name == "2026-09":
                day = pd.Timestamp("2026-09-04")
            staged = pd.concat([prepared.loc[prepared.trade_date.eq(day)], outside.loc[outside.trade_date.eq(day)]], ignore_index=True)
            if staged.empty or staged.duplicated(["security_id", "trade_date", "factor_id", "financial_basis"]).any():
                raise ValueError("Staged native day must have unique keys")
            staged.to_parquet(folder / "staged_native.parquet", index=False)
            source_path = Path(years[month.year]["path"])
            if sha256_file(source_path) != years[month.year]["sha256"]:
                raise ValueError("Dated original source changed")
            evidence[str(source_path)] = years[month.year]["sha256"]
            source = pd.read_parquet(source_path)
            source = source.loc[pd.to_datetime(source.trade_date).eq(day)].copy()
            client.command(f"TRUNCATE TABLE {database}.source")
            client.insert_df(f"{database}.source", clickhouse_rows(staged[COLUMNS]), column_names=COLUMNS)
            day_text = day.date().isoformat()
            oracle = None
            for basis in ("annual", "quarterly", "ttm"):
                for factor in ("mcap_mil", "csho"):
                    name = f"{basis}_{factor}"
                    graph = dict(version=2, experiment=dict(name="staged_capitalization_consumer", market="KR",
                        start_date=day_text, end_date=day_text, universe=dict(size_percentile=dict(side="top", percent=70))),
                        nodes=[dict(id="input", type="factor_input", version=1,
                            config=dict(factor_id=factor, financial_basis=basis, missing_policy="drop"))],
                        edges=[], outputs=dict(final_node_id="input", evaluation_node_ids=[]))
                    compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day_text],
                        factor_table=f"{database}.source", cap_table=f"{database}.source", listing_table="security_listing_episodes")
                    settings = "\nSETTINGS max_execution_time=25,max_threads=2"
                    actual = client.query_df(compiled.query+settings, parameters=compiled.parameters)
                    actual.security_id = actual.security_id.astype(object)
                    actual.to_parquet(folder / f"{name}.parquet", index=False)
                    export_json(folder / f"{name}.json", dict(graph=graph, query=compiled.query, parameters=compiled.parameters))
                    if oracle is None:
                        prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
                        if not marker:
                            raise ValueError("Unexpected public compiler output")
                        listed = client.query_df(prefix+"\nSELECT * FROM uv_dated_securities"+settings, parameters=compiled.parameters)
                        caps = client.query_df(prefix+"\nSELECT * FROM uv_ranked_caps"+settings, parameters=compiled.parameters)
                        caps.security_id = caps.security_id.astype(object)
                        listed.to_parquet(folder / "dated_universe.parquet", index=False)
                        caps.to_parquet(folder / "ranked_caps.parquet", index=False)
                        oracle = source.loc[source.security_id.isin(listed.security_id)].copy()
                        oracle["market_cap"] = oracle.market_cap/1e6
                        oracle = oracle.sort_values(["market_cap", "security_id"], ascending=[False, True])
                        oracle["rank"] = np.arange(1, len(oracle)+1)
                        oracle["eligible"] = oracle["rank"].le(int(np.ceil(len(oracle)*.7)))
                        oracle.to_parquet(folder / "original_oracle.parquet", index=False)
                        observed_caps = caps.set_index("security_id").sort_index()
                        all_expected = oracle.set_index("security_id").sort_index()
                        pd.testing.assert_index_equal(observed_caps.index, all_expected.index, exact=False)
                        np.testing.assert_allclose(observed_caps.market_cap, all_expected.market_cap, rtol=1e-10, atol=1e-8)
                        np.testing.assert_array_equal(observed_caps.size_rank_high, all_expected["rank"])
                        np.testing.assert_array_equal(observed_caps.size_count, len(oracle))
                        records.append(dict(day=day_text, source_quote_securities=len(source), dated_universe_securities=len(listed),
                            capitalization_securities=len(oracle), top70_count=int(oracle.eligible.sum()),
                            source_quote_ids_outside_universe=sorted(set(source.security_id)-set(listed.security_id))))
                        expected = oracle.loc[oracle.eligible].set_index("security_id").sort_index()
                    observed = actual.set_index("security_id").sort_index()
                    pd.testing.assert_index_equal(observed.index, expected.index, exact=False)
                    np.testing.assert_allclose(observed.value, expected.market_cap if factor=="mcap_mil" else expected.shares,
                        rtol=1e-10, atol=1e-8)
                    if observed.empty or not observed.is_valid.all():
                        raise ValueError("Expected finite top70 output is absent or invalid")
                    checks.append(dict(day=day_text, basis=basis, factor_id=factor, rows=len(observed), status="verified"))
                    export_json(args.output / "summary.json", report)
            print(dict(day=day_text, status="six_staged_factorlab_checks_verified", top70_count=len(expected)), flush=True)
        for path, digest in evidence.items():
            if sha256_file(path) != digest:
                raise ValueError("An input changed during the isolated verification")
        report.update(status="sampled_staged_factorlab_verified", verified_checks=len(checks), verified_days=len(records))
    except Exception as exc:
        report.update(status="failed", error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        shutil.copy2(__file__, args.output / Path(__file__).name)
        report["implementation_sha256"] = sha256_file(__file__)
        export_json(args.output / "summary.json", report)
        if created:
            client.command(f"DROP DATABASE {database}")
        client.close()


if __name__ == "__main__":
    main()
