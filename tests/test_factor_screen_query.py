import unittest

from api.factor_screen_query import (
    FactorCondition,
    build_factor_screen_query,
    build_latest_factor_values_query,
)


class FactorScreenQueryTest(unittest.TestCase):
    def test_build_latest_factor_values_query_uses_catalog_and_as_of_date(self):
        query, params = build_latest_factor_values_query(
            ["roe", "per"],
            as_of_date="2026-05-17",
            financial_basis="ttm",
        )

        self.assertEqual(params["as_of_date"], "2026-05-17")
        self.assertEqual(params["factor_ids"], ["roe", "per"])
        self.assertEqual(params["financial_basis"], "ttm")
        self.assertIn("FROM fact_daily_factors AS f", query)
        self.assertIn("selected_catalog AS", query)
        self.assertIn("latest_trade_date AS", query)
        self.assertIn("INNER JOIN selected_catalog AS c", query)
        self.assertIn("any(unit) AS unit", query)
        self.assertIn("argMax(f.factor_value", query)
        self.assertNotIn("factor_value * 100", query)
        self.assertIn("trade_date <= {as_of_date:Date}", query)
        self.assertIn("f.trade_date = (SELECT trade_date FROM latest_trade_date)", query)
        self.assertIn("f.financial_basis = {financial_basis:String}", query)
        self.assertNotIn("HAVING factor_value >= 0", query)

    def test_queries_default_to_annual_financial_basis(self):
        latest_query, latest_params = build_latest_factor_values_query(
            ["roe"],
            as_of_date="2026-05-17",
        )
        screen_query, screen_params = build_factor_screen_query(
            [FactorCondition.top("roe", 20)],
            as_of_date="2026-05-17",
        )

        self.assertEqual(latest_params["financial_basis"], "annual")
        self.assertEqual(screen_params["financial_basis"], "annual")
        self.assertIn("financial_basis = {financial_basis:String}", latest_query)
        self.assertIn("financial_basis = {financial_basis:String}", screen_query)

    def test_build_factor_screen_query_supports_mixed_dynamic_conditions(self):
        query, params = build_factor_screen_query(
            [
                FactorCondition.top("roe", 20, alias="high_roe"),
                FactorCondition.threshold("per", "<=", 10, alias="cheap_per"),
                {
                    "factor_id": "debt_to_equity",
                    "mode": "threshold",
                    "operator": "between",
                    "min_value": 0,
                    "max_value": 1.5,
                },
            ],
            as_of_date="2026-05-17",
            financial_basis="annual",
            limit=50,
        )

        self.assertEqual(params["factor_ids"], ["debt_to_equity", "per", "roe"])
        self.assertEqual(params["required_condition_count"], 3)
        self.assertEqual(params["condition_0_top_percent"], 20.0)
        self.assertEqual(params["condition_1_value"], 10.0)
        self.assertEqual(params["condition_2_min_value"], 0.0)
        self.assertEqual(params["condition_2_max_value"], 1.5)
        self.assertIn("row_number() OVER", query)
        self.assertIn("latest_trade_date AS", query)
        self.assertIn("any(factor_name) AS factor_name", query)
        self.assertIn("GROUP BY factor_id", query)
        self.assertIn("max(trade_date) AS latest_date", query)
        self.assertIn("f.trade_date <= (SELECT latest_date FROM latest_trade_date)", query)
        self.assertIn("has({factor_ids:Array(String)}, f.factor_id)", query)
        self.assertNotIn("HAVING factor_value >= 0", query)
        self.assertIn("f.security_id AS security_id", query)
        self.assertIn("f.factor_id AS factor_id", query)
        self.assertIn("argMax(f.factor_value", query)
        self.assertNotIn("factor_value * 100", query)
        self.assertIn("FROM latest_factor_values AS lv", query)
        self.assertIn("PARTITION BY lv.factor_id", query)
        self.assertNotIn("SELECT\n        *,", query)
        self.assertNotIn("UNION ALL", query)
        self.assertIn("countIf((factor_id = {condition_0_factor_id:String}", query)
        self.assertIn("arrayConcat(", query)
        self.assertIn("if(value_direction = 'LOWER_BETTER', rank_low, rank_high)", query)
        self.assertIn("factor_value <= {condition_1_value:Float64}", query)
        self.assertIn(
            "factor_value BETWEEN {condition_2_min_value:Float64} AND {condition_2_max_value:Float64}",
            query,
        )
        self.assertIn("HAVING matched_condition_count >= {required_condition_count:UInt32}", query)
        self.assertIn("LIMIT {limit:UInt64}", query)
        self.assertIn("high_roe_0_value", query)
        self.assertIn("cheap_per_1_value", query)

    def test_security_universe_exposes_security_id_alias_for_clickhouse(self):
        query, _ = build_factor_screen_query(
            [FactorCondition.top("roe", 30)],
            sector_codes=["Energy"],
        )

        self.assertIn("sm.security_id AS security_id", query)
        self.assertIn("ON u.security_id = f.security_id", query)
        self.assertIn("has({sector_codes:Array(String)}, iss.sector_code)", query)
        self.assertIn("any(iss.sector_code) AS sector_code", query)

    def test_security_universe_can_filter_and_expose_industry_groups(self):
        query, params = build_factor_screen_query(
            [FactorCondition.top("roe", 30)],
            industry_group_codes=["4530"],
            include_security_metadata=True,
        )

        self.assertEqual(params["industry_group_codes"], ["4530"])
        self.assertIn("has({industry_group_codes:Array(String)}, iss.industry_group_code)", query)
        self.assertIn("any(iss.industry_group_code) AS industry_group_code", query)
        self.assertIn("any(u.industry_group_name) AS industry_group_name", query)

    def test_build_factor_screen_query_supports_style_score_conditions(self):
        query, params = build_factor_screen_query(
            [
                FactorCondition.threshold("style_total_score", ">=", 60, alias="style"),
                FactorCondition.top("roe", 20, alias="high_roe"),
            ],
            as_of_date="2026-05-17",
            style_profile="MINERVINI_ZWEIG",
        )

        self.assertEqual(params["factor_ids"], ["roe", "style_total_score"])
        self.assertEqual(params["regular_factor_ids"], ["roe"])
        self.assertEqual(params["style_profile"], "MINERVINI_ZWEIG")
        self.assertIn("latest_style_trade_date AS", query)
        self.assertIn("FROM arcana.fact_daily_style_score AS s", query)
        self.assertIn("'style_total_score' AS factor_id", query)
        self.assertIn("argMax(toFloat64(s.total_score), tuple(s.trade_date, s.updated_at)) AS factor_value", query)
        self.assertIn("UNION ALL", query)
        self.assertIn("has({regular_factor_ids:Array(String)}, f.factor_id)", query)
        self.assertIn("factor_value >= {condition_0_value:Float64}", query)
        self.assertIn("style_0_value", query)

    def test_build_factor_screen_query_filters_market_and_uses_latest_values_before_as_of(self):
        query, params = build_factor_screen_query(
            [FactorCondition.top("roe", 30)],
            as_of_date="2026-05-17",
            market="us",
            include_security_metadata=True,
        )

        self.assertEqual(params["market_country"], "US")
        self.assertIn("AND sm.country = {market_country:String}", query)
        self.assertIn("f.trade_date <= (SELECT latest_date FROM latest_trade_date)", query)
        self.assertIn("argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value", query)
        self.assertNotIn("f.trade_date = (SELECT trade_date FROM latest_trade_date)", query)

    def test_style_score_aliases_are_canonicalized(self):
        query, params = build_factor_screen_query(
            [FactorCondition.threshold("total_score", ">=", 70)],
            as_of_date="2026-05-17",
        )

        self.assertEqual(params["factor_ids"], ["style_total_score"])
        self.assertEqual(params["condition_0_factor_id"], "style_total_score")
        self.assertEqual(params["style_profile"], "MINERVINI_ZWEIG")
        self.assertNotIn("selected_catalog AS", query)
        self.assertIn("FROM arcana.fact_daily_style_score AS s", query)

    def test_invalid_dynamic_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            build_latest_factor_values_query(["roe;DROP"])

        with self.assertRaises(ValueError):
            build_factor_screen_query(
                [FactorCondition.threshold("roe", ">=", 0.1)],
                factor_table="fact_daily_factors;DROP TABLE x",
            )

    def test_top_percent_must_be_within_percent_range(self):
        with self.assertRaises(ValueError):
            build_factor_screen_query([FactorCondition.top("roe", 0)])


if __name__ == "__main__":
    unittest.main()
