from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.normalization_validator import (
    build_stock_year_index,
    find_stock_year_pairs,
    parse_stock_period_from_filename,
    resolve_batch_stock_refs,
    stock_artifact_base,
)


class NormalizationValidatorMarketTest(unittest.TestCase):
    def test_parse_stock_period_accepts_non_kr_symbols(self):
        self.assertEqual(
            parse_stock_period_from_filename("us_normalized_AAPL_2025.12.csv"),
            {"market": "us", "stock_code": "AAPL", "period": "2025.12", "year": 2025},
        )
        self.assertEqual(
            parse_stock_period_from_filename("jp_normalized_7203_2025_03.debug.csv"),
            {"market": "jp", "stock_code": "7203", "period": "2025.03", "year": 2025},
        )
        self.assertEqual(
            parse_stock_period_from_filename("normalized_005930_2025.12.csv"),
            {"market": "kr", "stock_code": "005930", "period": "2025.12", "year": 2025},
        )

    def test_stock_year_index_keeps_markets_separate(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in [
                "us_normalized_7203_2025.12.csv",
                "us_normalized_7203_2025.12.debug.csv",
                "jp_normalized_7203_2025.12.csv",
                "jp_normalized_7203_2025.12.debug.csv",
            ]:
                (root / name).write_text("", encoding="utf-8")

            index = build_stock_year_index(root, 2025, 2025)

            self.assertEqual(sorted(index), ["jp:7203", "us:7203"])
            self.assertEqual(resolve_batch_stock_refs(index, ["us:7203"]), [("us", "7203")])

            with self.assertRaises(ValueError):
                find_stock_year_pairs(root, "7203", 2025, 2025, stock_year_index=index)

            pairs = find_stock_year_pairs(
                root,
                "7203",
                2025,
                2025,
                stock_year_index=index,
                market="jp",
            )

            self.assertEqual(pairs[0]["status"], "FOUND")
            self.assertIn("jp_normalized_7203_2025.12.csv", pairs[0]["normalized"])

    def test_stock_artifact_base_uses_market_prefix(self):
        self.assertEqual(stock_artifact_base("AAPL", 2024, 2025, "us"), "us_AAPL_2024_2025.stock")
        self.assertEqual(stock_artifact_base("005930", 2024, 2025), "kr_005930_2024_2025.stock")


if __name__ == "__main__":
    unittest.main()
