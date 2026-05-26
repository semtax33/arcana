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


class FakeClickHouseClient:
    def __init__(self):
        self.closed = False
        self.queries = []

    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "latest_factor_values AS" in query:
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
        return FakeDataFrame([])

    def close(self):
        self.closed = True


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


if __name__ == "__main__":
    unittest.main()
