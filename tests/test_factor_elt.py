import unittest

import pandas as pd

from factor_elt import (
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
        catalog_df = create_factor_catalog_dataframe(["roe", "na_20"])

        row_by_id = catalog_df.set_index("factor_id")
        self.assertEqual(row_by_id.loc["roe", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["na_20", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["na_20", "factor_group"], "trend")


if __name__ == "__main__":
    unittest.main()
