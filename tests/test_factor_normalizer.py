import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.transformers.factors import (
    FactorMarketDataCache,
    add_annual_financial_factors,
    add_daily_market_valuation_factors,
    add_dividend_factors,
    add_price_momentum_factors,
    create_stock_factor_dataframe,
    read_annual_financials,
    read_quarterly_financials,
    read_stock_dividends,
    read_stock_prices,
    read_stock_shares,
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
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                    "OPERATING_INCOME": 200,
                    "DNA_IS": 0,
                    "NET_INCOME": 100,
                    "NET_INCOME_PARENT": 100,
                    "TAX_EXPENSE": 25,
                    "PBT": 100,
                },
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                    "OPERATING_INCOME": 200,
                    "DNA_IS": 0,
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
        self.assertAlmostEqual(latest["roic_financial"], 30.0)
        self.assertAlmostEqual(latest["roic_operational"], 30.0)

    def test_ebitda_uses_reported_value_before_calculated_fallback(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                    "DNA_IS": 50,
                    "EBITDA": 180,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertEqual(result["oibdp"].iat[0], 180)
        self.assertAlmostEqual(result["ebitda_margin"].iat[0], 18.0)

    def test_ebitda_fallback_requires_real_depreciation_source(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                    "DEPRECIATION_EXPENSE": 30,
                    "AMORTIZATION": 20,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertEqual(result["dp"].iat[0], 50)
        self.assertEqual(result["oibdp"].iat[0], 150)
        self.assertAlmostEqual(result["ebitda_margin"].iat[0], 15.0)

    def test_ebitda_is_missing_without_reported_value_or_depreciation_source(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertTrue(pd.isna(result["dp"].iat[0]))
        self.assertTrue(pd.isna(result["oibdp"].iat[0]))
        self.assertTrue(pd.isna(result["ebitda_margin"].iat[0]))

    def test_nopat_uses_historical_tax_rate_without_imputing_rnd(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2024,
                    "financial_period": "2024-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                    "NET_INCOME": 75,
                    "NET_INCOME_PARENT": 75,
                    "TAX_EXPENSE": 25,
                    "PBT": 100,
                    "RND": 10,
                },
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 200,
                    "NET_INCOME": 100,
                    "NET_INCOME_PARENT": 100,
                    "TAX_EXPENSE": 25,
                    "PBT": -100,
                },
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[-1]

        self.assertTrue(pd.isna(latest["tax_rate"]))
        self.assertAlmostEqual(latest["nopat"], 150.0)
        self.assertAlmostEqual(latest["roic_financial"], 30.0)
        self.assertAlmostEqual(latest["roic_operational"], 30.0)
        self.assertTrue(pd.isna(latest["xrd"]))
        self.assertTrue(pd.isna(latest["rnd_margin"]))

    def test_nopat_uses_statutory_tax_rate_when_no_reported_rate_exists(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                    "NET_INCOME": 80,
                    "NET_INCOME_PARENT": 80,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[-1]

        self.assertTrue(pd.isna(latest["tax_rate"]))
        self.assertAlmostEqual(latest["nopat"], 79.0)

    def test_operating_income_falls_back_to_gross_profit_less_sgna(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                    "SGNA": 150,
                    "TAX_EXPENSE": 25,
                    "PBT": 100,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertAlmostEqual(result["oiadp"].iat[0], 250.0)
        self.assertEqual(result["operating_income_source"].iat[0], "derived_operating_income")
        self.assertAlmostEqual(result["nopat"].iat[0], 187.5)
        self.assertEqual(result["nopat_quality_flag"].iat[0], "derived_operating_income")

    def test_reported_operating_income_is_not_overwritten_by_fallback(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 200,
                    "GROSS_PROFIT": 400,
                    "SGNA": 50,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertAlmostEqual(result["oiadp"].iat[0], 200.0)
        self.assertEqual(result["operating_income_source"].iat[0], "reported_operating_income")

    def test_operating_income_stays_missing_when_fallback_inputs_are_incomplete(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "PPE": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "GROSS_PROFIT": 400,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertTrue(pd.isna(result["oiadp"].iat[0]))
        self.assertTrue(pd.isna(result["nopat"].iat[0]))
        self.assertEqual(result["operating_income_source"].iat[0], "missing_operating_income")
        self.assertEqual(result["nopat_quality_flag"].iat[0], "missing_operating_income")

    def test_missing_rnd_is_imputed_zero_for_non_rnd_intensive_sector(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "sector_code": "40",
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertEqual(result["xrd"].iat[0], 0)
        self.assertTrue(bool(result["xrd_imputed_zero"].iat[0]))
        self.assertEqual(result["rnd_margin"].iat[0], 0)

    def test_missing_rnd_stays_missing_for_rnd_intensive_sector(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "sector_code": "45",
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                }
            ]
        )

        result = add_annual_financial_factors(financial_df)

        self.assertTrue(pd.isna(result["xrd"].iat[0]))
        self.assertFalse(bool(result["xrd_imputed_zero"].iat[0]))
        self.assertTrue(pd.isna(result["rnd_margin"].iat[0]))

    def test_requested_growth_and_margin_factors_are_calculated(self):
        revenues = [100, 120, 144, 172.8, 207.36, 248.832]
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2020 + index,
                    "financial_period": f"{2020 + index}-12-31",
                    "TOTAL_ASSETS": 1_000 + index * 100,
                    "TOTAL_EQUITY": 500,
                    "EAOP": 500,
                    "REVENUE": revenue,
                    "OPERATING_INCOME": 10 * (index + 1),
                    "NET_INCOME": [-10, 10, 20, 30, 40, 50][index],
                    "NET_INCOME_PARENT": [-10, 10, 20, 30, 40, 50][index],
                    "RND": 10,
                    "CFO": 40,
                    "CAPEX_PPE": 10,
                    "PBT": 100,
                    "TAX_EXPENSE": 25,
                }
                for index, revenue in enumerate(revenues)
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[-1]

        self.assertAlmostEqual(latest["rnd_margin"], 10 / revenues[-1] * 100)
        self.assertAlmostEqual(latest["rnd_to_sales"], 10 / revenues[-1] * 100)
        self.assertAlmostEqual(latest["fcf_margin"], 30 / revenues[-1] * 100)
        self.assertAlmostEqual(latest["operating_profit_margin"], latest["opm"])
        self.assertAlmostEqual(latest["net_margin"], latest["npm"])
        self.assertAlmostEqual(latest["total_asset_turnover"], latest["asset_turnover"])
        self.assertAlmostEqual(latest["accrual_ratio"], (50 - 40) / ((1_500 + 1_400) / 2))
        self.assertAlmostEqual(latest["sales_growth_1y"], 20.0)
        self.assertAlmostEqual(latest["sales_growth_3y"], 72.8)
        self.assertAlmostEqual(latest["sales_growth_5y"], 148.832)
        self.assertAlmostEqual(latest["sales_cagr_3y"], 20.0)
        self.assertAlmostEqual(latest["net_income_growth_1y"], 25.0)
        self.assertAlmostEqual(latest["net_income_growth_3y"], 150.0)
        self.assertAlmostEqual(latest["net_income_growth_5y"], 600.0)
        self.assertAlmostEqual(latest["operating_income_growth_1y"], 20.0)
        self.assertAlmostEqual(latest["operating_income_growth_3y"], 100.0)
        self.assertAlmostEqual(latest["operating_income_growth_5y"], 500.0)

    def test_roe_growth_factors_are_calculated(self):
        net_income_parent = [100, 100, 110, 120, 130, 140, 150]
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2020 + index,
                    "financial_period": f"{2020 + index}-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "EAOP": 500,
                    "REVENUE": 1_000,
                    "OPERATING_INCOME": 100,
                    "NET_INCOME": value,
                    "NET_INCOME_PARENT": value,
                }
                for index, value in enumerate(net_income_parent)
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[-1]

        self.assertAlmostEqual(latest["roe"], 30.0)
        self.assertAlmostEqual(latest["roe_growth_1y"], (30.0 - 28.0) / 28.0 * 100)
        self.assertAlmostEqual(latest["roe_growth_3y"], 25.0)
        self.assertAlmostEqual(latest["roe_growth_5y"], 50.0)

    def test_fcf_dividend_sustainability_factors_are_calculated(self):
        financial_df = pd.DataFrame(
            [
                {
                    "fiscal_year": 2021 + index,
                    "financial_period": f"{2021 + index}-12-31",
                    "TOTAL_ASSETS": 1_000,
                    "TOTAL_EQUITY": 500,
                    "CASH_AND_EQUIVALENTS": 50,
                    "LONG_TERM_DEBT": 300,
                    "REVENUE": 1_000,
                    "NET_INCOME": 100,
                    "NET_INCOME_PARENT": 100,
                    "OPERATING_INCOME": 150,
                    "EBITDA": 100,
                    "CFO": 100,
                    "CAPEX_PPE": 20,
                    "DIV_PAID": 20,
                    "DEBT_ISSUE": 30,
                    "DEBT_REPAY": 20,
                    "BUYBACK": 30,
                    "EQ_ISSUE": 10,
                    "INT_PAID": 5,
                    "PBT": 100,
                    "TAX_EXPENSE": 25,
                }
                for index in range(5)
            ]
        )

        result = add_annual_financial_factors(financial_df)
        latest = result.iloc[-1]

        self.assertEqual(latest["fcf"], 80)
        self.assertEqual(latest["fcfe"], 90)
        self.assertAlmostEqual(latest["fcf_payout_ratio"], 25.0)
        self.assertAlmostEqual(latest["fcf_dividend_coverage"], 4.0)
        self.assertAlmostEqual(latest["fcf_after_dividends"], 60.0)
        self.assertAlmostEqual(latest["shareholder_return_fcf_coverage"], 2.0)
        self.assertAlmostEqual(latest["fcfe_payout_ratio"], 20 / 90 * 100)
        self.assertAlmostEqual(latest["capex_to_sales_pct"], 2.0)
        self.assertAlmostEqual(latest["capex_to_cfo_pct"], 20.0)
        self.assertAlmostEqual(latest["net_debt_to_ebitda"], 250 / 100)
        self.assertAlmostEqual(latest["net_debt_to_fcf"], 250 / 80)
        self.assertAlmostEqual(latest["interest_expense_to_fcf_pct"], 5 / 80 * 100)
        self.assertAlmostEqual(latest["fcf_interest_coverage"], 16.0)
        self.assertAlmostEqual(latest["fcf_negative_freq_5y_pct"], 0.0)

    def test_rnd_to_market_cap_factor_is_percent_of_market_cap(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [10],
                "volume": [100],
                "shares": [100],
                "market_cap": [1_000],
                "xrd": [50],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertAlmostEqual(result["rpr"].iat[0], 0.05)
        self.assertAlmostEqual(result["rnd_to_market_cap"].iat[0], 5.0)

    def test_ev_to_ebitda_requires_positive_ev_and_nonzero_ebitda(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"]),
                "close": [10, 10, 10],
                "volume": [100, 100, 100],
                "shares": [100, 100, 100],
                "market_cap": [1_000, 1_000, 1_000],
                "debt": [200, 200, 200],
                "che": [100, 1_500, 100],
                "oibdp": [100, 100, 0],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertEqual(result["enterprise_value"].iat[0], 1_100)
        self.assertAlmostEqual(result["ev_to_ebitda"].iat[0], 11.0)
        self.assertTrue(pd.isna(result["ev_to_ebitda"].iat[1]))
        self.assertEqual(result["ev_ebitda_quality_flag"].iat[1], "non_positive_enterprise_value")
        self.assertTrue(pd.isna(result["ev_to_ebitda"].iat[2]))
        self.assertEqual(result["ev_ebitda_quality_flag"].iat[2], "zero_ebitda")

    def test_negative_ebitda_ev_ratio_is_retained_but_flagged(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [10],
                "volume": [100],
                "shares": [100],
                "market_cap": [1_000],
                "oibdp": [-100],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertAlmostEqual(result["ev_to_ebitda"].iat[0], -10.0)
        self.assertEqual(result["ev_ebitda_quality_flag"].iat[0], "negative_ebitda")

    def test_ev_ebitda_flags_missing_inputs(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
                "close": [10, 10],
                "volume": [100, 100],
                "shares": [100, 100],
                "market_cap": [1_000, 1_000],
                "oibdp": [100, None],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertEqual(result["ev_ebitda_quality_flag"].iat[0], "missing_enterprise_value_inputs")
        self.assertEqual(result["ev_ebitda_quality_flag"].iat[1], "missing_ebitda")

    def test_ev_to_nopat_requires_positive_ev_and_positive_nopat(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    ["2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05", "2025-01-06"]
                ),
                "close": [10, 10, 10, 10, 10],
                "volume": [100, 100, 100, 100, 100],
                "shares": [100, 100, 100, 100, 100],
                "market_cap": [1_000, 1_000, 1_000, 1_000, 1_000],
                "debt": [200, 200, 200, 200, 200],
                "che": [100, 100, 100, 1_500, 100],
                "nopat": [100, 0, -50, 100, None],
                "oibdp": [100, 100, 100, 100, 100],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertAlmostEqual(result["ev_to_nopat"].iat[0], 11.0)
        self.assertTrue(pd.isna(result["ev_to_nopat"].iat[1]))
        self.assertEqual(result["ev_nopat_quality_flag"].iat[1], "zero_nopat")
        self.assertTrue(pd.isna(result["ev_to_nopat"].iat[2]))
        self.assertEqual(result["ev_nopat_quality_flag"].iat[2], "negative_nopat")
        self.assertTrue(pd.isna(result["ev_to_nopat"].iat[3]))
        self.assertEqual(result["ev_nopat_quality_flag"].iat[3], "non_positive_enterprise_value")
        self.assertTrue(pd.isna(result["ev_to_nopat"].iat[4]))
        self.assertEqual(result["ev_nopat_quality_flag"].iat[4], "missing_nopat")

    def test_ev_to_nopat_flags_market_cap_only_enterprise_value_inputs(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [10],
                "volume": [100],
                "shares": [100],
                "market_cap": [1_000],
                "nopat": [100],
                "oibdp": [100],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertAlmostEqual(result["enterprise_value"].iat[0], 1_000.0)
        self.assertAlmostEqual(result["ev_to_nopat"].iat[0], 10.0)
        self.assertEqual(result["ev_nopat_quality_flag"].iat[0], "missing_enterprise_value_inputs")

    def test_daily_fcf_shareholder_return_factors_use_cash_dividends(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [100],
                "volume": [100],
                "shares": [10],
                "market_cap": [1_000],
                "fcf": [200],
                "fcfe": [220],
                "sale": [1_000],
                "at": [5_000],
                "total_dividend_amount": [50],
                "sharehold_div_yield": [2],
                "tdpr": [25],
                "dvpsx": [2],
                "eps": [5],
                "prstkc": [30],
                "sstk": [10],
            }
        )

        result = add_daily_market_valuation_factors(daily_df)

        self.assertAlmostEqual(result["fcf_yield"].iat[0], 20.0)
        self.assertAlmostEqual(result["fcf_payout_ratio"].iat[0], 25.0)
        self.assertAlmostEqual(result["fcf_dividend_coverage"].iat[0], 4.0)
        self.assertAlmostEqual(result["fcf_after_dividends"].iat[0], 150.0)
        self.assertAlmostEqual(result["fcf_after_dividends_to_market_cap_pct"].iat[0], 15.0)
        self.assertAlmostEqual(result["shareholder_return_fcf_coverage"].iat[0], 200 / 70)
        self.assertAlmostEqual(result["fcfe_payout_ratio"].iat[0], 50 / 220 * 100)
        self.assertAlmostEqual(result["fcf_yield_dividend_yield_spread"].iat[0], 18.0)
        self.assertAlmostEqual(result["eps_dividend_coverage"].iat[0], 2.5)

    def test_dividend_payout_factor_is_empty_without_disclosure_events(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [50_000],
            }
        )

        with patch(
            "engine.transformers.factors.silver_dividend_asof_events",
            return_value=pd.DataFrame(),
        ):
            result = add_dividend_factors(daily_df, "005930")
            valued = add_daily_market_valuation_factors(result)

        self.assertTrue(pd.isna(result["tdpr"].iat[0]))
        self.assertTrue(pd.isna(valued["payout_ratio"].iat[0]))

    def test_dividend_factors_are_merged_from_disclosure_date(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-03-14", "2025-03-15", "2025-03-16"]),
                "close": [50_000, 50_000, 50_000],
            }
        )
        events = pd.DataFrame(
            {
                "report_date": pd.to_datetime(["2025-03-15"]),
                "annual_dividend_per_share": [1_000],
                "payout_ratio": [0.25],
                "total_dividend_amount": [100_000],
            }
        )

        with patch("engine.transformers.factors.silver_dividend_asof_events", return_value=events):
            result = add_dividend_factors(daily_df, "005930")

        self.assertTrue(pd.isna(result.loc[result["trade_date"] == pd.Timestamp("2025-03-14"), "dvpsx"].iat[0]))
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-03-15"), "dvpsx"].iat[0], 1_000)
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-03-16"), "tdpr"].iat[0], 25.0)

    def test_dividend_payout_factor_is_stored_as_percent_after_disclosure(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-03-16"]),
                "close": [50_000],
            }
        )
        events = pd.DataFrame(
            {
                "report_date": pd.to_datetime(["2025-03-15"]),
                "annual_dividend_per_share": [1_000],
                "payout_ratio": [0.25],
                "total_dividend_amount": [100_000],
            }
        )

        with patch("engine.transformers.factors.silver_dividend_asof_events", return_value=events):
            result = add_dividend_factors(daily_df, "005930")
            valued = add_daily_market_valuation_factors(result)

        self.assertEqual(result["tdpr"].iat[0], 25.0)
        self.assertEqual(valued["payout_ratio"].iat[0], 25.0)

    def test_us_dividend_factors_are_built_from_silver_dividend_events(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
                "close": [100, 100],
                "shares": [10, 10],
            }
        )
        events = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "dividend": [1.0],
                "payout_ratio": [None],
                "dividend_percent": [1.0],
            }
        )

        with patch("engine.transformers.factors.read_stock_dividends", return_value=events):
            result = add_dividend_factors(daily_df, "AAPL", market="us")

        self.assertEqual(result["dvpsx"].iat[0], 1.0)
        self.assertEqual(result["dvpsx"].iat[1], 1.0)
        self.assertAlmostEqual(result["sharehold_div_yield"].iat[1], 1.0)
        self.assertEqual(result["total_dividend_amount"].iat[1], 10.0)

    def test_us_dividend_factors_use_sec_event_payout_ratio(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
                "close": [100, 100],
                "shares": [10, 10],
            }
        )
        events = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "dividend": [1.0],
                "payout_ratio": [0.25],
                "dividend_percent": [pd.NA],
            }
        )

        with patch("engine.transformers.factors.read_stock_dividends", return_value=events):
            result = add_dividend_factors(daily_df, "AAPL", market="us")

        self.assertEqual(result["tdpr"].iat[0], 25.0)
        self.assertEqual(result["earnings_payout_ratio"].iat[1], 25.0)

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
            patch("engine.transformers.factors.read_stock_prices", return_value=price_df),
            patch("engine.transformers.factors.read_stock_shares", return_value=pd.DataFrame()),
            patch("engine.transformers.factors.read_annual_financials", return_value=financial_df),
            patch("engine.transformers.factors.add_dividend_factors", side_effect=lambda df, stock_code, **kwargs: df),
            patch("engine.transformers.factors.add_daily_market_valuation_factors", side_effect=lambda df: df),
            patch("engine.transformers.factors.add_price_momentum_factors", side_effect=lambda df: df),
        ):
            result = create_stock_factor_dataframe("005930", financial_basis="annual")

        self.assertTrue(pd.isna(result.loc[result["trade_date"] == pd.Timestamp("2025-04-14"), "at"].iat[0]))
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-04-15"), "at"].iat[0], 1_000)
        self.assertEqual(result.loc[result["trade_date"] == pd.Timestamp("2025-04-16"), "at"].iat[0], 1_000)

    def test_market_cap_is_derived_from_close_and_shares_when_share_file_lacks_market_cap(self):
        price_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [20],
                "volume": [1_000],
                "currency": ["USD"],
            }
        )
        shares_df = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2025-01-01"]),
                "shares": [100],
                "market_cap": [pd.NA],
            }
        )
        financial_df = pd.DataFrame(
            {
                "stock_code": ["AAPL"],
                "security_id": ["SEC_US_AAPL"],
                "financial_period": pd.to_datetime(["2024-12-31"]),
                "report_date": pd.to_datetime(["2025-01-01"]),
                "oibdp": [200],
            }
        )

        with (
            patch("engine.transformers.factors.read_stock_prices", return_value=price_df),
            patch("engine.transformers.factors.read_stock_shares", return_value=shares_df),
            patch("engine.transformers.factors.read_annual_financials", return_value=financial_df),
            patch("engine.transformers.factors.add_dividend_factors", side_effect=lambda df, stock_code, **kwargs: df),
            patch("engine.transformers.factors.add_price_momentum_factors", side_effect=lambda df: df),
        ):
            result = create_stock_factor_dataframe("AAPL", financial_basis="annual", market="us")

        self.assertEqual(result["market_cap"].iat[0], 2_000)
        self.assertEqual(result["enterprise_value"].iat[0], 2_000)
        self.assertAlmostEqual(result["ev_to_ebitda"].iat[0], 10.0)

    def test_create_stock_factor_dataframe_skips_financial_read_when_prices_are_missing(self):
        with (
            patch("engine.transformers.factors.read_stock_prices", return_value=pd.DataFrame()),
            patch("engine.transformers.factors.read_stock_shares") as read_shares,
            patch("engine.transformers.factors.read_annual_financials") as read_financials,
        ):
            result = create_stock_factor_dataframe("005930", financial_basis="annual")

        self.assertTrue(result.empty)
        read_shares.assert_not_called()
        read_financials.assert_not_called()

    def test_read_annual_financials_includes_fiscal_month_for_report_metadata_join(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()
            metadata_path = root / "report_metadata.csv"

            (financial_dir / "kr_normalized_005930_2025.12.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount",
                        "TOTAL_ASSETS,Assets,Assets,BS,2025.12,1000",
                    ]
                ),
                encoding="utf-8",
            )
            metadata_path.write_text(
                "\n".join(
                    [
                        "security_id,stock_code,fiscal_year,fiscal_month,period_end_date,report_date,rcept_no,report_name,source_type,source_url,updated_at",
                        "SEC_KR_005930,005930,2025,12,2025-12-31,2026-03-15,20260315000001,Annual,statement,https://dart.example/1,2026-05-22 09:00:00",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("engine.transformers.factors.FINANCIAL_DIR", financial_dir):
                result = read_annual_financials("005930", report_metadata_path=metadata_path)

        self.assertEqual(result["fiscal_month"].iat[0], 12)
        self.assertEqual(result["report_date"].dt.strftime("%Y-%m-%d").iat[0], "2026-03-15")
        self.assertEqual(result["rcept_no"].iat[0], "20260315000001")

    def test_read_annual_financials_prefers_consolidated_symbol_file(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()

            (financial_dir / "kr_normalized_005930.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount,fiscal_year,fiscal_month,fiscal_quarter",
                        "TOTAL_ASSETS,Assets,Assets,BS,2024.12,900,2024,12,4",
                        "TOTAL_ASSETS,Assets,Assets,BS,2025.12,1000,2025,12,4",
                    ]
                ),
                encoding="utf-8",
            )

            result = read_annual_financials("005930", financial_dir=financial_dir)

        self.assertEqual(result["fiscal_year"].tolist(), [2024, 2025])
        self.assertEqual(result["at"].tolist(), [900, 1000])

    def test_us_annual_financials_fill_missing_factor_inputs_from_edgartools(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()

            (financial_dir / "us_normalized_AAPL.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount,fiscal_year,fiscal_month,fiscal_quarter",
                        "REVENUE,Revenue,Revenue,IS,2025.12,1000,2025,12,4",
                        "OPERATING_INCOME,Operating Income,Operating Income,IS,2025.12,100,2025,12,4",
                    ]
                ),
                encoding="utf-8",
            )

            def provider(symbol, cik, entity_name, rules, start_year, end_year):
                return [
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "entity_name": entity_name,
                        "canonical_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "value": 50,
                        "period_end": "2025-12-31",
                        "filed": "2026-02-01",
                        "accn": "0000320193-26-000001",
                        "form": "10-K",
                        "fp": "FY",
                        "tag": "ResearchAndDevelopmentExpense",
                        "amount_policy": "abs",
                    }
                ]

            result = read_annual_financials(
                "AAPL",
                financial_dir=financial_dir,
                report_metadata_path=root / "missing_metadata.csv",
                market="us",
                edgartools_provider=provider,
            )

        self.assertEqual(result["xrd"].iat[0], 50)
        self.assertAlmostEqual(result["rnd_margin"].iat[0], 5.0)

    def test_us_quarterly_financials_periodize_edgartools_fallback_values(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()

            def provider(symbol, cik, entity_name, rules, start_year, end_year):
                return [
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "entity_name": entity_name,
                        "canonical_id": "REVENUE",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 3,
                        "value": 100,
                        "period_end": "2025-03-31",
                        "filed": "2025-04-25",
                        "accn": "0000320193-25-000001",
                        "form": "10-Q",
                        "fp": "Q1",
                        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
                    },
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "entity_name": entity_name,
                        "canonical_id": "REVENUE",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 6,
                        "value": 250,
                        "period_end": "2025-06-30",
                        "filed": "2025-07-25",
                        "accn": "0000320193-25-000002",
                        "form": "10-Q",
                        "fp": "Q2",
                        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
                    },
                ]

            result = read_quarterly_financials(
                "AAPL",
                financial_dir=financial_dir,
                report_metadata_path=root / "missing_metadata.csv",
                market="us",
                edgartools_provider=provider,
            )

        self.assertEqual(result["financial_period"].dt.strftime("%Y-%m-%d").tolist(), ["2025-03-31", "2025-06-30"])
        self.assertEqual(result["sale"].tolist(), [100, 150])

    def test_market_data_cache_matches_stock_readers(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            price_path = root / "prices.csv"
            shares_path = root / "shares.csv"
            dividend_path = root / "dividends.csv"

            price_path.write_text(
                "\n".join(
                    [
                        "security_id,trade_date,open,high,low,close,volume,adj_close",
                        "SEC_US_AAPL,2026-01-02,10,11,9,10,100,10",
                        "SEC_US_MSFT,2026-01-02,20,21,19,20,200,20",
                        "SEC_US_AAPL,2026-01-03,12,13,11,12,120,12",
                    ]
                ),
                encoding="utf-8",
            )
            shares_path.write_text(
                "\n".join(
                    [
                        "security_id,trade_date,shares,market_cap",
                        "SEC_US_AAPL,2026-01-02,1000,10000",
                        "SEC_US_MSFT,2026-01-02,2000,40000",
                    ]
                ),
                encoding="utf-8",
            )
            dividend_path.write_text(
                "\n".join(
                    [
                        "security_id,trade_date,dividend,payout_ratio,dividend_percent",
                        "SEC_US_AAPL,2026-01-02,0.2,0.3,2",
                        "SEC_US_MSFT,2026-01-02,0.1,0.2,1",
                    ]
                ),
                encoding="utf-8",
            )

            cache = FactorMarketDataCache(
                market="us",
                price_path=price_path,
                shares_path=shares_path,
                dividend_path=dividend_path,
            )

            pd.testing.assert_frame_equal(
                cache.prices("SEC_US_AAPL", stock_code="AAPL"),
                read_stock_prices("AAPL", path=price_path, market="us"),
            )
            pd.testing.assert_frame_equal(
                cache.shares("SEC_US_AAPL"),
                read_stock_shares("AAPL", path=shares_path, market="us"),
            )
            pd.testing.assert_frame_equal(
                cache.dividends("SEC_US_AAPL"),
                read_stock_dividends("AAPL", path=dividend_path, market="us"),
            )

    def test_market_data_cache_filters_dates_with_warmup_and_handles_missing_files(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            price_path = root / "prices.csv"
            missing_path = root / "missing.csv"

            price_path.write_text(
                "\n".join(
                    [
                        "security_id,trade_date,close,volume",
                        "SEC_US_AAPL,2025-12-31,9,90",
                        "SEC_US_AAPL,2026-01-01,10,100",
                        "SEC_US_AAPL,2026-01-02,11,110",
                        "SEC_US_AAPL,2026-01-03,12,120",
                        "SEC_US_AAPL,2026-01-04,13,130",
                    ]
                ),
                encoding="utf-8",
            )

            cache = FactorMarketDataCache(
                market="us",
                price_path=price_path,
                shares_path=missing_path,
                dividend_path=missing_path,
                start_date="2026-01-02",
                end_date="2026-01-03",
                start_warmup_days=1,
            )

            result = cache.prices("SEC_US_AAPL", stock_code="AAPL")

        self.assertEqual(result["trade_date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertTrue(cache.shares("SEC_US_AAPL").empty)
        self.assertTrue(cache.dividends("SEC_US_AAPL").empty)


if __name__ == "__main__":
    unittest.main()
