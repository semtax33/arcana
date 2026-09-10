"""Unknown operating-capital components cannot be treated as disclosed zero."""
import pandas as pd
import pytest

from engine.transformers.factors import add_annual_financial_factors


@pytest.mark.parametrize("missing", ["TRADE_RECEIVABLES", "INVENTORIES", "TRADE_PAYABLES", "PPE", "INTANGIBLE_ASSETS"])
def test_operational_roic_requires_all_capital_components(missing):
    frame = pd.DataFrame({"financial_period": pd.to_datetime(["2022-12-31", "2023-12-31"]),
        "TOTAL_ASSETS": [2000., 2000.], "TRADE_RECEIVABLES": [100., 100.],
        "INVENTORIES": [100., 100.], "TRADE_PAYABLES": [50., 50.],
        "PPE": [500., 500.], "INTANGIBLE_ASSETS": [50., 50.],
        "OPERATING_INCOME": [200., 200.], "PBT": [200., 200.], "TAX_EXPENSE": [60., 60.]})
    complete = add_annual_financial_factors(frame)
    assert complete.roic_operational.iloc[-1] == pytest.approx(20.)
    frame.loc[1, missing] = float("nan")
    incomplete = add_annual_financial_factors(frame)
    assert pd.isna(incomplete.roic_operational.iloc[-1])
    assert pd.isna(incomplete.roic_operational_growth_1y.iloc[-1])


def test_unknown_free_cash_flow_is_not_counted_as_a_profitable_year():
    frame = pd.DataFrame({"financial_period": pd.to_datetime(["2020-12-31", "2021-12-31", "2022-12-31"]),
        "TOTAL_ASSETS": [1000., 1000., 1000.], "CFO": [float("nan"), -20., float("nan")],
        "CAPEX_PPE": [0., 0., float("nan")]})
    result = add_annual_financial_factors(frame)
    for years in [5, 10]:
        values = result[f"fcf_negative_freq_{years}y_pct"]
        assert pd.isna(values.iloc[0])
        assert values.iloc[1:].tolist() == [100., 100.]
