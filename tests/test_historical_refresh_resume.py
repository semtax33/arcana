"""Changed historical inputs survive a completed public refresh checkpoint."""
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from engine.workflows.financial_history import publish_reviewed_financial_history
from api.repository.factor_lab_query import compile_factor_lab_graph
from test_share_input_refresh import share_refresh_environment, factorlab_value

pytestmark = pytest.mark.integration


def publish_sales(lake, received, amount):
    receipt = received.replace("-", "") + "000001"
    source = lake.bronze("dart", "test_receipts", receipt + ".html")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"<DOCUMENT><COMPANY-NAME AREGCIK='00999990'>Synthetic issuer</COMPANY-NAME>{amount}</DOCUMENT>", "utf-8")
    normalized = lake.silver("test_reviews", receipt + ".csv")
    normalized.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(canonical_account_id="REVENUE", statement_type="IS",
        original_account_name="매출액", period="2019.12", normalized_amount=amount)]).to_csv(normalized, index=False)
    record = dict(rcept_no=receipt, fiscal_year=2019, fiscal_month=12,
        period_end_date="2019-12-31", period_start_date="2019-01-01", report_date=received,
        financial_basis="annual", financial_scope="CFS", accounting_regime="K_IFRS", review_status="verified",
        review_evidence="Synthetic receipt with known issuer, period, amount, scope and publication date.",
        accepted_canonical_account_ids=["REVENUE"],
        normalized_path=normalized.relative_to(lake.root).as_posix(),
        normalized_sha256=hashlib.sha256(normalized.read_bytes()).hexdigest(),
        source_path=source.relative_to(lake.root).as_posix(),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_url=f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}")
    review = normalized.with_suffix(".json")
    review.write_text(json.dumps(dict(schema_version=1, market="kr", symbol="999990",
        corp_code="00999990", source_root=str(lake.root), receipts=[record])), "utf-8")
    publish_reviewed_financial_history(review, financial_dir=lake.silver("dart", "normalized"))


def test_resumed_refresh_reloads_new_financial_receipts_and_their_historical_snapshots(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    run("factors")
    run("snapshots")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "sale")

    publish_sales(lake, "2020-03-31", 100)
    assert "financial history changed" in run("snapshots", succeeds=False)
    run("factors")
    assert factorlab_value(client, days[-2], "sale").get("SEC_KR_999990") == 100
    run("snapshots")
    assert factorlab_value(client, days[-2], "sale", table="fact_daily_factor_snapshot").get("SEC_KR_999990") == 100

    publish_sales(lake, "2020-04-30", 80)
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-2], "sale").get("SEC_KR_999990") == 80
    assert factorlab_value(client, days[-2], "sale", table="fact_daily_factor_snapshot").get("SEC_KR_999990") == 80
    assert "skipping completed step: factors" in run("factors")
    assert "skipping completed step: snapshots" in run("snapshots")


