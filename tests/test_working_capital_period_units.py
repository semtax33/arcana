"""Working-capital days use flow amounts over their declared financial basis."""
import pandas as pd
import pytest

from engine.transformers.factors import add_annual_financial_factors


@pytest.mark.parametrize("periods,annualized,sales,cogs", [(1, False, 1460., 730.), (4, False, 365., 182.5), (4, True, 1460., 730.)])
def test_annual_quarter_and_ttm_flows_describe_the_same_working_capital_days(periods, annualized, sales, cogs):
    # Constant balances and sales of four dollars a day: receivables cover
    # 25 days, inventory 50 days and payables 25 days under a 365-day year.
    source = pd.DataFrame([dict(TOTAL_ASSETS=1000., TOTAL_EQUITY=500., INVENTORIES=100.,
        TRADE_RECEIVABLES=100., TRADE_PAYABLES=50., REVENUE=sales, COGS=cogs)] * 5)
    result = add_annual_financial_factors(source, periods_per_year=periods, annualized_flows=annualized).iloc[-1]
    assert result.ar_days == pytest.approx(25.)
    assert result.inv_days == pytest.approx(50.)
    assert result.ap_days == pytest.approx(25.)
    assert result.ccc == pytest.approx(50.)
