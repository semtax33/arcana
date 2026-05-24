import unittest
from datetime import date

from api.main import app
from api.service.style_score_service import (
    StyleScoreService,
    _build_factor_breakdown_query,
    _build_style_score_detail_query,
    _build_style_score_list_query,
    _resolve_available_trade_date,
)


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(self):
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        params = parameters or {}
        self.queries.append((query, params))
        if "nullIf(max(s.trade_date), toDate(0)) AS available_trade_date" in query:
            return FakeFrame([{"available_trade_date": "2026-05-22"}])
        if "FROM arcana.fact_daily_style_score AS s FINAL" in query:
            return FakeFrame(
                [
                    {
                        "trade_date": params.get("trade_date", "2026-05-22"),
                        "security_id": "SEC_A",
                        "issuer_id": "ISS_A",
                        "stock_code": "000001",
                        "company_name": "A Corp",
                        "industry_schema": "GICS",
                        "sector_code": "45",
                        "industry_group_code": "4530",
                        "industry_group_name": "Semiconductors",
                        "style_profile": "DEFAULT",
                        "value_score": 70.0,
                        "quality_score": 80.0,
                        "growth_score": None,
                        "momentum_score": 90.0,
                        "risk_score": 60.0,
                        "dividend_score": 55.0,
                        "total_score": 77.0,
                        "score_confidence": 0.8,
                        "available_factor_count": 13,
                        "required_factor_count": 28,
                        "missing_factor_ids": ["eps_yoy_pct"],
                        "invalid_factor_ids": [],
                    },
                ]
            )
        if "FROM arcana.fact_daily_factor_score" in query:
            return FakeFrame(
                [
                    {
                        "factor_id": "epr",
                        "style_group": "VALUE",
                        "factor_direction": 1,
                        "raw_factor_value": 0.05,
                        "winsorized_value": 0.05,
                        "percentile_score": 75.0,
                        "robust_z_score": 1.2,
                        "n_peers": 25,
                        "industry_level": "INDUSTRY_GROUP",
                        "industry_code": "4530",
                        "industry_name": "Semiconductors",
                        "is_valid": True,
                        "invalid_reason": "",
                        "is_winsorized": False,
                        "score_confidence": 1.0,
                    },
                    {
                        "factor_id": "dividend_yield",
                        "style_group": "DIVIDEND",
                        "factor_direction": 1,
                        "raw_factor_value": 2.5,
                        "winsorized_value": 2.5,
                        "percentile_score": 80.0,
                        "robust_z_score": 0.8,
                        "n_peers": 25,
                        "industry_level": "INDUSTRY_GROUP",
                        "industry_code": "4530",
                        "industry_name": "Semiconductors",
                        "is_valid": True,
                        "invalid_reason": "",
                        "is_winsorized": False,
                        "score_confidence": 1.0,
                    },
                    {
                        "factor_id": "sharehold_div_yield",
                        "style_group": "DIVIDEND",
                        "factor_direction": 1,
                        "raw_factor_value": 3.0,
                        "winsorized_value": 3.0,
                        "percentile_score": 55.0,
                        "robust_z_score": 0.2,
                        "n_peers": 25,
                        "industry_level": "INDUSTRY_GROUP",
                        "industry_code": "4530",
                        "industry_name": "Semiconductors",
                        "is_valid": True,
                        "invalid_reason": "",
                        "is_winsorized": False,
                        "score_confidence": 1.0,
                    },
                ]
            )
        return FakeFrame([])

    def close(self):
        self.closed = True


