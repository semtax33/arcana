import unittest
from datetime import date

try:
    from api.service.dto import FactorConditionDto, FactorScreenRequestDto
    from api.service.factor_screen_service import FactorScreenService
except ModuleNotFoundError as exc:  # pragma: no cover - local env dependency guard
    API_SERVICE_DEPS_ERROR = exc
else:
    API_SERVICE_DEPS_ERROR = None


class FakeDataFrame:
    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeDataFrame only supports records orientation")
        return self._records


class FakeQueryResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClickHouseClient:
    def __init__(self):
        self.closed = False
        self.queries = []

    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "latest_factor_values AS" in query:
            if parameters.get("condition_0_factor_id") == "roe":
                return FakeDataFrame(
                    [
                        {
                            "security_id": "SEC_KR_A",
                            "ticker": "A",
                            "issuer_name": "A Corp",
                            "country": "KR",
                            "market_cap": 1000,
                            "sector_code": "45",
                            "industry_group_code": "4510",
                            "industry_group_name": "Software",
                            "matched_condition_count": 2,
                            "matched_conditions": [
                                "0:top_percent:roe",
                                "1:threshold:roe",
                            ],
                            "latest_trade_date": date(2026, 5, 17),
                            "roe_0_value": 12.5,
                            "roe_0_trade_date": date(2026, 5, 17),
                            "roe_1_value": 9.5,
                            "roe_1_trade_date": date(2026, 5, 17),
                        }
                    ]
                )
            return FakeDataFrame(
                [
                    {
                        "security_id": "SEC_KR_A",
                        "ticker": "A",
                        "issuer_name": "A Corp",
                        "country": "KR",
                        "market_cap": 1000,
                        "sector_code": "45",
                        "industry_group_code": "4510",
                        "industry_group_name": "Software",
                        "matched_condition_count": 1,
                        "matched_conditions": ["0:top_percent:style_total_score"],
                        "latest_trade_date": date(2026, 5, 17),
                        "style_total_score_0_value": 82,
                        "style_total_score_0_trade_date": date(2026, 5, 17),
                    }
                ]
            )
        if "FROM factor_catalog" in query:
            return FakeDataFrame(
                [
                    {
                        "factor_id": "roe",
                        "factor_name": "ROE",
                        "unit": "percent",
                        "value_direction": "HIGHER_BETTER",
                    }
                ]
            )
        return FakeDataFrame([])

    def close(self):
        self.closed = True


