"""Historical share changes reach the real refresh command and FactorLab DB."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pandas as pd
import pytest

from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DataLakePaths

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share_refresh_environment(tmp_path):
    lake = DataLakePaths(tmp_path / "data-lake")
    database = "arcana_test_share_refresh_" + uuid4().hex
    admin = get_clickhouse_client(connect_timeout=3, send_receive_timeout=30)
    production = admin.query("SELECT currentDatabase()").result_rows[0][0]
    assert production.replace("_", "").isalnum()
    admin.command(f"CREATE DATABASE {database}")
    client = None
    try:
        for table in ("price_daily", "fact_daily_factors", "fact_daily_factor_snapshot", "factor_catalog",
                      "security_master", "issuers", "identifiers"):
            admin.command(f"CREATE TABLE {database}.{table} AS {production}.{table}")
        admin.command(f"CREATE TABLE {database}.security_listing_episodes ENGINE = Memory "
                      f"AS SELECT * FROM {production}.security_listing_episodes WHERE 0")
        client = get_clickhouse_client(database=database, connect_timeout=3, send_receive_timeout=30)
        days = pd.bdate_range("2020-01-02", periods=100)
        for symbol in ("999980", "999990"):
            raw = lake.bronze("krx", "price", f"kr_{symbol}.csv")
            raw.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"날짜": days, "시가": 100, "고가": 100, "저가": 100, "종가": 100,
                          "거래량": 50}).to_csv(raw, index=False)
            shares = lake.bronze("krx", "shares", f"kr_{symbol}.csv")
            shares.parent.mkdir(parents=True, exist_ok=True)
            share_days = [days[0], days[-1]] if symbol == "999980" else [days[-1]]
            pd.DataFrame({"날짜": share_days, "상장주식수": 10, "시가총액": 1000}).to_csv(shares, index=False)
            sid = "SEC_KR_" + symbol
            client.insert("security_master", [(sid, symbol, "KR", True, "KRX")],
                column_names=["security_id", "issuer_id", "country", "is_active", "exchange_code"])
            client.insert("issuers", [(symbol, "Synthetic share fixture")], column_names=["issuer_id", "legal_name_en"])
            client.insert("identifiers", [(sid, "KRX_CODE", symbol, True)],
                column_names=["security_id", "id_type", "id_value", "is_primary"])
            # The source CSV retains the 100-day turnover warmup. Only these
            # two observation dates are needed by the public snapshot consumer.
            client.insert("price_daily", [(sid, day.date(), *([Decimal("100")] * 5), 50, "KRW") for day in days[-2:]],
                column_names=["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "currency"])

        def run(mode, *, basis="annual", succeeds=True):
            program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE = paths.DataLakePaths(Path(sys.argv[1]))
from engine.core.source_storage import SourceRefreshLock
with SourceRefreshLock('kr', data_lake_root=paths.DATA_LAKE.root):
    if sys.argv[2] == 'normalize':
        from engine.transformers.market_data import normalize_price, normalize_shares
        normalize_price(str(paths.DATA_LAKE.bronze('krx','price','*.csv')))
        normalize_shares(str(paths.DATA_LAKE.bronze('krx','shares','*.csv')))
    else:
        from engine.workflows import refresh
        state = paths.DATA_LAKE.silver('refresh_state', sys.argv[2]+'_'+sys.argv[4]+'.json')
        args = refresh.build_arg_parser().parse_args(['--market','kr','--targets',sys.argv[2],
            '--symbols','999980,999990','--end-date',sys.argv[3],'--financial-basis',sys.argv[4],
            '--workers','1','--resume','--resume-state-path',str(state)])
        refresh.run_refresh(args)
"""
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
                str(lake.root), mode, days[-1].strftime("%Y%m%d"), basis], cwd=ROOT,
                env={**os.environ, "CLICKHOUSE_DATABASE": database}, text=True, encoding="utf-8",
                capture_output=True, timeout=120)
            logs = lake.silver("test_results", f"{mode}_{uuid4().hex}.log")
            logs.parent.mkdir(parents=True, exist_ok=True)
            logs.write_text(result.stdout + result.stderr, "utf-8")
            assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
            return result.stdout + result.stderr

        yield lake, client, days, run
    finally:
        if client is not None:
            client.close()
        assert database.startswith("arcana_test_share_refresh_")
        admin.command(f"DROP DATABASE {database}")
        admin.close()


