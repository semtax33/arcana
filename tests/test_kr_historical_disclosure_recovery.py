from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from scripts.recover_kr_historical_disclosures import (
    RecoveryContract,
    build_recovery_parser,
    merge_historical_shares,
    merge_report_metadata_frames,
    pending_statement_years,
)
from engine.workflows._internal.normalize_workflow import normalization_dependency_paths


class KrHistoricalDisclosureRecoveryTest(unittest.TestCase):
    def test_cli_defaults_to_serial_rate_limited_dart_recovery(self):
        args = build_recovery_parser().parse_args([])

        self.assertEqual(args.workers, 1)
        self.assertGreaterEqual(args.request_interval, 1.0)
        self.assertEqual(args.normalize_start_year, 2000)
        self.assertIsNone(args.normalize_end_year)

    def test_normalization_cache_tracks_semantic_runtime_sources(self):
        dependency_names = {
            path.relative_to(Path.cwd()).as_posix()
            for path in normalization_dependency_paths()
        }

        self.assertIn("engine/semantic/rules.py", dependency_names)
        self.assertIn("engine/semantic/matcher.py", dependency_names)

    def test_historical_shares_merge_preserves_existing_duplicate_as_authoritative(self):
        historical = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2002-01-02",
                    "shares": 100,
                    "market_cap": 1_000,
                },
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2010-01-04",
                    "shares": 110,
                    "market_cap": 1_100,
                },
            ]
        )
        existing = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2010-01-04",
                    "shares": 111,
                    "market_cap": 1_111,
                }
            ]
        )

        result = merge_historical_shares(existing, historical)

        self.assertEqual(len(result), 2)
        overlap = result.loc[result["trade_date"] == "2010-01-04"].iloc[0]
        self.assertEqual(overlap["shares"], 111)
        self.assertEqual(overlap["market_cap"], 1_111)

    def test_contract_is_deterministic_and_rejects_non_kr_ids(self):
        first = RecoveryContract.create(
            ["SEC_KR_005930", "SEC_KR_000660", "SEC_KR_005930"],
            start_date="2002-01-01",
            end_date="2012-12-31",
        )
        second = RecoveryContract.create(
            ["SEC_KR_000660", "SEC_KR_005930"],
            start_date="2002-01-01",
            end_date="2012-12-31",
        )

        self.assertEqual(first.target_sha256, second.target_sha256)
        self.assertEqual(first.symbols, ("000660", "005930"))
        with self.assertRaisesRegex(ValueError, "SEC_KR"):
            RecoveryContract.create(
                ["SEC_US_AAPL"],
                start_date="2002-01-01",
                end_date="2012-12-31",
            )

    def test_statement_year_markers_make_collection_resumable(self):
        with TemporaryDirectory() as tmp:
            marker_dir = Path(tmp)
            (marker_dir / "005930_2009.json").write_text("{}", encoding="utf-8")

            result = pending_statement_years(
                "005930",
                years=[2009, 2010, 2011],
                marker_dir=marker_dir,
            )

        self.assertEqual(result, [2010, 2011])

    def test_metadata_merge_keeps_future_rows_and_latest_filing_for_snapshot(self):
        existing = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "stock_code": "005930",
                    "fiscal_year": 2025,
                    "fiscal_month": 12,
                    "report_date": "2026-03-15",
                    "rcept_no": "20260315000001",
                    "source_type": "statement",
                }
            ]
        )
        recovered = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "stock_code": "005930",
                    "fiscal_year": 2011,
                    "fiscal_month": 12,
                    "report_date": "2012-03-01",
                    "rcept_no": "20120301000001",
                    "source_type": "statement",
                },
                {
                    "security_id": "SEC_KR_005930",
                    "stock_code": "005930",
                    "fiscal_year": 2011,
                    "fiscal_month": 12,
                    "report_date": "2012-03-20",
                    "rcept_no": "20120320000001",
                    "source_type": "statement",
                },
            ]
        )

        result = merge_report_metadata_frames([existing, recovered])

        self.assertEqual(set(result["fiscal_year"].astype(int)), {2011, 2025})
        historical = result.loc[result["fiscal_year"].astype(int) == 2011].iloc[0]
        self.assertEqual(historical["report_date"], "2012-03-20")


if __name__ == "__main__":
    unittest.main()
