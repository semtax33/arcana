"""Run full retained three-security histories through isolated public refresh.

The independently reconstructed universe uses source observations and DART
intervals. It never derives expected membership from the generated query.
"""
import argparse
import json
import math
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
from api.service.dto import FactorLabGraphDto
from api.service.factor_lab_service import FactorLabService
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Verification evidence belongs in Silver")
    prep = json.loads(args.preparation.read_text("utf-8"))
    assert prep["price_history_preview_verified"] and not prep["production_changed"]
    refs = {**prep["pinned_inputs"], str(args.preparation.resolve()): sha256_file(args.preparation)}
    manifest_path = Path(prep["proposal_path"])
    refs[str(manifest_path)] = prep["proposal_sha256"]
    for p, digest in refs.items():
        assert sha256_file(p) == digest, p
    manifest = json.loads(manifest_path.read_text("utf-8"))
    ids = ["SEC_KR_" + item["symbol"] for item in prep["checks"]]
    # Isolated replay input, deliberately not a production replacement file.
    # Keep all 15 listing/rights records while only loading the three price
    # histories under test. No historical issuer gets a fabricated master row.
    manifest["market_data_sources"] = [r for r in manifest["market_data_sources"] if r["security_id"] in ids]
    args.output.mkdir(parents=True, exist_ok=False)
    manifest_path = args.output / "isolated_three_price_input.json"
    export_json(manifest_path, manifest)
    originals = []
    for item in prep["checks"]:
        path = args.preparation.parent / item["symbol"] / "original_listed_observations.parquet"
        assert sha256_file(path) == item["original_sha256"]
        refs[str(path.resolve())] = item["original_sha256"]
        original = pd.read_parquet(path)
        original["security_id"] = "SEC_KR_" + item["symbol"]
        originals.append(original)
    originals = pd.concat(originals, ignore_index=True)
    originals.Date = pd.to_datetime(originals.Date)
    originals.to_parquet(args.output / "original_observations.parquet", index=False)
    ledger = DATA_LAKE.silver("corporate_actions", "kr_stock_splits.json")
    refs[str(ledger.resolve())] = sha256_file(ledger)
    isolated_lake = args.output / "isolated_lake"
    copied_ledger = isolated_lake / "silver/corporate_actions/kr_stock_splits.json"
    copied_ledger.parent.mkdir(parents=True)
    shutil.copy2(ledger, copied_ledger)
    start, end = "2016-09-21", "2026-06-19"
    database = "arcana_test_full_listing_prices_" + uuid4().hex
    admin = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
    client = None
    created = False
    report = dict(status="running", production_changed=False, coverage_complete=False,
        financial_histories_approved=False, full_split_source_coverage_verified=False,
        terminal_rights_complete=False, source_security_ids=ids, original_rows=len(originals),
        start_date=start, end_date=end, checks=[], input_sha256=refs)
    export_json(args.output / "summary.json", report)
    try:
        production = admin.query("SELECT currentDatabase()").result_rows[0][0]
        assert production.replace("_", "").isalnum()
        calendar_prices = admin.query_df("""SELECT security_id, trade_date,
            quote.1 AS open, quote.2 AS high, quote.3 AS low, quote.4 AS close,
            quote.5 AS adj_close, quote.6 AS volume, quote.7 AS currency
            FROM (SELECT security_id, trade_date,
                argMax(tuple(open,high,low,close,coalesce(adj_close,close),volume,currency),updated_at) AS quote
                FROM price_daily WHERE security_id='SEC_KR_005930'
                    AND trade_date BETWEEN {start:Date} AND {end:Date}
                GROUP BY security_id,trade_date) ORDER BY trade_date
            SETTINGS max_execution_time=30,max_threads=2""", parameters=dict(start=start, end=end))
        calendar_prices.trade_date = pd.to_datetime(calendar_prices.trade_date)
        assert len(calendar_prices) > 2000
        calendar_prices.to_parquet(args.output / "actual_calendar_price_rows.parquet", index=False)
        calendar_master = admin.query_df("""SELECT security_id,issuer_id,country,is_active,exchange_code
            FROM security_master WHERE security_id='SEC_KR_005930' ORDER BY updated_at DESC LIMIT 1""")
        assert len(calendar_master) == 1
        admin.command(f"CREATE DATABASE {database}")
        created = True
        for table in ("price_daily", "fact_daily_factors", "fact_daily_factor_snapshot", "factor_catalog",
                      "security_master", "issuers", "identifiers", "benchmark_price_daily"):
            admin.command(f"CREATE TABLE {database}.{table} AS {production}.{table}")
        client = get_clickhouse_client(database=database, connect_timeout=5, send_receive_timeout=40)
        for _, year_prices in calendar_prices.groupby(calendar_prices.trade_date.dt.year):
            client.insert_df("price_daily", year_prices)
        client.insert_df("security_master", calendar_master)
        client.insert("factor_catalog", [("mcap_mil", "Market capitalization", "HIGHER_BETTER")],
            column_names=["factor_id", "factor_name", "value_direction"])
        code = "\n".join([
            "import sys", "from pathlib import Path", "from engine.core import paths",
            "paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))", "from engine.workflows import refresh",
            "args=refresh.build_arg_parser().parse_args(['--market','kr','--targets','survivorship',",
            "'--end-date','2026-09-10','--survivorship-manifest',sys.argv[2],'--survivorship-no-download','--no-resume'])",
            "refresh.run_refresh(args)",
        ])
        replay = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(isolated_lake), str(manifest_path.resolve())],
            cwd=ROOT, env={**os.environ, "CLICKHOUSE_DATABASE": database}, capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        (args.output / "refresh_stdout.txt").write_text(replay.stdout, "utf-8")
        (args.output / "refresh_stderr.txt").write_text(replay.stderr, "utf-8")
        if replay.returncode:
            raise ValueError("Isolated public refresh failed; inspect retained logs")
        factory = lambda: get_clickhouse_client(database=database, connect_timeout=5, send_receive_timeout=40)
        service = FactorLabService(client_factory=factory)
        calendar = pd.DatetimeIndex(calendar_prices.trade_date.unique()).union(pd.DatetimeIndex(originals.Date.unique())).sort_values()
        calendar = calendar[(calendar >= start) & (calendar <= end)]
        assert calendar.is_unique and calendar.is_monotonic_increasing
        expected_parts = []
        for sid in ids:
            obs = originals.loc[originals.security_id.eq(sid)].sort_values("Date")
            aligned = pd.merge_asof(pd.DataFrame({"trade_date": calendar}), obs[["Date", "Marcap"]],
                left_on="trade_date", right_on="Date", direction="backward")
            episodes = [r for r in manifest["listing_episodes"] if r["security_id"] == sid]
            assert len(episodes) == 1
            episode = episodes[0]
            allowed = aligned.trade_date.ge(episode["valid_from"]) & aligned.trade_date.lt(episode["valid_until"])
            for halt in (r for r in manifest["trading_halts"] if r["security_id"] == sid):
                allowed &= ~(aligned.trade_date.ge(halt["start_date"]) & aligned.trade_date.lt(halt["end_date"]))
            aligned = aligned.loc[allowed & aligned.Marcap.gt(0)].copy()
            aligned["security_id"] = sid
            expected_parts.append(aligned)
        expected = pd.concat(expected_parts).sort_values(["trade_date", "Marcap", "security_id"], ascending=[True, False, True])
        expected["population"] = expected.groupby("trade_date").security_id.transform("size")
        expected["rank"] = expected.groupby("trade_date").cumcount() + 1
        expected = expected.loc[expected["rank"] <= expected.population.map(lambda n: math.ceil(n * .7))]
        expected["value"] = expected.Marcap / 1_000_000
        expected.to_parquet(args.output / "independent_top70_expected.parquet", index=False)
        total_rows = 0
        for month in pd.period_range(start, end, freq="M"):
            dates = calendar[(calendar >= month.start_time) & (calendar <= month.end_time)]
            if dates.empty:
                continue
            graph = dict(version=2, experiment=dict(name="Full proposed listing history", market="KR",
                start_date=str(dates.min().date()), end_date=str(dates.max().date()),
                universe=dict(size_percentile=dict(side="top", percent=70))),
                nodes=[dict(id="input", type="factor_input", version=1,
                    config=dict(factor_id="mcap_mil", financial_basis="annual", missing_policy="drop"))],
                edges=[], outputs=dict(final_node_id="input", evaluation_node_ids=[]))
            compiled = service.compile_graph(FactorLabGraphDto.model_validate(graph))
            actual = client.query_df(compiled.query, parameters=compiled.parameters)
            actual.trade_date = pd.to_datetime(actual.trade_date)
            wanted = expected.loc[expected.trade_date.isin(dates), ["trade_date", "security_id", "value"]]
            observed = actual[["trade_date", "security_id", "value"]]
            folder = args.output / str(month)
            folder.mkdir()
            observed.to_parquet(folder / "actual.parquet", index=False)
            wanted.to_parquet(folder / "expected.parquet", index=False)
            (folder / "query.sql").write_text(compiled.query, "utf-8")
            export_json(folder / "graph.json", graph)
            export_json(folder / "parameters.json", compiled.parameters)
            combined = wanted.merge(observed, on=["trade_date", "security_id"], how="outer", indicator=True,
                validate="one_to_one", suffixes=("_expected", "_actual"))
            assert combined._merge.eq("both").all(), f"Membership mismatch {month}"
            np.testing.assert_allclose(combined.value_actual, combined.value_expected, rtol=1e-12, atol=1e-8)
            total_rows += len(actual)
            report["checks"].append(dict(month=str(month), days=len(dates), rows=len(actual),
                hashes={p.name: sha256_file(p) for p in folder.iterdir()}))
            export_json(args.output / "summary.json", report)
            print(str(month), len(dates), len(actual), "verified", flush=True)
        assert total_rows == len(expected)
        for p, digest in refs.items():
            assert sha256_file(p) == digest, p
        report.update(status="full_three_security_public_refresh_factorlab_history_verified",
            verified_days=len(calendar), top70_rows=total_rows,
            limitation="Three-security isolated source universe, not whole-market or terminal-rights completion. A zero-volume day is not itself a confirmed regulatory halt.")
    except BaseException as error:
        report.update(status="failed_check_evidence", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if client is not None:
            client.close()
        if created:
            assert database.startswith("arcana_test_full_listing_prices_")
            admin.command(f"DROP DATABASE {database}")
        admin.close()
        shutil.copy2(__file__, args.output / Path(__file__).name)
        report["implementation_sha256"] = sha256_file(__file__)
        export_json(args.output / "summary.json", report)
    export_json(DATA_LAKE.gold("survivorship", "kr", "listing_price_preview", "20260911", "full_history_verification.json"),
        {**report, "silver_summary": str((args.output / "summary.json").resolve()),
         "silver_summary_sha256": sha256_file(args.output / "summary.json")})
    print(json.dumps({k: report[k] for k in ("status", "verified_days", "top70_rows")}), flush=True)


if __name__ == "__main__":
    main()
