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
        self.assertNotIn("HAVING factor_value >= 0", query)
        self.assertIn("value_direction) = 'LOWER_BETTER'", query)
        self.assertIn("percentile_score", query)
        self.assertNotIn("sm.is_active", query)

    def test_factor_snapshot_query_supports_style_score_conditions(self):
        query, params = build_factor_snapshot_query(
            [
                FactorCondition.top("style_total_score", 10),
                FactorCondition.threshold("roe", ">=", 15),
            ],
            signal_date="2026-03-31",
            style_profile="DIVIDEND_QUALITY",
        )

        self.assertEqual(params["factor_ids"], ["roe", "style_total_score"])
        self.assertEqual(params["regular_factor_ids"], ["roe"])
        self.assertEqual(params["style_profile"], "DIVIDEND_QUALITY")
        self.assertIn("FROM arcana.fact_daily_style_score AS s", query)
        self.assertIn("'style_total_score' AS factor_id", query)
        self.assertIn("argMax(toFloat64(s.total_score), tuple(s.trade_date, s.updated_at))", query)
        self.assertIn("s.style_profile = {style_profile:String}", query)
        self.assertIn("UNION ALL", query)
        self.assertIn("has({regular_factor_ids:Array(String)}, f.factor_id)", query)

    def test_factor_snapshot_query_canonicalizes_style_score_aliases(self):
        query, params = build_factor_snapshot_query(
            [FactorCondition.threshold("total_score", ">=", 70)],
            signal_date="2026-03-31",
        )

        self.assertEqual(params["factor_ids"], ["style_total_score"])
        self.assertEqual(params["style_profile"], "DEFAULT")
        self.assertNotIn("selected_catalog AS", query)
        self.assertIn("FROM arcana.fact_daily_style_score AS s", query)

    def test_factor_snapshot_query_canonicalizes_general_factor_aliases(self):
        _, params = build_factor_snapshot_query(
            [
                FactorCondition.top("EV/NOPAT", 20),
                FactorCondition.top("EV_TO_NOPAT", 20),
                FactorCondition.threshold("WORKING CAPITAL TURNOVER", ">=", 1),
            ],
            signal_date="2026-03-31",
        )

        self.assertEqual(
            params["factor_ids"],
            ["ev_to_nopat", "working_capital_turnover"],
        )

    def test_factor_snapshot_query_can_filter_industry_groups(self):
        query, params = build_factor_snapshot_query(
            [FactorCondition.top("roe", 30)],
            signal_date="2026-01-01",
            industry_group_codes=["4530"],
        )

        self.assertEqual(params["industry_group_codes"], ["4530"])
        self.assertIn("security_universe AS", query)
        self.assertIn("has({industry_group_codes:Array(String)}, iss.industry_group_code)", query)
        self.assertIn("INNER JOIN security_universe AS u", query)

    def test_factor_snapshot_query_can_filter_market(self):
        query, params = build_factor_snapshot_query(
            [FactorCondition.top("roe", 30)],
            signal_date="2026-01-01",
            market="KR",
        )

        self.assertEqual(params["market_country"], "KR")
        self.assertIn("security_universe AS", query)
        self.assertIn("sm.country = {market_country:String}", query)
        self.assertIn("INNER JOIN security_universe AS u", query)


if __name__ == "__main__":
    unittest.main()