def factorlab_value(client, day, factor, *, table="fact_daily_factors", basis="annual"):
    text = day.strftime("%Y-%m-%d")
    graph = {"version": 2, "experiment": {"name": "share_input_refresh", "market": "KR", "start_date": text, "end_date": text},
        "nodes": [{"id": "input", "type": "factor_input", "version": 1,
            "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
        "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
    compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[text],
        factor_table=table, listing_table="security_listing_episodes")
    rows = client.query_df(compiled.query, parameters=compiled.parameters)
    return dict(zip(rows.security_id, rows.value)) if len(rows) else {}


def register_earlier_shares(lake, day):
    source = lake.bronze("marcap", "data", "marcap-2020.parquet")
    source.parent.mkdir(parents=True)
    pd.DataFrame([dict(Code="999990", Date=day, Close=100, Stocks=20, Marcap=2000)]).to_parquet(source, index=False)
    manifest = lake.silver("krx", "shares", "historical_sources.json")
    manifest.write_text(json.dumps(dict(schema_version=1, sources=[dict(year=2020,
        path=source.relative_to(lake.root).as_posix(), sha256=hashlib.sha256(source.read_bytes()).hexdigest())])), "utf-8")


def test_resumed_refresh_applies_earlier_shares_and_dependent_turnover(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    assert factorlab_value(client, days[-1], "csho")["SEC_KR_999980"] == 10
    register_earlier_shares(lake, days[0])
    run("normalize")
    run("factors")

    assert factorlab_value(client, days[-2], "csho")["SEC_KR_999990"] == 20
    assert factorlab_value(client, days[-2], "mcap_mil")["SEC_KR_999990"] == pytest.approx(.002)
    # Fifty daily shares traded / twenty outstanding shares, expressed as percent.
    assert factorlab_value(client, days[-1], "adturn_pct_12_1")["SEC_KR_999990"] == pytest.approx(250)
    assert factorlab_value(client, days[-1], "csho")["SEC_KR_999980"] == 10


def test_refresh_detects_new_share_source_without_separate_normalization(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    register_earlier_shares(lake, days[0])
    run("factors")
    assert factorlab_value(client, days[-2], "csho")["SEC_KR_999990"] == 20


def test_resumed_snapshot_refresh_reaches_the_historical_factorlab_date(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    run("snapshots")
    register_earlier_shares(lake, days[0])
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-2], "csho", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == 20
    assert factorlab_value(client, days[-1], "adturn_pct_12_1", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == pytest.approx(250)


def test_withdrawn_share_history_revokes_old_values_across_missing_months(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    register_earlier_shares(lake, days[0])
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-1], "adturn_pct_12_1")["SEC_KR_999990"] == pytest.approx(250)
    lake.silver("krx", "shares", "historical_sources.json").unlink()
    run("factors")
    run("snapshots")
    for table in ("fact_daily_factors", "fact_daily_factor_snapshot"):
        assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "csho", table=table)
        assert "SEC_KR_999990" not in factorlab_value(client, days[-1], "adturn_pct_12_1", table=table)
        assert factorlab_value(client, days[-1], "csho", table=table)["SEC_KR_999990"] == 10
        assert factorlab_value(client, days[-1], "adturn_pct_12_1", table=table)["SEC_KR_999980"] == pytest.approx(500)


def test_refresh_recovers_a_share_file_swap_interrupted_before_its_receipt(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    latest_path = lake.silver("market_input_changes", "kr", "latest.json")
    previous_pointer = latest_path.read_bytes()
    register_earlier_shares(lake, days[0])
    run("normalize")
    # Reconstruct the durable state after CSV replacement but before the
    # publication receipt and latest pointer were committed.
    journal = Path(json.loads(latest_path.read_text("utf-8"))["report_path"])
    record = json.loads(journal.read_text("utf-8"))
    record["status"] = "prepared"
    journal.write_text(json.dumps(record), "utf-8")
    latest_path.write_bytes(previous_pointer)
    run("factors")
    assert factorlab_value(client, days[-2], "csho")["SEC_KR_999990"] == 20


def test_verified_share_refresh_exports_consumer_values_to_gold(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    register_earlier_shares(lake, days[0])
    raw_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (lake.root / "bronze").rglob("*") if path.is_file()}
    run("factors")
    run("snapshots")
    publications = list(lake.gold("share_input_rebuilds", "kr", "999990", "annual").glob("*/*/summary.json"))
    assert len(publications) == 2
    for path in publications:
        report = json.loads(path.read_text("utf-8"))
        assert report["status"] == "published_and_verified"
        assert report["whole_market_survivorship_complete"] is False
        frames = [pd.read_parquet(item["path"]) for item in report["artifacts"]]
        rows = pd.concat(frames)
        actual = rows.loc[rows.factor_id.eq("csho") & pd.to_datetime(rows.trade_date).eq(days[-2]), "factor_value"]
        assert actual.tolist() == [20]
    assert raw_files == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (lake.root / "bronze").rglob("*") if path.is_file()}


def test_share_snapshots_require_the_same_basis_and_completed_replays_are_idle(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    register_earlier_shares(lake, days[0])
    for basis in ("annual", "quarterly", "ttm"):
        error = run("snapshots", basis=basis, succeeds=False)
        assert "rebuild historical factors for this financial basis before snapshots" in error
        run("factors", basis=basis)
        run("snapshots", basis=basis)
        assert factorlab_value(client, days[-2], "csho", table="fact_daily_factor_snapshot", basis=basis)["SEC_KR_999990"] == 20
    published = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (lake.root / "gold").rglob("*") if path.is_file()}
    for basis in ("annual", "quarterly", "ttm"):
        assert "skipping completed step: factors" in run("factors", basis=basis)
        assert "skipping completed step: snapshots" in run("snapshots", basis=basis)
    assert published == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (lake.root / "gold").rglob("*") if path.is_file()}


def test_refresh_rejects_share_values_changed_outside_normalization(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    normalized = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    rows = pd.read_csv(normalized)
    rows.loc[rows.security_id.eq("SEC_KR_999990"), "shares"] = 99
    rows.to_csv(normalized, index=False)
    error = run("factors", succeeds=False)
    assert "outside its normalization journal" in error
    assert factorlab_value(client, days[-1], "csho")["SEC_KR_999990"] == 10
