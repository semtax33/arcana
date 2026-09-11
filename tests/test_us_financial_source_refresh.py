"""Changed ordinary SEC inputs propagate through resumed public refreshes."""
import json
from hashlib import sha256

import pandas as pd
import pytest

from test_us_period_vintage_refresh import ordinary_us_refresh
from test_historical_refresh_resume import us_refresh_environment
from test_share_input_refresh import share_refresh_environment, factorlab_value

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("basis,month", [("annual", 12), ("quarterly", 9), ("ttm", 9)])
def test_resumed_refresh_reloads_changed_ordinary_sec_balances(ordinary_us_refresh, basis, month):
    _, days, publish, run, value = ordinary_us_refresh

    def source(long_debt):
        publish([(2019, month, "2020-04-01", {"TOTAL_ASSETS": 400,
            "TOTAL_EQUITY": 200, "LONG_TERM_DEBT": long_debt, "SHORT_TERM_DEBT": 0})])

    source(100)
    run("factors", basis)
    run("snapshots", basis)
    assert value(days[-1], "debt_to_equity", basis) == .5
    assert value(days[-1], "debt_to_equity", basis, snapshot=True) == .5
    source(150)
    run("factors", basis)
    assert value(days[-1], "debt_to_equity", basis) == .75
    run("snapshots", basis)
    assert value(days[-1], "debt_to_equity", basis, snapshot=True) == .75
    assert "skipping completed step: factors" in run("factors", basis)
    assert "skipping completed step: snapshots" in run("snapshots", basis)


def test_corrected_sec_publication_date_clears_premature_native_and_snapshot_values(ordinary_us_refresh):
    lake, days, publish, run, value = ordinary_us_refresh
    publish([(2019, 9, "2020-04-01", {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
                                      "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0})])
    run("factors")
    run("snapshots")
    assert value(days[-2], "debt_to_equity", snapshot=True) == .5
    path = lake.silver("sec", "us_report_metadata.csv")
    metadata = pd.read_csv(path, dtype={"stock_code": str})
    metadata["report_date"] = str(days[-2].date())
    metadata.to_csv(path, index=False)
    assert "financial history changed" in run("snapshots", succeeds=False)
    assert value(days[-2], "debt_to_equity", snapshot=True) == .5
    run("factors")
    assert value(days[-2], "debt_to_equity") is None
    assert value(days[-1], "debt_to_equity") == .5
    run("snapshots")
    assert value(days[-2], "debt_to_equity", snapshot=True) is None
    assert value(days[-1], "debt_to_equity", snapshot=True) == .5


def test_empty_reviewed_history_withdraws_prior_ordinary_sec_values(ordinary_us_refresh):
    lake, days, publish, run, value = ordinary_us_refresh
    publish([(2019, 9, "2020-04-01", {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
                                      "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0})])
    run("factors")
    run("snapshots")
    assert value(days[-1], "debt_to_equity", snapshot=True) == .5
    manifest = lake.silver("sec", "normalized", "history", "999990", "manifest.json")
    manifest.write_text(json.dumps(dict(schema_version=1, market="us", symbol="999990", receipts=[])), "utf-8")
    run("factors")
    assert value(days[-1], "debt_to_equity") is None
    run("snapshots")
    assert value(days[-1], "debt_to_equity", snapshot=True) is None


@pytest.mark.parametrize("withdrawn", ["statements", "metadata"])
def test_withdrawn_sec_input_clears_financial_values_but_preserves_price_factors(ordinary_us_refresh, withdrawn):
    lake, days, publish, run, value = ordinary_us_refresh
    publish([(2019, 9, "2020-04-01", {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
                                      "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0})])
    run("factors")
    run("snapshots")
    assert value(days[-1], "debt_to_equity", snapshot=True) == .5
    path = (lake.silver("sec", "normalized", "us_normalized_999990.csv") if withdrawn == "statements"
            else lake.silver("sec", "us_report_metadata.csv"))
    path.unlink()
    run("factors")
    assert value(days[-1], "debt_to_equity") is None
    assert value(days[-1], "ma_50") == 100
    run("snapshots")
    assert value(days[-1], "debt_to_equity", snapshot=True) is None
    assert value(days[-1], "ma_50", snapshot=True) == 100


def test_other_issuer_metadata_does_not_reopen_completed_financial_refresh(ordinary_us_refresh):
    lake, days, publish, run, value = ordinary_us_refresh
    publish([(2019, 9, "2020-04-01", {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
                                      "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0})])
    run("factors")
    run("snapshots")
    path = lake.silver("sec", "us_report_metadata.csv")
    metadata = pd.read_csv(path, dtype={"stock_code": str})
    unrelated = metadata.assign(stock_code="999980", report_date="2020-04-30", rcept_no="other-issuer")
    pd.concat([metadata, unrelated], ignore_index=True).to_csv(path, index=False)
    assert "skipping completed step: factors" in run("factors")
    assert "skipping completed step: snapshots" in run("snapshots")
    assert value(days[-1], "debt_to_equity") == .5
    assert value(days[-1], "debt_to_equity", snapshot=True) == .5


def test_withdrawn_korean_receipt_history_clears_resumed_factorlab_values(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    history = lake.silver("dart", "normalized", "history", "999990")
    history.mkdir(parents=True, exist_ok=True)
    facts = history / "synthetic.csv"
    pd.DataFrame([dict(canonical_account_id=k, normalized_amount=v, original_account_name=k, statement_type="BS")
        for k, v in {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200, "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}.items()]).to_csv(facts, index=False)
    receipt = dict(rcept_no="synthetic-kr-receipt", fiscal_year=2019, fiscal_month=12,
        period_end_date="2019-12-31", report_date="2020-03-31", financial_basis="annual",
        normalized_path=facts.name, normalized_sha256=sha256(facts.read_bytes()).hexdigest())
    manifest = history / "manifest.json"
    payload = dict(schema_version=1, market="kr", symbol="999990", receipts=[receipt])
    manifest.write_text(json.dumps(payload), "utf-8")
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-1], "debt_to_equity", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == .5
    payload["receipts"] = []
    manifest.write_text(json.dumps(payload), "utf-8")
    run("factors")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-1], "debt_to_equity")
    run("snapshots")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-1], "debt_to_equity", table="fact_daily_factor_snapshot")
