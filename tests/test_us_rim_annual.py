"""Regression coverage for the US annual RIM historical-ROE fallback."""

import unittest
from unittest.mock import patch

import pandas as pd

from engine.transformers._internal import factor_metrics


def _identity(daily_df, *args, **kwargs):
    return daily_df


class UsAnnualRimTest(unittest.TestCase):
    def test_eps_consensus_implies_operating_income_surprise_for_fy1_only(self):
        daily = pd.DataFrame(
            {
                "us_eps_consensus": [6.0, 6.0],
                "us_consensus_analyst_count": [4, 4],
                "us_consensus_horizon": ["FY1", "FQ1"],
                "DILUTED_SHARES": [10.0, 10.0],
                "ni_parent": [50.0, 50.0],
                "oiadp": [100.0, 100.0],
            }
        )

        result = factor_metrics.add_eps_implied_operating_income_surprise_factor(
            daily,
            market="us",
        )

        # FY1 implied NI is 60 and the disclosed OI/NI ratio is 2.0,
        # yielding 120 implied OI versus 100 disclosed OI.
        self.assertAlmostEqual(
            result["eps_implied_operating_income_surprise_pct"].iat[0],
            20.0,
        )
        self.assertTrue(
            pd.isna(result["eps_implied_operating_income_surprise_pct"].iat[1])
        )

    def test_us_rim_priority_is_analyst_oi_then_eps_implied_then_historical_roe(self):
        daily = pd.DataFrame(
            {
                "roe": [10.0, 10.0, 10.0],
                "oiadp": [100.0, 100.0, 100.0],
                "us_operating_income_consensus": [130.0, float("nan"), float("nan")],
                "us_consensus_analyst_count": [4, 4, 4],
                "us_consensus_horizon": ["FY1", "FY1", "FY1"],
                "eps_implied_operating_income_surprise_pct": [20.0, 20.0, float("nan")],
                "historical_roe_3y_avg": [15.0, 15.0, 15.0],
                "cost_of_equity": [10.0, 10.0, 10.0],
                "bps": [100.0, 100.0, 100.0],
                "close": [100.0, 100.0, 100.0],
            }
        )

        result = factor_metrics.add_equity_valuation_factors(
            daily,
            market="us",
            rim_decay_factor=0.8,
        )

        self.assertAlmostEqual(result["rim_upside_potential"].iat[0], 0.08)
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[1],
            100 * (0.12 - 0.10) * 0.8 / 0.30 / 100,
        )
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[2],
            100 * (0.15 - 0.10) * 0.8 / 0.30 / 100,
        )

    def test_uses_three_year_historical_roe_fallback(self):
        prices = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10"]),
                "close": [100.0],
            }
        )
        financials = pd.DataFrame(
            {
                "financial_period": pd.to_datetime(
                    ["2023-12-31", "2024-12-31", "2025-12-31"]
                ),
                "report_date": pd.to_datetime(
                    ["2024-02-01", "2025-02-01", "2026-02-01"]
                ),
                "roe": [0.10, 0.11, 0.12],
            }
        )

        def mark_fallback(daily_df, *args, **kwargs):
            self.assertEqual(kwargs, {})
            return daily_df.assign(rim_annual_fallback_used=True)

        with (
            patch.object(factor_metrics, "read_stock_prices", return_value=prices),
            patch.object(factor_metrics, "read_stock_shares", return_value=pd.DataFrame()),
            patch.object(factor_metrics, "read_annual_financials", return_value=financials),
            patch.object(factor_metrics, "add_dividend_factors", side_effect=_identity),
            patch.object(
                factor_metrics, "add_daily_market_valuation_factors", side_effect=_identity
            ),
            patch.object(factor_metrics, "add_consensus_factors", side_effect=_identity),
            patch.object(factor_metrics, "add_real_consensus_factors", side_effect=_identity),
            patch.object(factor_metrics, "add_us_consensus_factors", side_effect=_identity),
            patch.object(factor_metrics, "add_wacc_factors", side_effect=_identity),
            patch.object(
                factor_metrics, "add_rim_historical_roe_fallback", side_effect=mark_fallback
            ) as fallback,
            patch.object(
                factor_metrics, "add_equity_valuation_factors", side_effect=_identity
            ),
            patch.object(factor_metrics, "add_price_momentum_factors", side_effect=_identity),
        ):
            result = factor_metrics.create_stock_factor_dataframe(
                "AAPL",
                market="us",
                financial_basis="annual",
            )

        fallback.assert_called_once()
        self.assertTrue(result["rim_annual_fallback_used"].all())
