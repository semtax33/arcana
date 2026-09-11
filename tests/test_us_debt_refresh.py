"""US refresh preserves the difference between unknown and explicit zero debt."""
import hashlib
import json

import pandas as pd
import pytest

from test_share_input_refresh import share_refresh_environment
from test_historical_refresh_resume import us_refresh_environment


pytestmark = pytest.mark.integration


def test_us_refresh_distinguishes_unknown_current_debt_from_a_disclosed_zero(us_refresh_environment):
    lake, days, run, value = us_refresh_environment
    history = lake.silver("sec", "normalized", "history", "999990")
    facts = history / "2019.csv"
    accounts = {"TOTAL_ASSETS": 500, "TOTAL_EQUITY": 200, "LONG_TERM_DEBT": 100}

    def publish(received):
        pd.DataFrame([dict(canonical_account_id=name, statement_type="BS", original_account_name=name,
            normalized_amount=amount) for name, amount in accounts.items()]).to_csv(facts, index=False)
        receipt = dict(rcept_no="synthetic-sec-debt-accession", fiscal_year=2019, fiscal_month=12,
            period_end_date="2019-12-31", report_date=received, financial_basis="annual",
            normalized_path=facts.name, normalized_sha256=hashlib.sha256(facts.read_bytes()).hexdigest())
        (history / "manifest.json").write_text(json.dumps(dict(schema_version=1, market="us",
            symbol="999990", receipts=[receipt])), "utf-8")

    publish("2020-03-31")
    run("factors")
    assert value("dltt") == 100
    assert value("debt_to_equity") is None
    run("snapshots")
    assert value("debt_to_equity", snapshot=True) is None

    accounts["SHORT_TERM_DEBT"] = 0
    publish("2020-04-30")
    run("factors")
    assert value("debt_to_equity") == .5
    run("snapshots")
    assert value("debt_to_equity", snapshot=True) == .5
