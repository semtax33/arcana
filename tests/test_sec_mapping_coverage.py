from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.sec_mapping_coverage import build_coverage_report, write_coverage_report


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class SecMappingCoverageTest(unittest.TestCase):
    def test_empty_directory_reports_zero_coverage(self):
        with TemporaryDirectory() as tmp:
            report = build_coverage_report(tmp, focus_ids=["RND", "CAPEX_PPE"])
            written = write_coverage_report(report, Path(tmp) / "coverage")

            self.assertEqual(report.symbol_year_count, 0)
            self.assertEqual(report.canonical_coverage[0]["coverage_pct"], 0.0)
            self.assertEqual(report.source_contribution, [])
            self.assertTrue(all(path.exists() for path in written))

    def test_reports_canonical_coverage_source_contribution_and_delta(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            write_rows(
                normalized / "us_normalized_AAPL.csv",
                [
                    {
                        "canonical_account_id": "RND",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                    {
                        "canonical_account_id": "CAPEX_PPE",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                ],
            )
            write_rows(
                normalized / "us_normalized_MSFT.csv",
                [
                    {
                        "canonical_account_id": "RND",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                    {
                        "canonical_account_id": "REVENUE",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                ],
            )
            write_rows(
                normalized / "us_normalized_AAPL.debug.csv",
                [
                    {
                        "canonical_account_id": "RND",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                        "source": "notes",
                    },
                    {
                        "canonical_account_id": "CAPEX_PPE",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                        "source": "companyfacts_alternate",
                    },
                ],
            )
            baseline = root / "baseline.csv"
            write_rows(
                baseline,
                [
                    {
                        "canonical_id": "RND",
                        "covered_symbol_years": "1",
                        "missing_symbol_years": "1",
                        "total_symbol_years": "2",
                        "coverage_pct": "50",
                    }
                ],
            )

            report = build_coverage_report(
                normalized,
                focus_ids=["RND", "CAPEX_PPE"],
                baseline_csv=baseline,
            )

            coverage_by_id = {
                row["canonical_id"]: row
                for row in report.canonical_coverage
            }
            self.assertEqual(report.symbol_count, 2)
            self.assertEqual(report.symbol_year_count, 2)
            self.assertEqual(coverage_by_id["RND"]["coverage_pct"], 100.0)
            self.assertEqual(coverage_by_id["RND"]["delta_pct"], 50.0)
            self.assertEqual(coverage_by_id["CAPEX_PPE"]["coverage_pct"], 50.0)
            self.assertEqual(
                report.source_contribution,
                [
                    {
                        "canonical_id": "CAPEX_PPE",
                        "source": "companyfacts_alternate",
                        "covered_symbol_years": 1,
                        "row_count": 1,
                    },
                    {
                        "canonical_id": "RND",
                        "source": "notes",
                        "covered_symbol_years": 1,
                        "row_count": 1,
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
