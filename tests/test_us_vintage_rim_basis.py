"""A quarterly ROE cannot supply the annual-return RIM fallback."""
import pandas as pd
import pytest

from test_us_period_vintage_refresh import ordinary_us_refresh
from test_historical_refresh_resume import us_refresh_environment
from test_share_input_refresh import share_refresh_environment

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("basis,expected_roe", [("quarterly", 10), ("ttm", 40)])
def test_reload_uses_only_an_annual_return_basis_for_historical_rim(ordinary_us_refresh, basis, expected_roe):
    lake, days, publish, run, value = ordinary_us_refresh
    records = []
    for year in range(2014, 2020):
        for quarter in range(1, 5):
            period = pd.Timestamp(year, quarter * 3, 1) + pd.offsets.MonthEnd()
            records.append((year, quarter * 3, str((period + pd.Timedelta(days=45)).date()),
                {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200, "NET_INCOME": quarter * 20}))
    publish(records)
    path = lake.silver("sec", "normalized", "us_normalized_999990.csv")
    facts = pd.read_csv(path)
    facts.loc[facts.canonical_account_id.eq("NET_INCOME"), "statement_type"] = "IS"
    facts.to_csv(path, index=False)
    shares = lake.silver("us", "shares", "us_normalized_shares.csv")
    shares.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(security_id="SEC_US_999990", trade_date=str(days[0].date()),
                       shares=10, market_cap=1000)]).to_csv(shares, index=False)
    run("factors", basis)
    assert value(days[-1], "roe", basis) == pytest.approx(expected_roe)
    assert value(days[-1], "bps", basis) == pytest.approx(20)
    assert value(days[-1], "cost_of_equity", basis) > 0
    native_rim = value(days[-1], "rim_upside_potential", basis)
    if basis == "quarterly":
        assert native_rim is None
    else:
        assert native_rim is not None
    run("snapshots", basis)
    assert value(days[-1], "rim_upside_potential", basis, snapshot=True) == native_rim