class StyleScoreServiceTest(unittest.TestCase):
    def test_get_style_scores_returns_rows_and_closes_client(self):
        client = FakeClickHouseClient()

        result = StyleScoreService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 24),
        ).get_style_scores(
            min_confidence=0.5,
            industry_group_code="4530",
            limit=10,
        )

        self.assertTrue(client.closed)
        self.assertEqual(result.trade_date, date(2026, 5, 22))
        self.assertEqual(result.rows[0].rank, 1)
        self.assertEqual(result.rows[0].security_id, "SEC_A")
        self.assertEqual(result.rows[0].total_score, 77.0)
        self.assertEqual(result.rows[0].missing_factor_ids, ["eps_yoy_pct"])
        query, params = client.queries[1]
        self.assertIn("iss.industry_group_code = {industry_group_code:String}", query)
        self.assertIn("s.score_confidence >= {min_confidence:Float64}", query)
        self.assertEqual(params["trade_date"], "2026-05-22")
        self.assertEqual(params["style_profile"], "DEFAULT")
        self.assertEqual(params["limit"], 10)

    def test_get_style_score_detail_returns_factor_breakdown(self):
        client = FakeClickHouseClient()

        result = StyleScoreService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 24),
        ).get_style_score_detail("SEC_A")

        self.assertIsNotNone(result.row)
        self.assertEqual(result.row.trade_date, date(2026, 5, 22))
        self.assertEqual(result.factors[0].factor_id, "epr")
        self.assertEqual(result.factors[0].percentile_score, 75.0)
        self.assertEqual(len(client.queries), 3)
        self.assertIn("endsWith(s.security_id, concat('_', {security_id:String}))", client.queries[0][0])
        self.assertEqual(client.queries[0][1]["security_id"], "SEC_A")
        self.assertEqual(client.queries[1][1]["trade_date"], "2026-05-22")
        self.assertEqual(client.queries[2][1]["trade_date"], "2026-05-22")

    def test_get_style_score_components_returns_card_scores(self):
        client = FakeClickHouseClient()

        result = StyleScoreService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 24),
        ).get_style_score_components("SEC_A")

        by_key = {component.component_key: component for component in result.components}
        self.assertEqual(result.trade_date, date(2026, 5, 22))
        self.assertEqual(result.security_id, "SEC_A")
        self.assertEqual(result.stock_code, "000001")
        self.assertEqual(result.company_name, "A Corp")
        self.assertEqual(by_key["COMPOSITE"].score, 77.0)
        self.assertEqual(by_key["VALUE"].score, 70.0)
        self.assertEqual(by_key["QUALITY"].score, 80.0)
        self.assertEqual(by_key["DIVIDEND"].label, "Dividend & Shareholder Return")
        self.assertEqual(by_key["DIVIDEND"].score, 55.0)
        self.assertEqual(by_key["DIVIDEND"].required_factor_count, 5)
        self.assertEqual(by_key["DIVIDEND"].available_factor_count, 2)
        self.assertGreater(by_key["VALUE"].required_factor_count, 1)

    def test_get_component_detail_returns_weighted_factor_table(self):
        client = FakeClickHouseClient()

        result = StyleScoreService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 24),
        ).get_style_score_component_detail("SEC_A", "VALUE")

        by_factor = {factor.factor_id: factor for factor in result.factors}
        self.assertEqual(result.component.component_key, "VALUE")
        self.assertEqual(result.security_id, "SEC_A")
        self.assertEqual(result.company_name, "A Corp")
        self.assertAlmostEqual(by_factor["epr"].factor_weight, 0.25)
        self.assertAlmostEqual(by_factor["epr"].weighted_score, 18.75)
        self.assertEqual(by_factor["fcfpr"].invalid_reason, "MISSING")

    def test_dividend_component_detail_includes_market_dividend_yield(self):
        client = FakeClickHouseClient()

        result = StyleScoreService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 24),
        ).get_style_score_component_detail("SEC_A", "DIVIDEND")

        by_factor = {factor.factor_id: factor for factor in result.factors}
        self.assertIn("dividend_yield", by_factor)
        self.assertEqual(by_factor["dividend_yield"].label, "DIVIDEND_YIELD")
        self.assertAlmostEqual(by_factor["dividend_yield"].factor_weight, 0.30)
        self.assertAlmostEqual(by_factor["dividend_yield"].weighted_score, 24.0)
        self.assertAlmostEqual(by_factor["sharehold_div_yield"].factor_weight, 0.15)
        self.assertEqual(by_factor["sharehold_net_buyback_yield"].label, "NET_BUYBACK_YIELD")

    def test_resolve_available_trade_date_returns_latest_loaded_date(self):
        client = FakeClickHouseClient()

        result = _resolve_available_trade_date(client, date(2026, 5, 24), "DEFAULT")

        self.assertEqual(result, date(2026, 5, 22))

    def test_style_score_query_uses_sector_and_industry_group_not_industry_code(self):
        query = _build_style_score_list_query(
            has_min_confidence=True,
            has_industry_group_code=True,
            has_sector_code=True,
        )

        self.assertIn("iss.sector_code", query)
        self.assertIn("iss.industry_group_code", query)
        self.assertIn("iss.industry_group_name", query)
        self.assertNotIn("iss.industry_code", query)

    def test_detail_queries_accept_stock_code_like_identifier(self):
        style_query = _build_style_score_detail_query()
        factor_query = _build_factor_breakdown_query()

        self.assertIn("endsWith(s.security_id, concat('_', {security_id:String}))", style_query)
        self.assertIn("endsWith(security_id, concat('_', {security_id:String}))", factor_query)

    def test_app_registers_style_score_routes(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/api/style-scores", paths)
        self.assertIn("/api/style-scores/{security_id}", paths)
        self.assertIn("/api/style-scores/{security_id}/components", paths)
        self.assertIn("/api/style-scores/{security_id}/components/{component_key}", paths)


if __name__ == "__main__":
    unittest.main()
