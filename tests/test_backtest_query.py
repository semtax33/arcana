import unittest

from api.repository.backtest_query import (
    build_factor_raw_batch_query,
    build_factor_snapshot_batch_query,
    build_factor_snapshot_query,
    build_portfolio_return_query,
    build_trading_days_query,
)
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

    def test_factor_snapshot_query_can_use_daily_snapshot_table(self):
        query, params = build_factor_snapshot_query(
            [FactorCondition.top("roe", 20), FactorCondition.top("per", 20)],
            signal_date="2026-03-31",
            financial_basis="annual",
            factor_table="fact_daily_factor_snapshot",
            factor_table_is_snapshot=True,
        )

        self.assertEqual(params["signal_date"], "2026-03-31")
        self.assertIn("latest_snapshot_date AS", query)
        self.assertIn("FROM fact_daily_factor_snapshot", query)
        self.assertIn("f.trade_date = (SELECT snapshot_date FROM latest_snapshot_date)", query)
        self.assertIn("argMax(f.source_trade_date, tuple(f.trade_date, f.updated_at)) AS trade_date", query)
        self.assertIn("f.source_trade_date <= {signal_date:Date}", query)
        self.assertNotIn("WHERE f.trade_date <= {signal_date:Date}", query)

    def test_factor_snapshot_batch_query_uses_snapshot_dates_for_multiple_signals(self):
        query, params = build_factor_snapshot_batch_query(
            [FactorCondition.top("roe", 20), FactorCondition.top("per", 20)],
            signal_dates=["2026-03-31", "2026-06-30"],
            snapshot_dates=["2026-03-30", "2026-06-30"],
            market="KR",
            financial_basis="annual",
        )

        self.assertEqual(params["signal_dates"], ["2026-03-31", "2026-06-30"])
        self.assertEqual(params["snapshot_dates"], ["2026-03-30", "2026-06-30"])
        self.assertEqual(params["factor_ids"], ["per", "roe"])
        self.assertEqual(params["market_country"], "KR")
        self.assertIn("snapshot_date_map AS", query)
        self.assertIn("arrayZip(", query)
        self.assertNotIn("CROSS JOIN", query)
        self.assertIn("f.trade_date IN {snapshot_dates:Array(Date)}", query)
        self.assertIn("f.source_trade_date <= lsd.signal_date", query)
        self.assertIn("PARTITION BY signal_date, factor_id", query)
        self.assertIn("rf.signal_date AS signal_date", query)

    def test_factor_snapshot_batch_query_rejects_future_snapshot_dates(self):
        with self.assertRaisesRegex(ValueError, "must not be later"):
            build_factor_snapshot_batch_query(
                [FactorCondition.top("roe", 20)],
                signal_dates=["2026-03-31"],
                snapshot_dates=["2026-04-01"],
            )

    def test_factor_raw_batch_query_scans_all_signal_dates_in_one_query(self):
        query, params = build_factor_raw_batch_query(
            [FactorCondition(factor_id="roe", mode="top_percent", top_percent=20)],
            signal_dates=["2026-06-30", "2026-03-31"],
            market="KR",
            raw_lookback_days=540,
        )

        self.assertIn("wide_latest_factor_values AS", query)
        self.assertEqual(query.count("FROM fact_daily_factors AS f"), 1)
        self.assertIn("argMaxIf", query)
        self.assertNotIn("CROSS JOIN signal_date_list", query)
        self.assertIn("f.trade_date >= {raw_start_date:Date}", query)
        self.assertEqual(params["signal_dates"], ["2026-03-31", "2026-06-30"])
        self.assertEqual(params["raw_start_date"], "2024-10-07")

    def test_portfolio_return_query_returns_only_daily_segment_results(self):
        query, params, position_rows = build_portfolio_return_query(
            segments=[
                {
                    "security_ids": ["SEC_B", "SEC_A"],
                    "start_date": "2026-01-02",
                    "end_date": "2026-01-03",
                    "transaction_cost_bps": 5,
                }
            ],
            trading_days=["2026-01-02", "2026-01-03"],
        )

        self.assertIn("portfolio_positions AS", query)
        self.assertIn("lagInFrame", query)
        self.assertIn("avgIf", query)
        self.assertIn("ranked_segment_returns AS", query)
        self.assertEqual(params["security_ids"], ["SEC_A", "SEC_B"])
        self.assertEqual(
            position_rows,
            [
                (0, "SEC_A", "2026-01-02", "2026-01-03", 5.0),
                (0, "SEC_B", "2026-01-02", "2026-01-03", 5.0),
            ],
        )

    def test_trading_days_query_can_scope_dates_to_market(self):
        query, params = build_trading_days_query(
            start_date="2026-01-01",
            end_date="2026-01-31",
            market="KR",
        )

        self.assertIn("INNER JOIN security_master AS sm", query)
        self.assertIn("sm.country = {market_country:String}", query)
        self.assertEqual(params["market_country"], "KR")


if __name__ == "__main__":
    unittest.main()
