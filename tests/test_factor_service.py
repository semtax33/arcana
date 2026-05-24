import unittest

from api.service.factor_service import FactorService


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        return FakeFrame(self.rows)

    def close(self):
        self.closed = True


class FactorServiceTest(unittest.TestCase):
    def test_get_factors_appends_style_score_virtual_factors(self):
        client = FakeClickHouseClient()

        result = FactorService(client_factory=lambda: client).get_factors(
            factor_type="style_score",
        )

        factor_ids = {factor.factor_id for factor in result}
        self.assertTrue(client.closed)
        self.assertIn("style_total_score", factor_ids)
        self.assertIn("style_value_score", factor_ids)
        self.assertTrue(all(factor.factor_type == "style_score" for factor in result))

    def test_get_factors_searches_virtual_style_factors(self):
        client = FakeClickHouseClient()

        result = FactorService(client_factory=lambda: client).get_factors(search="momentum")

        self.assertEqual([factor.factor_id for factor in result], ["style_momentum_score"])


if __name__ == "__main__":
    unittest.main()
