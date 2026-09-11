"""Verify every restored trading day's top-70 count and values via FactorLab.

This checks ranking against independent original observations within the actual
dated universe. Current-master fallback remains explicitly unverified history.
No recipe, strategy, factor cache or holdout evaluation is written.
"""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-publication", type=Path, required=True)
    parser.add_argument("--observation-sources", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Consumer verification artifacts must stay in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    publication = json.loads(args.native_publication.read_text("utf-8"))
    if not publication["native_published"]:
        raise ValueError("Finish native publication before full consumer verification")
    requested_months = [row["month"] for row in publication["months"]]
    cutoff = pd.Timestamp(publication.get("end_date") or f"{max(publication['source_years'])}-12-31")
    preparation_path = Path(publication["preparation"]) / "summary.json"
    if sha256_file(preparation_path) != publication["preparation_sha256"]:
        raise ValueError("Native source preparation changed")
    preparation = json.loads(preparation_path.read_text("utf-8"))
    share_publication_path = Path(preparation["share_publication_path"])
    if sha256_file(share_publication_path) != preparation["share_publication_sha256"]:
        raise ValueError("Share input publication changed")
    share_publication = json.loads(share_publication_path.read_text("utf-8"))
    audit_refs, source_years = {}, []
    if args.observation_sources:
        index_path = args.observation_sources.resolve()
        index_hash = sha256_file(index_path)
        if preparation["source_audits_sha256"].get(str(index_path)) != index_hash:
            raise ValueError("Observation index was not used in the native source audit")
        index = json.loads(index_path.read_text("utf-8"))
        if (index["status"] != "original_observations_verified"
                or index["share_publication_sha256"] != preparation["share_publication_sha256"]
                or index["end_date"] != cutoff.date().isoformat()):
            raise ValueError("Observation source scope differs from native publication")
        audit_refs.update(index["source_audits_sha256"])
        audit_refs[str(index_path)] = index_hash
        source_years = [dict(year=year["year"], path=Path(year["path"]), sha256=year["sha256"])
            for year in index["years"] if year["year"] in publication["source_years"]]
    elif "preparation_path" in share_publication:
        path = Path(share_publication["preparation_path"])
        if sha256_file(path) != share_publication["preparation_sha256"]:
            raise ValueError("Share source preparation changed")
        audit_refs[str(path)] = share_publication["preparation_sha256"]
        for entry in json.loads(path.read_text("utf-8"))["audits"]:
            audit_path = Path(entry["path"])
            if sha256_file(audit_path) != entry["sha256"]:
                raise ValueError("Original observation audit changed")
            audit_refs[str(audit_path)] = entry["sha256"]
            audit = json.loads(audit_path.read_text("utf-8"))
            source_years.extend(dict(year=year["year"], path=audit_path.parent / str(year["year"]) / "prepared.parquet",
                sha256=year["hashes"]["prepared.parquet"]) for year in audit["years"]
                if year["year"] in publication["source_years"])
    else:
        audit_path = Path(share_publication["source_audit_path"])
        if sha256_file(audit_path) != share_publication["source_audit_sha256"]:
            raise ValueError("Original observation audit changed")
        audit_refs[str(audit_path)] = share_publication["source_audit_sha256"]
        audit = json.loads(audit_path.read_text("utf-8"))
        source_years = [dict(year=year["year"], path=audit_path.parent / f"prepared_{year['year']}.parquet",
            sha256=year["prepared_sha256"]) for year in audit["years"] if year["year"] in publication["source_years"]]
    if sorted(year["year"] for year in source_years) != sorted(publication["source_years"]):
        raise ValueError("Original observation audit does not cover the native publication")
    if (len(requested_months) != len(set(requested_months))
            or requested_months != [str(month) for year in source_years
                for month in pd.period_range(f"{year['year']}-01", f"{year['year']}-12", freq="M")
                if month.start_time <= cutoff]):
        raise ValueError("Native publication does not cover the requested source months")
    for path, digest in audit_refs.items():
        if sha256_file(path) != digest:
            raise ValueError("An original observation source changed")
    episodes_path = DATA_LAKE.silver("survivorship","kr","listing_episodes.json")
    episodes_hash = sha256_file(episodes_path)
    episodes = [r for r in json.loads(episodes_path.read_text("utf-8"))["rows"] if r["status"] == "confirmed"]
    reviewed_ids = {r["security_id"] for r in episodes}
    records, daily = [], []
    report = dict(status="running", coverage_complete=False, native_publication_path=str(args.native_publication.resolve()),
        native_publication_sha256=sha256_file(args.native_publication), listing_episodes_sha256=episodes_hash,
        implementation_sha256=sha256_file(__file__), months=records,
        source_years=publication["source_years"], source_audits_sha256=audit_refs,
        requested_months=requested_months, end_date=cutoff.date().isoformat(),
        policy="ceil(70% of security COUNT), before factor missingness. The dated universe still uses current-master fallback for unreviewed listings; excluded quote identities include preferred and other securities.")
    shutil.copy2(__file__, args.output / Path(__file__).name)
    export_json(args.output / "summary.json",report)
    client = get_clickhouse_client(connect_timeout=5,send_receive_timeout=40)
    try:
        for year in source_years:
            source_path = year["path"]
            if sha256_file(source_path) != year["sha256"]:
                raise ValueError("Original observation preparation changed")
            source = pd.read_parquet(source_path)
            source.trade_date = pd.to_datetime(source.trade_date)
            source = source.loc[source.trade_date.le(cutoff)]
            for month in pd.period_range(f"{year['year']}-01",f"{year['year']}-12",freq="M"):
                if str(month) not in requested_months:
                    continue
                folder = args.output / str(month)
                folder.mkdir()
                observed = source.loc[source.trade_date.between(month.start_time,month.end_time)].copy()
                days = sorted(observed.trade_date.dt.strftime("%Y-%m-%d").unique())
                if not days:
                    raise ValueError("Requested month has no verified original quote dates")
                graph = {"version":2,"experiment":{"name":"restored_market_cap_observations","market":"KR",
                    "start_date":days[0],"end_date":days[-1],"universe":{"size_percentile":{"side":"top","percent":70}}},
                    "nodes":[{"id":"input","type":"factor_input","version":1,"config":{"factor_id":"mcap_mil","financial_basis":"annual","missing_policy":"drop"}}],
                    "edges":[],"outputs":{"final_node_id":"input","evaluation_node_ids":[]}}
                compiled = compile_factor_lab_graph(graph,known_factor_ids={"mcap_mil"},trade_dates=days,
                    factor_table="fact_daily_factors",listing_table="security_listing_episodes")
                prefix,marker,_ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
                if not marker:
                    raise ValueError("Unexpected public FactorLab query shape")
                settings = "\nSETTINGS max_execution_time=30,max_threads=2"
                actual = client.query_df(compiled.query+settings,parameters=compiled.parameters)
                caps = client.query_df(prefix+"\nSELECT * FROM uv_ranked_caps"+settings,parameters=compiled.parameters)
                listed = client.query_df(prefix+"\nSELECT * FROM uv_dated_securities"+settings,parameters=compiled.parameters)
                for frame in (actual,caps,listed):
                    frame.trade_date = pd.to_datetime(frame.trade_date).astype("datetime64[ns]")
                    frame.security_id = frame.security_id.astype(object)
                    if frame.duplicated(KEYS).any():
                        raise ValueError("Consumer returned duplicate dated security identities")
                oracle = observed.merge(listed[KEYS],on=KEYS,how="inner",validate="one_to_one")
                oracle["market_cap"] = oracle.market_cap / 1000000
                oracle = oracle.sort_values(["trade_date","market_cap","security_id"],ascending=[True,False,True])
                oracle["rank"] = oracle.groupby("trade_date").cumcount()+1
                oracle["population"] = oracle.groupby("trade_date").security_id.transform("size")
                oracle["eligible"] = oracle["rank"] <= np.ceil(oracle.population*.7)
                expected = oracle.set_index(KEYS).sort_index()
                ranked = caps.set_index(KEYS).sort_index()
                for name,frame in [("factorlab_values",actual),("capitalization_ranks",caps),("dated_universe",listed),("original_source_oracle",oracle)]:
                    frame.to_parquet(folder/f"{name}.parquet",index=False)
                if not expected.index.equals(ranked.index):
                    export_json(folder/"index_discrepancy.json",dict(expected_rows=len(expected),ranked_rows=len(ranked),
                        expected_dtypes=[str(level.dtype) for level in expected.index.levels],
                        ranked_dtypes=[str(level.dtype) for level in ranked.index.levels],
                        expected_only_rows=len(expected.index.difference(ranked.index)),
                        ranked_only_rows=len(ranked.index.difference(expected.index))))
                    expected.loc[expected.index.difference(ranked.index)].reset_index().to_parquet(folder/"expected_only.parquet",index=False)
                    ranked.loc[ranked.index.difference(expected.index)].reset_index().to_parquet(folder/"ranked_only.parquet",index=False)
                    raise ValueError("Native capitalization universe differs from dated source observations")
                np.testing.assert_allclose(ranked.market_cap,expected.market_cap,rtol=1e-10,atol=1e-8)
                np.testing.assert_array_equal(ranked.size_rank_high,expected["rank"])
                np.testing.assert_array_equal(ranked.size_count,expected.population)
                eligible = expected.loc[expected.eligible]
                final = actual.set_index(KEYS).sort_index()
                if not final.index.equals(eligible.index):
                    raise ValueError("Actual FactorLab output differs from expected top-70 membership")
                if not final.is_valid.all():
                    raise ValueError("Expected finite market-cap output is invalid")
                np.testing.assert_allclose(final.value,eligible.market_cap,rtol=1e-10,atol=1e-8)
                for day in days:
                    when = pd.Timestamp(day)
                    active = {r["security_id"] for r in episodes if r["valid_from"] <= day and (r["valid_until"] is None or day < r["valid_until"])}
                    identities = set(listed.loc[listed.trade_date.eq(when),"security_id"])
                    if identities & reviewed_ids != active:
                        raise ValueError("Confirmed listing membership differs from reviewed interval")
                    pool = oracle.loc[oracle.trade_date.eq(when)]
                    quotes = observed.loc[observed.trade_date.eq(when)]
                    count = int(pool.eligible.sum())
                    if count != int(np.ceil(len(pool)*.7)):
                        raise ValueError("Top-70 count differs from policy")
                    daily.append(dict(trade_date=day,source_quote_securities=len(quotes),dated_universe_securities=len(identities),
                        capitalization_securities=len(pool),top70_securities=count,confirmed_listing_securities=len(active),
                        quote_ids_outside_dated_universe=len(set(quotes.security_id)-identities),
                        missing_cap_in_dated_universe=len(identities)-len(pool)))
                (folder/"query.sql").write_text(compiled.query,"utf-8")
                export_json(folder/"graph.json",graph)
                record = dict(month=str(month),days=len(days),source_rows=len(observed),capitalization_rows=len(caps),
                    top70_output_rows=len(actual),status="verified",hashes={p.name:sha256_file(p) for p in folder.glob("*.parquet")})
                records.append(record)
                export_json(args.output/"summary.json",report)
                print({k:v for k,v in record.items() if k!='hashes'},flush=True)
    finally:
        client.close()
    if sha256_file(episodes_path) != episodes_hash:
        raise ValueError("Listing episodes changed during verification")
    for path, digest in audit_refs.items():
        if sha256_file(path) != digest:
            raise ValueError("Original source audit changed during verification")
    for year in source_years:
        if sha256_file(year["path"]) != year["sha256"]:
            raise ValueError("Original observations changed during verification")
    daily_path = args.output/"daily_counts.parquet"
    pd.DataFrame(daily).to_parquet(daily_path,index=False)
    report.update(status="verified",verified_days=len(daily),source_quote_rows=sum(r['source_rows'] for r in records),
        capitalization_rows=sum(r['capitalization_rows'] for r in records),top70_output_rows=sum(r['top70_output_rows'] for r in records),
        daily_counts_sha256=sha256_file(daily_path),recipe_or_holdout_changed=False)
    export_json(args.output/"summary.json",report)
    years = sorted(publication["source_years"])
    scope_name = "_".join(map(str, years))
    if len(years) > 1 and years == list(range(years[0], years[-1] + 1)):
        scope_name = f"{years[0]}_{years[-1]}"
    export_json(DATA_LAKE.gold('survivorship','kr','capitalization_factors',scope_name,'factorlab_verification.json'),
        {**report,'verification_path':str((args.output/'summary.json').resolve()),'daily_counts_path':str(daily_path.resolve())})
    print(report['status'],report['verified_days'],report['capitalization_rows'],report['top70_output_rows'],flush=True)


if __name__ == '__main__':
    main()
