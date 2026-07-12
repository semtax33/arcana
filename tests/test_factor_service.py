import unittest

from api.service.factor_service import FactorService


def _factor_row(
    factor_id,
    *,
    factor_name=None,
    factor_type="quality",
    factor_group="quality",
    unit="percent",
    value_direction="HIGHER_BETTER",
    description="",
    is_active=True,
):
    return {
        "factor_id": factor_id,
        "factor_name": factor_name or factor_id,
        "factor_type": factor_type,
        "factor_group": factor_group,
        "unit": unit,
        "value_direction": value_direction,
        "description": description,
        "is_active": is_active,
    }


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

    def test_get_factors_removes_duplicate_catalog_rows_by_factor_id(self):
        client = FakeClickHouseClient(
            [
                _factor_row("roe", factor_name="ROE duplicate"),
                _factor_row("roe", factor_name="Return on Equity"),
            ]
        )

        result = FactorService(client_factory=lambda: client).get_factors()

        roe_factors = [factor for factor in result if factor.factor_id.strip().lower() == "roe"]
        self.assertEqual(len(roe_factors), 1)
        self.assertEqual(roe_factors[0].factor_name, "Return on Equity")

    def test_get_factors_prefers_virtual_style_factor_over_catalog_duplicate(self):
        client = FakeClickHouseClient(
            [
                _factor_row(
                    "style_total_score",
                    factor_name="DB Style Score",
                    factor_type="style_score",
                    factor_group="style_score",
                    unit="score",
                    value_direction="LOWER_BETTER",
                )
            ]
        )

        result = FactorService(client_factory=lambda: client).get_factors(
            factor_type="style_score",
        )

        matching = [factor for factor in result if factor.factor_id == "style_total_score"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].factor_name, "Style Score")
        self.assertEqual(matching[0].value_direction, "HIGHER_BETTER")


if __name__ == "__main__":
    unittest.main()
