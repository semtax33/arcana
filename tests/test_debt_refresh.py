"""A partial debt disclosure must not become a complete leverage factor."""
import hashlib
import json

import pandas as pd
import pytest

from engine.workflows.financial_history import publish_reviewed_financial_history
from test_share_input_refresh import share_refresh_environment, factorlab_value


pytestmark = pytest.mark.integration


def publish_debt_statement(lake, *, current=None, noncurrent=None, received="2020-03-31"):
    receipt = received.replace("-", "") + "000001"
    accounts = {"TOTAL_ASSETS": 500, "TOTAL_EQUITY": 200}
    if current is not None:
        accounts["SHORT_TERM_DEBT"] = current
    if noncurrent is not None:
        accounts["LONG_TERM_DEBT"] = noncurrent
    source = lake.bronze("dart", "test_receipts", receipt + ".html")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<DOCUMENT><COMPANY-NAME AREGCIK='00999990'>Synthetic issuer</COMPANY-NAME>"
                      + json.dumps(accounts) + "</DOCUMENT>", "utf-8")
    normalized = lake.silver("test_reviews", receipt + ".csv")
    normalized.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(canonical_account_id=name, statement_type="BS", original_account_name=name,
        period="2019.12", normalized_amount=value) for name, value in accounts.items()]).to_csv(normalized, index=False)
    record = dict(rcept_no=receipt, fiscal_year=2019, fiscal_month=12, period_end_date="2019-12-31",
        period_start_date="2019-01-01", report_date=received, financial_basis="annual", financial_scope="CFS",
        accounting_regime="K_IFRS", review_status="verified", review_evidence="Synthetic independently specified debt balances; an omitted maturity is unknown.",
        accepted_canonical_account_ids=list(accounts), normalized_path=normalized.relative_to(lake.root).as_posix(),
        normalized_sha256=hashlib.sha256(normalized.read_bytes()).hexdigest(),
        source_path=source.relative_to(lake.root).as_posix(), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_url=f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}")
    review = normalized.with_suffix(".json")
    review.write_text(json.dumps(dict(schema_version=1, market="kr", symbol="999990", corp_code="00999990",
        source_root=str(lake.root), receipts=[record])), "utf-8")
    publish_reviewed_financial_history(review, financial_dir=lake.silver("dart", "normalized"))


def test_refresh_abstains_from_leverage_when_current_debt_is_unknown(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    publish_debt_statement(lake, noncurrent=100)
    run("factors")
    assert factorlab_value(client, days[-2], "dltt")["SEC_KR_999990"] == 100
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity")
    run("snapshots")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity", table="fact_daily_factor_snapshot")


def test_refresh_abstains_from_leverage_when_noncurrent_debt_is_unknown(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    publish_debt_statement(lake, current=20)
    run("factors")
    assert factorlab_value(client, days[-2], "dlc")["SEC_KR_999990"] == 20
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity")
    run("snapshots")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity", table="fact_daily_factor_snapshot")


@pytest.mark.parametrize("current,noncurrent,expected", [(0, 100, .5), (20, 0, .1), (20, 100, .6), (0, 0, 0)])
def test_refresh_preserves_disclosed_zero_and_complete_debt_balances(share_refresh_environment, current, noncurrent, expected):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    publish_debt_statement(lake, current=current, noncurrent=noncurrent)
    run("factors")
    assert factorlab_value(client, days[-2], "debt_to_equity")["SEC_KR_999990"] == pytest.approx(expected)
    run("snapshots")
    assert factorlab_value(client, days[-2], "debt_to_equity", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == pytest.approx(expected)


def test_resumed_refresh_revokes_leverage_after_a_debt_component_is_unreviewed(share_refresh_environment):
    lake, client, days, run = share_refresh_environment
    run("normalize")
    publish_debt_statement(lake, current=0, noncurrent=100)
    run("factors")
    run("snapshots")
    assert factorlab_value(client, days[-2], "debt_to_equity", table="fact_daily_factor_snapshot")["SEC_KR_999990"] == .5

    publish_debt_statement(lake, noncurrent=100, received="2020-04-30")
    run("factors")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity")
    run("snapshots")
    assert "SEC_KR_999990" not in factorlab_value(client, days[-2], "debt_to_equity", table="fact_daily_factor_snapshot")
