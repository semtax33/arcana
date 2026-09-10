"""A date-only SEC filing cannot enter historical factors on its filing day."""
from unittest.mock import patch

import pandas as pd

from engine.transformers.factors import create_stock_factor_dataframe


def test_date_only_historical_filing_becomes_available_on_following_day():
    prices = pd.DataFrame({
        "security_id": ["SEC_US_ATVI"] * 3,
        "trade_date": pd.to_datetime(["2015-02-02", "2015-02-03", "2015-02-04"]),
        "close": [20, 21, 22], "volume": [1000] * 3, "currency": ["USD"] * 3,
    })
    financials = pd.DataFrame({
        "stock_code": ["ATVI"], "security_id": ["SEC_US_ATVI"],
        "financial_period": pd.to_datetime(["2014-12-31"]),
        "report_date": pd.to_datetime(["2015-02-03"]), "sale": [500],
    })
    with (
        patch("engine.transformers.factors.read_stock_prices", return_value=prices),
        patch("engine.transformers.factors.read_stock_shares", return_value=pd.DataFrame()),
        patch("engine.transformers.factors.read_annual_financials", return_value=financials),
        patch("engine.transformers.factors.add_dividend_factors", side_effect=lambda df, *a, **kw: df),
        patch("engine.transformers.factors.add_price_momentum_factors", side_effect=lambda df: df),
    ):
        result = create_stock_factor_dataframe(
            "ATVI", market="us", financial_basis="annual", require_report_metadata=True,
            financial_availability_delay_days=1,
        )
    assert result.financial_period.iloc[:2].isna().all()
    assert result.sale.iloc[:2].isna().all()
    assert result.sale.iloc[2] == 500
    assert result.report_date.iloc[2] == pd.Timestamp("2015-02-03")
    assert result.financial_available_date.iloc[2] == pd.Timestamp("2015-02-04")
