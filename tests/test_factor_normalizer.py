import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.factor_normalizer import (
    add_annual_financial_factors,
    add_daily_market_valuation_factors,
    add_dividend_factors,
    add_price_momentum_factors,
    create_stock_factor_dataframe,
    read_annual_financials,
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

    def test_dividend_payout_factor_is_empty_without_disclosure_events(self):
        daily_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02"]),
                "close": [50_000],
            }
        )

        with patch(
            "engine.factor_normalizer.silver_dividend_asof_events",
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

        with patch("engine.factor_normalizer.silver_dividend_asof_events", return_value=events):
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

        with patch("engine.factor_normalizer.silver_dividend_asof_events", return_value=events):
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

    def test_read_annual_financials_includes_fiscal_month_for_report_metadata_join(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()
            metadata_path = root / "report_metadata.csv"

            (financial_dir / "normalized_005930_2025.12.csv").write_text(
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

            with patch("engine.factor_normalizer.FINANCIAL_DIR", financial_dir):
                result = read_annual_financials("005930", report_metadata_path=metadata_path)

        self.assertEqual(result["fiscal_month"].iat[0], 12)
        self.assertEqual(result["report_date"].dt.strftime("%Y-%m-%d").iat[0], "2026-03-15")
        self.assertEqual(result["rcept_no"].iat[0], "20260315000001")


if __name__ == "__main__":
    unittest.main()
