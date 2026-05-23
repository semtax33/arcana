import unittest

from api.repository.backtest_query import build_factor_snapshot_query
from api.repository.factor_screen_query import FactorCondition


class BacktestQueryTest(unittest.TestCase):
    def test_factor_snapshot_query_uses_signal_date_and_point_in_time_latest_values(self):
        query, params = build_factor_snapshot_query(
            [FactorCondition.top("roe", 20), FactorCondition.top("per", 20)],
            signal_date="2026-03-31",
            financial_basis="annual",
        )

        self.assertEqual(params["signal_date"], "2026-03-31")
        self.assertEqual(params["factor_ids"], ["per", "roe"])
        self.assertEqual(params["financial_basis"], "annual")
        self.assertIn("f.trade_date <= {signal_date:Date}", query)
        self.assertIn("argMax(f.factor_value, tuple(f.trade_date, f.updated_at))", query)
        self.assertIn("value_direction) = 'LOWER_BETTER'", query)
        self.assertIn("percentile_score", query)
        self.assertNotIn("sm.is_active", query)


if __name__ == "__main__":
    unittest.main()
