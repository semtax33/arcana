import unittest
from datetime import datetime

import pandas as pd

from engine.statements import (
    deduplicate_report_metadata,
    extract_dart_report_metadata_from_search_html,
    parse_report_period_from_title,
    report_date_from_rcept_no,
)


class ReportMetadataTest(unittest.TestCase):
    def test_parse_search_html_extracts_period_rcept_no_and_report_date(self):
        html = """
        <html><body>
          <a href="/dsaf001/main.do?rcpNo=20240515001234">
            Sample Corp
            quarterly report (2024.03)
          </a>
        </body></html>
        """

        result = extract_dart_report_metadata_from_search_html(
            html,
            "5930",
            source_type="statement",
            updated_at=datetime(2026, 5, 22, 9, 0, 0),
        )

        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row["stock_code"], "005930")
        self.assertEqual(row["security_id"], "SEC_KR_005930")
        self.assertEqual(row["fiscal_year"], 2024)
        self.assertEqual(row["fiscal_month"], 3)
        self.assertEqual(row["period_end_date"], "2024-03-31")
        self.assertEqual(row["rcept_no"], "20240515001234")
        self.assertEqual(row["report_date"], "2024-05-15")
        self.assertEqual(row["source_type"], "statement")

    def test_deduplicate_keeps_latest_report_for_same_period(self):
        rows = [
            {
                "stock_code": "005930",
                "fiscal_year": 2024,
                "fiscal_month": 12,
                "report_date": "2025-03-01",
                "rcept_no": "20250301000001",
                "source_type": "statement",
            },
            {
                "stock_code": "005930",
                "fiscal_year": 2024,
                "fiscal_month": 12,
                "report_date": "2025-03-15",
                "rcept_no": "20250315000001",
                "source_type": "statement",
            },
            {
                "stock_code": "005930",
                "fiscal_year": 2024,
                "fiscal_month": 12,
                "report_date": "2025-03-10",
                "rcept_no": "20250310000001",
                "source_type": "comment",
            },
        ]

        result = deduplicate_report_metadata(pd.DataFrame(rows))

        self.assertEqual(len(result), 2)
        statement = result.loc[result["source_type"] == "statement"].iloc[0]
        self.assertEqual(statement["report_date"], "2025-03-15")
        self.assertEqual(statement["rcept_no"], "20250315000001")

    def test_report_period_and_date_helpers_validate_inputs(self):
        self.assertEqual(parse_report_period_from_title("annual report (2024.12)"), (2024, 12))
        self.assertEqual(report_date_from_rcept_no("20250315000001"), "2025-03-15")
        self.assertIsNone(parse_report_period_from_title("ad hoc report (2024.05)"))
        with self.assertRaises(ValueError):
            report_date_from_rcept_no("20250315")


if __name__ == "__main__":
    unittest.main()
