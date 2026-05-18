import unittest

from engine import dividend_normalizer


class DividendNormalizerTest(unittest.TestCase):
    def test_normalize_dividend_amount_preserves_decimal_places(self):
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("1,000 원"), 1000)
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("45.21 원"), 45.21)
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("-"), 0)

    def test_calculate_payout_ratio_allows_values_above_one(self):
        original_total = dividend_normalizer.calculate_total_dividend_amount
        original_income = dividend_normalizer.calculate_net_income
        try:
            dividend_normalizer.calculate_total_dividend_amount = lambda stock_code, year: 25
            dividend_normalizer.calculate_net_income = lambda stock_code, year, month: 100
            self.assertEqual(dividend_normalizer.calculate_payout_ratio("005930", 2025), 0.25)

            dividend_normalizer.calculate_total_dividend_amount = lambda stock_code, year: 125
            self.assertEqual(dividend_normalizer.calculate_payout_ratio("005930", 2025), 1.25)

            dividend_normalizer.calculate_total_dividend_amount = lambda stock_code, year: 25
            dividend_normalizer.calculate_net_income = lambda stock_code, year, month: -100
            self.assertIsNone(dividend_normalizer.calculate_payout_ratio("005930", 2025))
        finally:
            dividend_normalizer.calculate_total_dividend_amount = original_total
            dividend_normalizer.calculate_net_income = original_income

    def test_deduplicate_dividend_records_keeps_latest_disclosure_per_event(self):
        records = [
            {
                "dividend_base_date": "2025-12-31",
                "dividend_type": "결산배당",
                "dividend_disclosure_date": "2026-02-01",
                "source_file": "old.json",
                "dividend_per_share": 100,
            },
            {
                "dividend_base_date": "2025-12-31",
                "dividend_type": "결산배당",
                "dividend_disclosure_date": "2026-02-03",
                "source_file": "new.json",
                "dividend_per_share": 120,
            },
            {
                "dividend_base_date": "2025-06-30",
                "dividend_type": "중간배당",
                "dividend_disclosure_date": "2025-07-15",
                "source_file": "mid.json",
                "dividend_per_share": 50,
            },
        ]

        result = dividend_normalizer.deduplicate_dividend_records(records)

        self.assertEqual(len(result), 2)
        self.assertEqual(sum(row["dividend_per_share"] for row in result), 170)


if __name__ == "__main__":
    unittest.main()
