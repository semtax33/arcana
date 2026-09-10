"""ROE must use the same ownership population in income and equity."""
import numpy as np
import pandas as pd
import pytest

from engine.transformers.factors import add_annual_financial_factors


@pytest.mark.parametrize("missing", ["parent_income", "current_parent_equity", "prior_parent_equity"])
def test_incomplete_parent_basis_uses_complete_group_income_and_equity(missing):
    frame = pd.DataFrame({"financial_period": pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31"]),
        "TOTAL_ASSETS": [900., 1000., 1100.], "TOTAL_EQUITY": [700., 800., 900.],
        "EAOP": [400., 500., 600.], "NET_INCOME": [70., 80., 90.], "NET_INCOME_PARENT": [40., 50., 60.]})
    column, index = {"parent_income": ("NET_INCOME_PARENT", 2), "current_parent_equity": ("EAOP", 2),
                     "prior_parent_equity": ("EAOP", 1)}[missing]
    frame.loc[index, column] = np.nan
    result = add_annual_financial_factors(frame)
    assert result.roe.iloc[-1] == pytest.approx(90 / 850 * 100)
    assert result.roe_ownership_basis.iloc[-1] == "group"
    if missing != "prior_parent_equity":
        assert pd.isna(result.roe_growth_1y.iloc[-1]), "Changing ownership basis is not ROE growth"


def test_complete_parent_basis_preserves_parent_roe():
    frame = pd.DataFrame({"financial_period": pd.to_datetime(["2022-12-31", "2023-12-31"]),
        "TOTAL_ASSETS": [1000., 1100.], "TOTAL_EQUITY": [800., 900.], "EAOP": [500., 600.],
        "NET_INCOME": [80., 90.], "NET_INCOME_PARENT": [50., 60.]})
    result = add_annual_financial_factors(frame)
    assert result.roe.iloc[-1] == pytest.approx(60 / 550 * 100)
    assert result.roe_ownership_basis.iloc[-1] == "parent"


def test_roe_average_clears_when_ownership_basis_changes():
    from engine.transformers.factors import add_rim_historical_roe_fallback
    history = pd.DataFrame({"financial_period": pd.to_datetime(["2019-12-31", "2020-12-31", "2021-12-31", "2022-12-31"]),
        "report_date": pd.to_datetime(["2020-03-31", "2021-03-31", "2022-03-31", "2023-03-31"]),
        "roe": [10., 20., 30., 40.], "roe_ownership_basis": ["parent", "parent", "parent", "group"]})
    days = pd.DataFrame({"trade_date": pd.to_datetime(["2022-04-01", "2023-04-01"])})
    result = add_rim_historical_roe_fallback(days, history)
    assert result.historical_roe_3y_avg.iloc[0] == 20
    assert pd.isna(result.historical_roe_3y_avg.iloc[1])


@pytest.mark.parametrize("missing", ["current_parent", "prior_parent"])
def test_net_income_growth_compares_one_ownership_basis(missing):
    frame = pd.DataFrame({"financial_period": pd.date_range("2018-12-31", periods=6, freq="YE"),
        "TOTAL_ASSETS": [1000.] * 6, "NET_INCOME": [100.] * 5 + [200.],
        "NET_INCOME_PARENT": [20.] * 5 + [60.]})
    if missing == "current_parent":
        frame.loc[5, "NET_INCOME_PARENT"] = np.nan
    else:
        frame.loc[:4, "NET_INCOME_PARENT"] = np.nan
    result = add_annual_financial_factors(frame)
    for years in (1, 3, 5):
        assert result[f"net_income_growth_{years}y"].iloc[-1] == pytest.approx(100.)
