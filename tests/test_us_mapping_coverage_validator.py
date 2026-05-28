from __future__ import annotations

import csv
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from engine.us_mapping_coverage_validator import (
    build_mapping_coverage_report,
    write_report_files,
)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_rules(path: Path) -> None:
    rules = {
        "companyfacts_rules": [
            {
                "canonical_id": "RND",
                "fs_type": "IS",
                "primary_tags": ["us-gaap:ResearchAndDevelopmentExpense"],
            },
            {
                "canonical_id": "CAPEX_PPE",
                "fs_type": "CF",
                "primary_tags": ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"],
            },
        ],
        "notes_rules": [
            {
                "id": "notes_rnd",
                "canonical_id": "RND",
                "fs_type": "IS",
                "tags": ["ResearchAndDevelopmentExpense"],
            }
        ],
        "edgartools_fallback_rules": [
            {
                "canonical_id": "RND",
                "fs_type": "IS",
                "tags": ["ResearchAndDevelopmentExpense"],
            }
        ],
    }
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")


class UsMappingCoverageValidatorTest(unittest.TestCase):
    def test_empty_directory_warns_and_writes_headers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "us_mapping.yaml"
            write_rules(rules_path)

            report = build_mapping_coverage_report(
                root / "normalized",
                rule_path=rules_path,
                output_dir=root / "out",
                required_ids=["RND"],
            )
            written = write_report_files(report)

            self.assertEqual(report.verdict, "WARN")
            self.assertEqual(report.symbol_year_count, 0)
            self.assertTrue(all(path.exists() for path in written))
            self.assertIn("canonical_id", (root / "out" / "canonical_coverage.csv").read_text(encoding="utf-8-sig"))
            self.assertIn("factor_id", (root / "out" / "factor_readiness.csv").read_text(encoding="utf-8-sig"))

    def test_reports_canonical_source_and_expected_rule_coverage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "us_mapping.yaml"
            normalized = root / "normalized"
            write_rules(rules_path)

            write_csv(
                normalized / "us_normalized_AAPL.csv",
                [
                    {
                        "symbol": "AAPL",
                        "canonical_account_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                    {
                        "symbol": "AAPL",
                        "canonical_account_id": "CAPEX_PPE",
                        "statement_type": "CF",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                    {
                        "symbol": "AAPL",
                        "canonical_account_id": "REVENUE",
                        "statement_type": "IS",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    },
                ],
            )
            write_csv(
                normalized / "us_normalized_MSFT.csv",
                [
                    {
                        "symbol": "MSFT",
                        "canonical_account_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                    }
                ],
            )
            write_csv(
                normalized / "us_normalized_AAPL.debug.csv",
                [
                    {
                        "symbol": "AAPL",
                        "canonical_account_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                        "source": "companyfacts_primary",
                        "rule_id": "companyfacts_primary:RND:us-gaap:ResearchAndDevelopmentExpense",
                    },
                    {
                        "symbol": "AAPL",
                        "canonical_account_id": "CAPEX_PPE",
                        "statement_type": "CF",
                        "fiscal_year": "2025",
                        "fiscal_month": "12",
                        "source": "companyfacts_primary",
                        "rule_id": "companyfacts_primary:CAPEX_PPE:us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                    },
                ],
            )

            report = build_mapping_coverage_report(
                normalized,
                rule_path=rules_path,
                output_dir=root / "out",
                required_ids=["RND", "CAPEX_PPE"],
                min_required_coverage_pct=60,
            )

            coverage_by_id = {row["canonical_id"]: row for row in report.canonical_coverage}
            readiness_by_id = {row["factor_id"]: row for row in report.factor_readiness}
            rule_by_key = {row["rule_key"]: row for row in report.rule_coverage}

            self.assertEqual(report.verdict, "WARN")
            self.assertEqual(report.symbol_count, 2)
            self.assertEqual(report.symbol_year_count, 2)
            self.assertEqual(coverage_by_id["RND"]["coverage_pct"], 100.0)
            self.assertEqual(coverage_by_id["CAPEX_PPE"]["coverage_pct"], 50.0)
            self.assertEqual(readiness_by_id["RND"]["coverage_pct"], 100.0)
            self.assertEqual(readiness_by_id["RND_MARGIN"]["coverage_pct"], 50.0)
            self.assertEqual(
                rule_by_key["companyfacts_primary:RND:us-gaap:ResearchAndDevelopmentExpense"]["row_count"],
                1,
            )
            self.assertEqual(
                report.source_contribution,
                [
                    {
                        "canonical_id": "CAPEX_PPE",
                        "source": "companyfacts_primary",
                        "covered_symbol_years": 1,
                        "row_count": 1,
                    },
                    {
                        "canonical_id": "RND",
                        "source": "companyfacts_primary",
                        "covered_symbol_years": 1,
                        "row_count": 1,
                    },
                ],
            )

    def test_progress_output_goes_to_stderr(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "us_mapping.yaml"
            write_rules(rules_path)

            stderr = StringIO()
            with redirect_stderr(stderr):
                build_mapping_coverage_report(
                    root / "normalized",
                    rule_path=rules_path,
                    output_dir=root / "out",
                    required_ids=["RND"],
                    progress_interval=1,
                )

            output = stderr.getvalue()
            self.assertIn("[US_MAPPING_COVERAGE] START", output)
            self.assertIn("[US_MAPPING_COVERAGE] DISCOVERED", output)
            self.assertIn("[US_MAPPING_COVERAGE] DONE", output)


if __name__ == "__main__":
    unittest.main()
