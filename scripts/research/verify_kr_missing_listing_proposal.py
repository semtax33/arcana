"""Replay proposed DART identities with actual market observations in isolation."""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.service.backtest_service import BacktestService
from api.service.dto import FactorBacktestRequestDto, FactorConditionDto, FactorLabGraphDto
from api.service.factor_lab_service import FactorLabService
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-summary", type=Path, required=True)
    parser.add_argument("--observation-sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Isolated verification belongs in Silver")
    output.mkdir(parents=True, exist_ok=False)
    proposal = json.loads(args.proposal_summary.read_bytes())
    refs = {**proposal["pinned_inputs"], str(args.proposal_summary.resolve()): digest(args.proposal_summary),
            str(args.observation_sources.resolve()): digest(args.observation_sources)}
    index = json.loads(args.observation_sources.read_bytes())
    original = next(row for row in index["years"] if row["year"] == 2026)
    refs[original["path"]] = original["sha256"]
    manifest = args.proposal_summary.parent / "identity_validation_input.json"
    refs[str(manifest.resolve())] = digest(manifest)
    for path, expected in refs.items():
        if digest(path) != expected:
            raise ValueError("Proposal or market-source reference changed")
    source = pd.read_parquet(original["path"])
    ids = [row["episode"]["security_id"] for row in proposal["additions"]]
    start, end = "2026-05-26", "2026-06-22"
    source = source.loc[source.security_id.isin(ids + ["SEC_KR_005930"])
                        & source.trade_date.between(start, end)].copy()
    source.to_parquet(output / "original_market_observations.parquet", index=False)
    admin = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
    production = admin.query("SELECT currentDatabase()").result_rows[0][0]
    if not production.replace("_", "").isalnum():
        raise ValueError("Unexpected source database identifier")
    database = "arcana_test_listing_proposal_" + uuid4().hex
    client = None
    records = []
    try:
        params = dict(ids=ids + ["SEC_KR_005930"], start=start, end=end)
        prices = admin.query_df("""SELECT security_id, trade_date,
            quote.1 AS open, quote.2 AS high, quote.3 AS low, quote.4 AS close,
            quote.5 AS adj_close, quote.6 AS volume, quote.7 AS currency
            FROM (SELECT security_id, trade_date,
                argMax(tuple(open,high,low,close,coalesce(adj_close,close),volume,currency),updated_at) AS quote
                FROM price_daily WHERE has({ids:Array(String)},security_id)
                    AND trade_date BETWEEN {start:Date} AND {end:Date}
                GROUP BY security_id,trade_date) ORDER BY security_id,trade_date""", parameters=params)
        prices.trade_date = pd.to_datetime(prices.trade_date).astype("datetime64[ns]")
        compared = source.merge(prices, on=["security_id", "trade_date"], how="outer", validate="one_to_one", indicator=True)
        if (not compared._merge.eq("both").all()
                or not np.allclose(compared.raw_close, compared.close.astype(float), rtol=0, atol=1e-8)
                or not np.array_equal(compared.raw_volume.astype(float), compared.volume.astype(float))):
            raise ValueError("Actual price rows do not agree with the reviewed market originals")
        prices.to_parquet(output / "actual_price_rows.parquet", index=False)
        calendar_master = admin.query_df("""SELECT security_id,issuer_id,country,is_active,exchange_code
            FROM security_master WHERE security_id='SEC_KR_005930' ORDER BY updated_at DESC LIMIT 1""")
        if len(calendar_master) != 1:
            raise ValueError("A real market calendar security is required")
        admin.command(f"CREATE DATABASE {database}")
        for table in ("price_daily", "fact_daily_factors", "fact_daily_factor_snapshot", "factor_catalog",
                      "security_master", "issuers", "identifiers", "benchmark_price_daily"):
            admin.command(f"CREATE TABLE {database}.{table} AS {production}.{table}")
        client = get_clickhouse_client(database=database, connect_timeout=5, send_receive_timeout=40)
        client.insert_df("price_daily", prices)
        client.insert_df("security_master", calendar_master)
        factors = source.loc[source.security_id.isin(ids), ["security_id", "trade_date", "market_cap"]].copy()
        factors["factor_value"] = factors.pop("market_cap") / 1_000_000
        factors["factor_id"], factors["financial_basis"], factors["currency"] = "mcap_mil", "annual", "KRW"
        client.insert_df("fact_daily_factors", factors)
        client.insert("factor_catalog", [("mcap_mil", "Market capitalization", "HIGHER_BETTER")],
            column_names=["factor_id", "factor_name", "value_direction"])
        code = "\n".join([
            "import sys", "from pathlib import Path", "from engine.core import paths",
            "paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))", "from engine.workflows import refresh",
            "args=refresh.build_arg_parser().parse_args(['--market','kr','--targets','survivorship',",
            "'--end-date','2026-09-10','--survivorship-manifest',sys.argv[2],'--survivorship-no-download','--no-resume'])",
            "refresh.run_refresh(args)",
        ])
        replay = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(output / "isolated_lake"), str(manifest.resolve())],
            cwd=ROOT, env={**os.environ, "CLICKHOUSE_DATABASE": database}, capture_output=True, text=True,
            encoding="utf-8", timeout=90)
        (output / "refresh_stdout.txt").write_text(replay.stdout, "utf-8")
        (output / "refresh_stderr.txt").write_text(replay.stderr, "utf-8")
        if replay.returncode:
            raise ValueError("Isolated public refresh failed; inspect the retained logs")
        factory = lambda: get_clickhouse_client(database=database, connect_timeout=5, send_receive_timeout=40)
        graph = dict(version=2, experiment=dict(name="DART proposed histories", market="KR", start_date=start,
            end_date=end, universe=dict(size_percentile=dict(side="top", percent=70))),
            nodes=[dict(id="input", type="factor_input", version=1,
                config=dict(factor_id="mcap_mil", financial_basis="annual", missing_policy="drop"))],
            edges=[], outputs=dict(final_node_id="input", evaluation_node_ids=[]))
        compiled = FactorLabService(client_factory=factory).compile_graph(FactorLabGraphDto.model_validate(graph))
        (output / "factorlab.sql").write_text(compiled.query, "utf-8")
        export_json(output / "parameters.json", compiled.parameters)
        actual = client.query_df(compiled.query, parameters=compiled.parameters)
        actual.to_parquet(output / "factorlab_output.parquet", index=False)
        prefix, marker, _ = compiled.query.rpartition("\nSELECT *\nFROM node_input")
        if not marker:
            raise ValueError("Unexpected public graph query shape")
        members = client.query_df(prefix + "\nSELECT * FROM uv_dated_securities", parameters=compiled.parameters)
        members.trade_date = pd.to_datetime(members.trade_date)
        members.to_parquet(output / "dated_membership.parquet", index=False)
        boundary_cases = {
            "SEC_KR_204210": [("2026-06-01", False), ("2026-06-02", True), ("2026-06-11", True), ("2026-06-12", False)],
            "SEC_KR_464440": [("2026-06-08", False), ("2026-06-09", True), ("2026-06-17", True), ("2026-06-18", False)],
            "SEC_KR_464680": [("2026-05-27", False), ("2026-05-28", True), ("2026-06-08", True), ("2026-06-09", False)],
        }
        for sid, cases in boundary_cases.items():
            for day, expected in cases:
                present = bool(((members.security_id == sid) & (members.trade_date == pd.Timestamp(day))).any())
                if present != expected:
                    raise ValueError(f"Dated universe disagrees with DART interval: {sid} {day}")
                records.append(dict(security_id=sid, trade_date=day, eligible=present))
        oracle = source.loc[source.security_id.isin(ids)].merge(members[["security_id", "trade_date"]],
            on=["security_id", "trade_date"], how="inner", validate="one_to_one")
        oracle = oracle.sort_values(["trade_date", "market_cap", "security_id"], ascending=[True, False, True])
        oracle["rank"] = oracle.groupby("trade_date").cumcount()+1
        oracle["count"] = oracle.groupby("trade_date").security_id.transform("size")
        selected = oracle.loc[oracle["rank"] <= np.ceil(oracle["count"]*.7)].copy()
        actual.trade_date = pd.to_datetime(actual.trade_date)
        compared = selected.merge(actual[["security_id", "trade_date", "value"]],
            on=["security_id", "trade_date"], how="outer", indicator=True, validate="one_to_one")
        if not compared._merge.eq("both").all() or not np.allclose(compared.market_cap/1_000_000, compared.value):
            raise ValueError("FactorLab top-70 values or members differ from the originals")
        compared.to_parquet(output / "top70_original_comparison.parquet", index=False)
        outcomes = []
        for sid, entry, stop in (("SEC_KR_204210", "2026-06-03", "2026-06-12"),
                                 ("SEC_KR_464440", "2026-06-10", "2026-06-18"),
                                 ("SEC_KR_464680", "2026-05-29", "2026-06-09")):
            table = "target_" + sid.rsplit("_", 1)[1]
            client.command(f"CREATE VIEW {table} AS SELECT * FROM fact_daily_factors WHERE security_id='{sid}'")
            request = FactorBacktestRequestDto(conditions=[FactorConditionDto(factor_id="mcap_mil",
                mode="top_percent", top_percent=100)], start_date=date.fromisoformat(entry), end_date=date.fromisoformat(stop),
                market="kr", factor_table=table, max_positions=1, transaction_cost_bps=0, benchmarks=[], rebalance_frequency="monthly")
            try:
                BacktestService(client_factory=factory).run_factor_backtest(request)
            except ValueError as error:
                if "Lifecycle entitlements are not complete" not in str(error):
                    raise
                outcomes.append(dict(security_id=sid, result="unresolved_rights_rejected", message=str(error)))
            else:
                raise ValueError(f"Unresolved real terminal exposure produced a return: {sid}")
        for path, expected in refs.items():
            if digest(path) != expected:
                raise ValueError("An input changed during the isolated replay")
        implementation = output / "implementation" / Path(__file__).name
        implementation.parent.mkdir()
        shutil.copyfile(__file__, implementation)
        report = dict(status="actual_sources_public_refresh_factorlab_and_terminal_exposure_verified",
            price_rows_verified=len(prices), factor_rows_staged=len(factors), boundary_checks=records,
            top70_rows_verified=len(compared), unresolved_terminal_checks=outcomes, input_sha256=refs,
            implementation_sha256=digest(implementation), production_changed=False,
            financial_histories_approved=False, terminal_rights_complete=False, coverage_complete=False)
        export_json(output / "summary.json", report)
        export_json(DATA_LAKE.gold("survivorship", "kr", "listing_refresh_proposal", "20260911", "isolated_verification.json"),
            {**report, "silver_summary": str(output / "summary.json"), "silver_summary_sha256": digest(output / "summary.json")})
        print({key: report[key] for key in ("status", "price_rows_verified", "top70_rows_verified", "production_changed")}, flush=True)
    finally:
        if client is not None:
            client.close()
        if not database.startswith("arcana_test_listing_proposal_"):
            raise ValueError("Unexpected isolated database target")
        admin.command(f"DROP DATABASE IF EXISTS {database}")
        admin.close()


if __name__ == "__main__":
    main()
