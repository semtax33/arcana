import unittest
from unittest.mock import patch

import pandas as pd

from engine.factor_normalizer import (
    add_annual_financial_factors,
    add_daily_market_valuation_factors,
    add_dividend_factors,
    add_price_momentum_factors,
    create_stock_factor_dataframe,
)


class FactorNormalizerTest(unittest.TestCase):
    def test_balance_component_outlier_falls_back_to_bounded_candidate(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TRADE_RECEIVABLES": 10_000_000,
                    "OTHER_RECEIVABLES": 120,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertEqual(result["rect"].iat[0], 120)

    def test_temporal_unit_outlier_is_removed_before_factor_calculation(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2023,
                    "financial_period": "2023-12-31",
                    "TOTAL_ASSETS": 2_000,
                    "EAOP": 1_000,
                    "NET_INCOME": 100,
                },
                {
                    "fiscal_year": 2024,
                    "financial_period": "2024-12-31",
                    "TOTAL_ASSETS": 2_100_000_000,
                    "EAOP": 1_100_000_000,
                    "NET_INCOME": 120_000_000,
                },
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 2_200,
                    "EAOP": 1_200,
                    "NET_INCOME": 120,
                },
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertTrue(pd.isna(result["at"].iat[1]))
        self.assertTrue(pd.isna(result["ceq"].iat[1]))
        self.assertTrue(pd.isna(result["ni_parent"].iat[1]))

    def test_percent_unit_financial_factors_are_stored_as_percent(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2024,
                    "financial_period": "2024-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                    "OPERATING_INCOME": 200,
                    "NET_INCOME": 100,
                    "NET_INCOME_PARENT": 100,
                    "TAX_EXPENSE": 25,
                    "PBT": 100,
                },
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                    "OPERATING_INCOME": 200,
                    "NET_INCOME": 100,
                    "NET_INCOME_PARENT": 100,
                    "TAX_EXPENSE": 25,
                    "PBT": 100,
                },
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[1]

        self.assertAlmostEqual(latest["gpm"], 40.0)
        self.assertAlmostEqual(latest["opm"], 20.0)
        self.assertAlmostEqual(latest["ebitda_margin"], 20.0)
        self.assertAlmostEqual(latest["npm"], 10.0)
        self.assertAlmostEqual(latest["tax_rate"], 25.0)
        self.assertAlmostEqual(latest["roe"], 20.0)

    def test_dividend_payout_factor_is_stored_as_percent(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [50_000],
            }
        )

        with (
            patch(
                "engine.factor_normalizer.calculate_silver_total_dividend_per_share_with_fallback",
                return_value=1_000,
            ),
            patch(
                "engine.factor_normalizer.calculate_silver_payout_ratio_with_fallback",
                return_value=0.25,
            ),
            patch(
                "engine.factor_normalizer.calculate_silver_total_dividend_amount",
                return_value=100_000,
            ),
        ):
            result = add_dividend_factors(daily_df, "005930")
            valued = add_daily_market_valuation_factors(result)

        self.assertEqual(result["tdpr"].iat[0], 25.0)
        self.assertEqual(valued["payout_ratio"].iat[0], 25.0)

    def test_mdd_uses_returns_not_growth_multipliers(self):
        close = [100.0] * 21 + [100.0] * 80 + [50.0] * 80 + [75.0] * 180
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.date_range("2025-01-01", periods=len(close), freq="D"),
                "close": close,
                "high": [value + 1 for value in close],
                "low": [value - 2 for value in close],
                "volume": 1_000,
                "shares": 1_000_000,
            }
        )

        result = add_price_momentum_factors(daily_df)

        self.assertLess(result["mdd1yr_12_1_pct"].dropna().min(), -40)

    def test_price_momentum_adds_standard_technical_indicators(self):
        close = list(range(1, 41))
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.date_range("2025-01-01", periods=len(close), freq="D"),
                "close": close,
                "high": [value + 1 for value in close],
                "low": [value - 2 for value in close],
                "volume": 1_000,
                "shares": 1_000_000,
            }
        )

        result = add_price_momentum_factors(daily_df)
        latest = result.iloc[-1]

        self.assertAlmostEqual(latest["rsi_14"], 100.0)
        self.assertTrue(pd.notna(latest["macd"]))
        self.assertTrue(pd.notna(latest["macd_signal"]))
        self.assertTrue(pd.notna(latest["macd_hist"]))
        self.assertAlmostEqual(latest["bb_middle"], pd.Series(close[-20:]).mean())
        self.assertGreater(latest["bb_upper"], latest["bb_middle"])
        self.assertLess(latest["bb_lower"], latest["bb_middle"])
        self.assertGreater(latest["bb_width_pct"], 0)
        self.assertGreater(latest["bb_percent_b"], 0)
        self.assertAlmostEqual(latest["ma_50"], pd.Series(close).mean())
        self.assertAlmostEqual(latest["ma_120"], pd.Series(close).mean())
        self.assertAlmostEqual(latest["ma_150"], pd.Series(close).mean())
        self.assertAlmostEqual(latest["ma_200"], pd.Series(close).mean())
        self.assertGreater(latest["ati"], 0)
        self.assertLess(latest["williams_r_14"], 0)
        self.assertGreater(latest["williams_r_14"], -10)
        self.assertGreater(latest["cmf_20"], 0)
        self.assertAlmostEqual(latest["mfi_14"], 100.0)

    def test_financials_are_merged_from_report_date_not_period_end(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_KR_005930"] * 4,
                "trade_date": pd.to_datetime(["2025-03-31", "2025-04-14", "2025-04-15", "2025-04-16"]),
                "close": [100, 100, 100, 100],
                "volume": [1_000, 1_000, 1_000, 1_000],
                "currency": ["KRW"] * 4,
            }
        )
        financial_df = pd.DataFrame(
            {
                "stock_code": ["005930"],
                "security_id": ["SEC_KR_005930"],
                "fiscal_year": [2025],
                "financial_period": pd.to_datetime(["2025-03-31"]),
                "report_date": pd.to_datetime(["2025-04-15"]),
                "at": [1_000],
            }
        )

        with (
            patch("engine.factor_normalizer.read_stock_prices", return_value=price_df),
            patch("engine.factor_normalizer.read_stock_shares", return_value=pd.DataFrame()),
            patch("engine.factor_normalizer.read_annual_financials", return_value=financial_df),
            patch("engine.factor_normalizer.add_dividend_factors", side_effect=lambda df, stock_code: df),
            patch("engine.factor_normalizer.add_daily_market_valuation_factors", side_effect=lambda df: df),
            patch("engine.factor_normalizer.add_price_momentum_factors", side_effect=lambda df: df),
        ):
            result = create_stock_factor_dataframe("005930", financial_basis="annual")

        self.assertTrue(pd.isna(result.loc[result["trade_date"] == pd.Timestamp("2025-04-14"), "at"].iat[0]))
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-04-15"), "at"].iat[0], 1_000)
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-04-16"), "at"].iat[0], 1_000)


if __name__ == "__main__":
    unittest.main()
