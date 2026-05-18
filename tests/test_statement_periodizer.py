import unittest

import pandas as pd

from engine.statement_periodizer import add_quarter_and_ttm_amounts


class StatementPeriodizerTest(unittest.TestCase):
    def test_flow_statement_defaults_to_cumulative_ytd(self):
        snapshot_df = pd.DataFrame(
            [
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 3,
                    "financial_period": "2025-03-31",
                    "NET_INCOME": 10,
                    "TOTAL_EQUITY": 100,
                    "_fs_type_by_id": {"NET_INCOME": "IS", "TOTAL_EQUITY": "BS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 6,
                    "financial_period": "2025-06-30",
                    "NET_INCOME": 25,
                    "TOTAL_EQUITY": 120,
                    "_fs_type_by_id": {"NET_INCOME": "IS", "TOTAL_EQUITY": "BS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 9,
                    "financial_period": "2025-09-30",
                    "NET_INCOME": 45,
                    "TOTAL_EQUITY": 140,
                    "_fs_type_by_id": {"NET_INCOME": "IS", "TOTAL_EQUITY": "BS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 12,
                    "financial_period": "2025-12-31",
                    "NET_INCOME": 80,
                    "TOTAL_EQUITY": 160,
                    "_fs_type_by_id": {"NET_INCOME": "IS", "TOTAL_EQUITY": "BS"},
                },
            ]
        )

        result = add_quarter_and_ttm_amounts(snapshot_df)

        self.assertEqual(result["NET_INCOME_quarter"].tolist(), [10, 15, 20, 35])
        self.assertEqual(result["NET_INCOME_ttm"].iloc[-1], 80)
        self.assertEqual(result["TOTAL_EQUITY_quarter"].tolist(), [100, 120, 140, 160])
        self.assertEqual(result["TOTAL_EQUITY_ttm"].tolist(), [100, 120, 140, 160])

    def test_cumulative_statement_types_override_is_preserved(self):
        snapshot_df = pd.DataFrame(
            [
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 3,
                    "financial_period": "2025-03-31",
                    "NET_INCOME": 10,
                    "_fs_type_by_id": {"NET_INCOME": "IS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 6,
                    "financial_period": "2025-06-30",
                    "NET_INCOME": 25,
                    "_fs_type_by_id": {"NET_INCOME": "IS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 9,
                    "financial_period": "2025-09-30",
                    "NET_INCOME": 45,
                    "_fs_type_by_id": {"NET_INCOME": "IS"},
                },
                {
                    "stock_code": "000001",
                    "security_id": "SEC_KR_000001",
                    "fiscal_year": 2025,
                    "fiscal_month": 12,
                    "financial_period": "2025-12-31",
                    "NET_INCOME": 80,
                    "_fs_type_by_id": {"NET_INCOME": "IS"},
                },
            ]
        )

        result = add_quarter_and_ttm_amounts(
            snapshot_df,
            cumulative_statement_types={"CF"},
        )

        self.assertEqual(result["NET_INCOME_quarter"].tolist(), [10, 25, 45, 0])


if __name__ == "__main__":
    unittest.main()