def build_prices(lake, days, *, ratio=None, market="kr"):
    ledger = dict(market=market, as_of=days[-1].strftime("%Y-%m-%d"), events=[])
    if ratio is not None:
        source = lake.bronze("dart", "test_receipts", "split.html")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"Synthetic share-unit change: {ratio}", "utf-8")
        ledger["events"].append(dict(security_id="SEC_KR_999990", effective_date=ledger["as_of"],
            new_shares=str(ratio), old_shares="1", source="DART", source_id="synthetic-share-unit-event",
            source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20200520000001",
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), published_date=ledger["as_of"],
            action_type="split" if ratio > 1 else "reverse_split"))
    path = lake.silver("test_reviews", "split.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), "utf-8")
    program = """
import json, sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE = paths.DataLakePaths(Path(sys.argv[1]))
from engine.workflows.stock_splits import build_split_price_panels, normalized_price_frame
ledger = json.loads(Path(sys.argv[2]).read_text('utf-8'))
market = ledger['market']
result = build_split_price_panels(market, ledger, symbols=['999980','999990'], refresh_us=False)
assert not result['errors'], result['errors']
normalized_price_frame(market, output_path=paths.DATA_LAKE.silver('krx' if market == 'kr' else 'us','price',market+'_normalized_price.csv'))
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root), str(path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ratio, expected", [(2, 50), (.5, 200)])
def test_resumed_refresh_reloads_share_unit_corrections_into_factorlab(share_refresh_environment, ratio, expected):
    lake, client, days, run = share_refresh_environment
    raw = lake.bronze("krx", "price", "kr_999990.csv")
    frame = pd.read_csv(raw)
    frame.loc[len(frame)-1, ["시가", "고가", "저가", "종가"]] = expected
    frame.to_csv(raw, index=False)
    run("normalize")
    build_prices(lake, days)
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-2], "ma_50")["SEC_KR_999990"] == 100
    build_prices(lake, days, ratio=ratio)
    assert "price adjustments changed" in run("snapshots", succeeds=False)
    assert factorlab_value(client, days[-2], "ma_50", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == 100
    run("factors")
    assert factorlab_value(client, days[-2], "ma_50")["SEC_KR_999990"] == expected
    run("snapshots")
    assert factorlab_value(client, days[-2], "ma_50", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == expected
    assert factorlab_value(client, days[-1], "ma_50")["SEC_KR_999980"] == 100
    assert "skipping completed step: factors" in run("factors")
    assert "skipping completed step: snapshots" in run("snapshots")


@pytest.fixture
def us_refresh_environment(share_refresh_environment):
    lake, client, days, _ = share_refresh_environment
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    for symbol in ("999980", "999990"):
        sid = "SEC_US_" + symbol
        client.insert("security_master", [(sid, symbol, "US", True, "NASDAQ")],
            column_names=["security_id", "issuer_id", "country", "is_active", "exchange_code"])
        client.insert("identifiers", [(sid, "TICKER", symbol, True)],
            column_names=["security_id", "id_type", "id_value", "is_primary"])
        client.insert("price_daily", [(sid, day.date(), *([Decimal("100")] * 5), 50, "USD") for day in days[-2:]],
            column_names=["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "currency"])
        source = lake.bronze("alpha-vantage", "price", f"ticker={symbol}.json")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps({"Meta Data": {"2. Symbol": symbol}, "Time Series (Daily)": {
            day.strftime("%Y-%m-%d"): {"1. open": "100", "2. high": "100", "3. low": "100",
                "4. close": "100", "5. adjusted close": "100", "6. volume": "50",
                "7. dividend amount": "0", "8. split coefficient": "1"} for day in days}}), "utf-8")
        history = lake.silver("sec", "normalized", "history", symbol, "manifest.json")
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps(dict(schema_version=1, market="us", symbol=symbol, receipts=[])), "utf-8")
    build_prices(lake, days, market="us")

    def run(mode, *, succeeds=True):
        program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE = paths.DataLakePaths(Path(sys.argv[1]))
from engine.workflows import refresh
args = refresh.build_arg_parser().parse_args(['--market','us','--targets',sys.argv[2],
    '--symbols','999980,999990','--end-date',sys.argv[3],'--financial-basis','annual','--workers','1',
    '--resume','--resume-state-path',str(paths.DATA_LAKE.silver('refresh_state','us_'+sys.argv[2]+'.json'))])
refresh.run_refresh(args)
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(lake.root), mode, days[-1].strftime("%Y%m%d")], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "CLICKHOUSE_DATABASE": database}, text=True, encoding="utf-8", capture_output=True, timeout=120)
        assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
        return result.stdout + result.stderr

    def value(factor, *, snapshot=False):
        day = days[-2].strftime("%Y-%m-%d")
        graph = {"version": 2, "experiment": {"name": "us_refresh", "market": "US", "start_date": day, "end_date": day},
            "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                "config": {"factor_id": factor, "financial_basis": "annual", "missing_policy": "drop"}}],
            "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
        compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day],
            factor_table="fact_daily_factor_snapshot" if snapshot else "fact_daily_factors", listing_table="security_listing_episodes")
        rows = client.query_df(compiled.query, parameters=compiled.parameters)
        return dict(zip(rows.security_id, rows.value)).get("SEC_US_999990") if len(rows) else None

    return lake, days, run, value


def test_us_resumed_refresh_reloads_alpha_vantage_split_corrections(us_refresh_environment):
    lake, days, run, value = us_refresh_environment
    run("factors")
    run("snapshots")
    assert value("ma_50") == value("ma_50", snapshot=True) == 100
    source = lake.bronze("alpha-vantage", "price", "ticker=999990.json")
    payload = json.loads(source.read_text("utf-8"))
    last = payload["Time Series (Daily)"][days[-1].strftime("%Y-%m-%d")]
    last.update({"1. open": "50", "2. high": "50", "3. low": "50", "4. close": "50", "8. split coefficient": "2"})
    source.write_text(json.dumps(payload), "utf-8")
    build_prices(lake, days, market="us")
    assert "price adjustments changed" in run("snapshots", succeeds=False)
    assert value("ma_50", snapshot=True) == 100
    run("factors")
    assert value("ma_50") == 50
    run("snapshots")
    assert value("ma_50", snapshot=True) == 50
    assert "skipping completed step: factors" in run("factors")
    assert "skipping completed step: snapshots" in run("snapshots")


def test_us_resumed_refresh_reloads_changed_financial_history(us_refresh_environment):
    lake, days, run, value = us_refresh_environment
    run("factors")
    run("snapshots")
    assert value("sale") is None
    history = lake.silver("sec", "normalized", "history", "999990")
    facts = history / "2019.csv"
    pd.DataFrame([dict(canonical_account_id="REVENUE", statement_type="IS",
        original_account_name="Revenue", normalized_amount=500)]).to_csv(facts, index=False)
    receipt = dict(rcept_no="synthetic-sec-accession", fiscal_year=2019, fiscal_month=12,
        period_end_date="2019-12-31", report_date="2020-03-31", financial_basis="annual",
        normalized_path=facts.name, normalized_sha256=hashlib.sha256(facts.read_bytes()).hexdigest())
    (history / "manifest.json").write_text(json.dumps(dict(schema_version=1, market="us",
        symbol="999990", receipts=[receipt])), "utf-8")
    assert "financial history changed" in run("snapshots", succeeds=False)
    run("factors")
    assert value("sale") == 500
    run("snapshots")
    assert value("sale", snapshot=True) == 500
    assert "skipping completed step: factors" in run("factors")
    assert "skipping completed step: snapshots" in run("snapshots")
