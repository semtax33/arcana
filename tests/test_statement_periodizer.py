import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

import pandas as pd

from engine.transformers.filing_periods import add_quarter_and_ttm_amounts, read_period_snapshots


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

    def test_read_period_snapshots_joins_report_metadata_and_falls_back_to_period_end(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            financial_dir = root / "financials"
            financial_dir.mkdir()
            metadata_path = root / "report_metadata.csv"

            for month in [3, 6]:
                prefix = "kr_" if month == 3 else ""
                (financial_dir / f"{prefix}normalized_005930_2025.{month:02d}.csv").write_text(
                    "\n".join(
                        [
                            "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount",
                            f"REVENUE,Revenue,Revenue,IS,2025.{month},100",
                        ]
                    ),
                    encoding="utf-8",
                )

            metadata_path.write_text(
                "\n".join(
                    [
                        "security_id,stock_code,fiscal_year,fiscal_month,period_end_date,report_date,rcept_no,report_name,source_type,source_url,updated_at",
                        "SEC_KR_005930,005930,2025,3,2025-03-31,2025-05-15,20250515000001,Q1,statement,https://dart.example/1,2026-05-22 09:00:00",
                    ]
                ),
                encoding="utf-8",
            )

            result = read_period_snapshots(
                "005930",
                financial_dir=financial_dir,
                report_metadata_path=metadata_path,
            )

        self.assertEqual(result["report_date"].dt.strftime("%Y-%m-%d").tolist(), ["2025-05-15", "2025-06-30"])
        self.assertEqual(result["rcept_no"].iloc[0], "20250515000001")


if __name__ == "__main__":
    unittest.main()