class SnapshotAvailableFakeClickHouseClient(FakeClickHouseClient):
    def query(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if query.startswith("EXISTS TABLE fact_daily_factor_snapshot"):
            return FakeQueryResult([(1,)])
        if "FROM fact_daily_factor_snapshot" in query:
            return FakeQueryResult([(len((parameters or {}).get("factor_ids", [])),)])
        return FakeQueryResult([])


class OtherMarketOnlySnapshotFakeClickHouseClient(SnapshotAvailableFakeClickHouseClient):
    def query(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if query.startswith("EXISTS TABLE fact_daily_factor_snapshot"):
            return FakeQueryResult([(1,)])
        if "FROM fact_daily_factor_snapshot" in query:
            self.assert_market_prefix = (parameters or {}).get("market_security_prefix")
            return FakeQueryResult([(0,)])
        return FakeQueryResult([])


@unittest.skipIf(
    API_SERVICE_DEPS_ERROR is not None,
    f"API service dependencies are not available: {API_SERVICE_DEPS_ERROR}",
)
class FactorScreenServiceTest(unittest.TestCase):
    def test_style_score_screen_uses_minervini_profile_when_request_uses_default(self):
        client = FakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="style_total_score",
                    mode="top_percent",
                    top_percent=30,
                )
            ],
            style_profile="DEFAULT",
        )

        result = FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        self.assertTrue(client.closed)
        self.assertEqual(result.total_count, 1)
        screen_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(screen_queries), 1)
        _, params = screen_queries[0]
        self.assertEqual(params["style_profile"], "MINERVINI_ZWEIG")

    def test_screen_passes_market_filter_to_repository_query(self):
        client = FakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="roe",
                    mode="top_percent",
                    top_percent=30,
                )
            ],
            market="us",
        )

        FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        screen_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(screen_queries), 1)
        query, params = screen_queries[0]
        self.assertEqual(params["market_country"], "US")
        self.assertIn("AND sm.country = {market_country:String}", query)

    def test_screen_deduplicates_display_factors_without_dropping_conditions(self):
        client = FakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="roe",
                    mode="top_percent",
                    top_percent=30,
                ),
                FactorConditionDto(
                    factor_id="ROE",
                    mode="threshold",
                    operator=">",
                    value=10,
                ),
            ],
        )

        result = FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        screen_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(screen_queries), 1)
        _, params = screen_queries[0]
        self.assertEqual(params["required_condition_count"], 2)
        self.assertEqual(params["factor_ids"], ["roe"])
        self.assertEqual(params["condition_0_factor_id"], "roe")
        self.assertEqual(params["condition_1_factor_id"], "roe")
        self.assertEqual(len(result.factor_columns), 1)
        self.assertEqual(result.factor_columns[0].factor_id, "roe")
        self.assertEqual(len(result.rows[0].factor_values), 1)
        self.assertEqual(result.rows[0].factor_values["roe_0"].value, 12.5)
        self.assertEqual(result.rows[0].matched_condition_count, 2)

    def test_screen_uses_snapshot_table_when_snapshot_rows_exist(self):
        client = SnapshotAvailableFakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="roe",
                    mode="top_percent",
                    top_percent=30,
                )
            ],
            limit=25,
        )

        FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        screen_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(screen_queries), 1)
        query, params = screen_queries[0]
        self.assertEqual(params["limit"], 25)
        self.assertIn("FROM fact_daily_factor_snapshot AS f", query)
        self.assertIn("f.trade_date = (SELECT latest_date FROM latest_trade_date)", query)

        coverage_queries = [
            (query, params)
            for query, params in client.queries
            if "countDistinct(factor_id) AS factor_count" in query
        ]
        self.assertEqual(len(coverage_queries), 1)
        coverage_query, coverage_params = coverage_queries[0]
        self.assertIn("latest_snapshot_date AS", coverage_query)
        self.assertIn("latest_raw_date AS", coverage_query)
        self.assertIn("ORDER BY trade_date DESC", coverage_query)
        self.assertIn(
            "trade_date >= (SELECT trade_date FROM latest_raw_date)",
            coverage_query,
        )
        self.assertIn(
            "source_trade_date <= {as_of_date:Date}",
            coverage_query,
        )
        self.assertIn("FROM fact_daily_factors", coverage_query)
        self.assertIn("as_of_date", coverage_params)

    def test_screen_accepts_carry_forward_snapshot_on_market_holiday(self):
        client = SnapshotAvailableFakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="roe",
                    mode="top_percent",
                    top_percent=30,
                )
            ],
            as_of_date=date(2026, 7, 18),
            market="kr",
        )

        FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        screen_query = next(
            query for query, _ in client.queries if "latest_factor_values AS" in query
        )
        self.assertIn("FROM fact_daily_factor_snapshot AS f", screen_query)
        coverage_query, coverage_params = next(
            (query, params)
            for query, params in client.queries
            if "countDistinct(factor_id) AS factor_count" in query
        )
        self.assertEqual(coverage_params["as_of_date"], "2026-07-18")
        self.assertEqual(coverage_params["market_security_prefix"], "SEC_KR_")
        self.assertIn(
            "trade_date >= (SELECT trade_date FROM latest_raw_date)",
            coverage_query,
        )
        self.assertIn(
            "source_trade_date <= {as_of_date:Date}",
            coverage_query,
        )

    def test_screen_ignores_snapshot_rows_that_exist_only_for_another_market(self):
        client = OtherMarketOnlySnapshotFakeClickHouseClient()
        request = FactorScreenRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="roe",
                    mode="top_percent",
                    top_percent=30,
                )
            ],
            market="kr",
        )

        FactorScreenService(client_factory=lambda: client).screen_stocks(request)

        screen_query = next(
            query for query, _ in client.queries if "latest_factor_values AS" in query
        )
        self.assertIn("FROM fact_daily_factors AS f", screen_query)
        self.assertEqual(client.assert_market_prefix, "SEC_KR_")
        self.assertIn(
            "startsWith(security_id, {market_security_prefix:String})",
            screen_query,
        )


if __name__ == "__main__":
    unittest.main()
