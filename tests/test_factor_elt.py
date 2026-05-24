import unittest

import pandas as pd

from engine.factor_elt import (
    FACT_DAILY_FACTOR_COLUMNS,
    create_factor_catalog_dataframe,
    prepare_daily_factor_rows,
)


class FactorEltTest(unittest.TestCase):
    def test_prepare_daily_factor_rows_melts_only_factor_columns(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "stock_code": "005930",
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "close": 75000,
                    "volume": 100,
                    "shares": 10,
                    "market_cap": 750000,
                    "trading_value": 7_500_000,
                    "currency": "KRW",
                    "updated_at": "2026-01-03 09:00:00",
                    "roe": 0.12,
                    "per": 11.5,
                    "na_20": None,
                }
            ]
        )

        result = prepare_daily_factor_rows(wide_df, financial_basis="ttm")

        self.assertEqual(list(result.columns), FACT_DAILY_FACTOR_COLUMNS)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result["factor_id"]), {"per", "roe"})
        self.assertNotIn("close", set(result["factor_id"]))
        self.assertNotIn("volume", set(result["factor_id"]))
        self.assertTrue((result["financial_basis"] == "ttm").all())
        self.assertEqual(result.loc[result["factor_id"] == "roe", "factor_value"].iat[0], 0.12)

    def test_prepare_daily_factor_rows_returns_empty_when_all_values_null(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "roe": None,
                }
            ]
        )

        result = prepare_daily_factor_rows(wide_df, factor_ids=["roe"])

        self.assertEqual(list(result.columns), FACT_DAILY_FACTOR_COLUMNS)
        self.assertTrue(result.empty)

    def test_create_factor_catalog_dataframe_marks_technical_factors(self):
        catalog_df = create_factor_catalog_dataframe(
            [
                "roe",
                "roic_financial",
                "roic_operational",
                "na_20",
                "ma_50",
                "ma_120",
                "ma_150",
                "ma_200",
                "rsi_14",
                "macd",
                "bb_upper",
                "ati",
                "williams_r_14",
                "cmf_20",
                "mfi_14",
            ]
        )

        row_by_id = catalog_df.set_index("factor_id")
        self.assertEqual(row_by_id.loc["roe", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roe", "unit"], "percent")
        self.assertEqual(row_by_id.loc["roic_financial", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roic_financial", "unit"], "percent")
        self.assertEqual(row_by_id.loc["roic_operational", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roic_operational", "unit"], "percent")
        self.assertEqual(row_by_id.loc["na_20", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["na_20", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_50", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["ma_50", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_50", "unit"], "krw")
        self.assertEqual(row_by_id.loc["ma_120", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_150", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_200", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["rsi_14", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["rsi_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["rsi_14", "unit"], "percent")
        self.assertEqual(row_by_id.loc["rsi_14", "value_direction"], "NEUTRAL")
        self.assertEqual(row_by_id.loc["macd", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["macd", "unit"], "krw")
        self.assertEqual(row_by_id.loc["bb_upper", "factor_group"], "volatility")
        self.assertEqual(row_by_id.loc["bb_upper", "unit"], "krw")
        self.assertEqual(row_by_id.loc["ati", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["ati", "factor_group"], "volume")
        self.assertEqual(row_by_id.loc["ati", "unit"], "shares")
        self.assertEqual(row_by_id.loc["williams_r_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["williams_r_14", "unit"], "percent")
        self.assertEqual(row_by_id.loc["cmf_20", "factor_group"], "volume")
        self.assertEqual(row_by_id.loc["cmf_20", "unit"], "ratio")
        self.assertEqual(row_by_id.loc["mfi_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["mfi_14", "unit"], "percent")

    def test_create_factor_catalog_dataframe_registers_requested_factors(self):
        factor_ids = [
            "rnd_margin",
            "fcf_margin",
            "sales_cagr_3y",
            "rnd_to_sales",
            "operating_profit_margin",
            "net_income_growth_1y",
            "net_income_growth_3y",
            "net_income_growth_5y",
            "operating_income_growth_1y",
            "operating_income_growth_3y",
            "operating_income_growth_5y",
            "sales_growth_1y",
            "sales_growth_3y",
            "sales_growth_5y",
            "net_margin",
            "total_asset_turnover",
            "rnd_to_market_cap",
        ]

        catalog_df = create_factor_catalog_dataframe(factor_ids)

        row_by_id = catalog_df.set_index("factor_id")
        self.assertEqual(row_by_id.loc["rnd_margin", "factor_name"], "R&D Margin")
        self.assertEqual(row_by_id.loc["rnd_margin", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["rnd_margin", "unit"], "percent")
        self.assertEqual(row_by_id.loc["fcf_margin", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["sales_cagr_3y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["sales_cagr_3y", "unit"], "percent")
        self.assertEqual(row_by_id.loc["net_income_growth_5y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["operating_income_growth_3y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["sales_growth_5y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["total_asset_turnover", "unit"], "times")
        self.assertEqual(row_by_id.loc["rnd_to_market_cap", "factor_type"], "valuation")
        self.assertEqual(row_by_id.loc["rnd_to_market_cap", "unit"], "percent")
        self.assertTrue(
            (row_by_id.loc[factor_ids, "value_direction"] == "HIGHER_BETTER").all()
        )


if __name__ == "__main__":
    unittest.main()
