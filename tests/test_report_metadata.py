import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from engine.extractors.filings import (
    deduplicate_report_metadata,
    extract_dart_report_metadata_from_search_html,
    fetch_dart_report_metadata,
    parse_report_period_from_title,
    report_date_from_rcept_no,
)


class ReportMetadataTest(unittest.TestCase):
    def test_fetch_report_metadata_splits_long_ranges_and_merges_windows(self):
        class CountingThrottle:
            def __init__(self):
                self.wait_count = 0

            def wait(self):
                self.wait_count += 1

        class FakeResponse:
            status_code = 200
            headers = {}
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            def __init__(self, text: str):
                self.text = text

            def raise_for_status(self):
                return None

        class FakeSession:
            def __init__(self):
                self.windows = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def request(self, _method, _url, **kwargs):
                fields = dict(kwargs["data"])
                window = (fields["startDate"], fields["endDate"])
                self.windows.append(window)
                report_year = int(fields["endDate"][:4])
                rcept_no = f"{report_year}0315000001"
                return FakeResponse(
                    '<a href="/dsaf001/main.do?rcpNo='
                    f'{rcept_no}">annual report ({report_year - 1}.12)</a>'
                )

        session = FakeSession()
        throttle = CountingThrottle()
        with patch(
            "engine.extractors._internal.dart_filings.requests.Session",
            return_value=session,
        ):
            result = fetch_dart_report_metadata(
                "005930",
                start_date="20000101",
                end_date="20251231",
                throttle=throttle,
            )

        self.assertEqual(
            session.windows,
            [
                ("20000101", "20091231"),
                ("20100101", "20191231"),
                ("20200101", "20251231"),
            ],
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(throttle.wait_count, 3)
        self.assertEqual(
            result["report_date"].tolist(),
            ["2009-03-15", "2019-03-15", "2025-03-15"],
        )

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
