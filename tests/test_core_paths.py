from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.core.paths import (
    first_existing_path,
    market_csv_name,
    market_symbol_csv_name,
    parse_statement_snapshot_filename,
    statement_snapshot_name,
)


class CorePathsTest(unittest.TestCase):
    def test_market_csv_names_use_market_prefix(self):
        self.assertEqual(market_csv_name("normalized_price"), "kr_normalized_price.csv")
        self.assertEqual(market_symbol_csv_name("005930"), "kr_005930.csv")
        self.assertEqual(market_csv_name("normalized_price", market="us"), "us_normalized_price.csv")

    def test_statement_snapshot_name_and_parse_include_market_prefix(self):
        name = statement_snapshot_name("5930", 2025, 12)

        self.assertEqual(name, "kr_normalized_005930_2025.12.csv")
        self.assertEqual(
            parse_statement_snapshot_filename(name),
            {"market": "kr", "stock_code": "005930", "year": 2025, "month": 12},
        )

    def test_parse_statement_snapshot_filename_accepts_legacy_name(self):
        self.assertEqual(
            parse_statement_snapshot_filename("normalized_005930_2025.12.csv"),
            {"market": "kr", "stock_code": "005930", "year": 2025, "month": 12},
        )

    def test_first_existing_path_prefers_primary_then_legacy(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = root / "kr_report_metadata.csv"
            legacy = root / "report_metadata.csv"
            legacy.write_text("", encoding="utf-8")

            self.assertEqual(first_existing_path(primary, legacy), legacy)

            primary.write_text("", encoding="utf-8")

            self.assertEqual(first_existing_path(primary, legacy), primary)


if __name__ == "__main__":
    unittest.main()
