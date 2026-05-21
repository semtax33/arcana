import tempfile
import unittest
from datetime import date
from pathlib import Path

from api.service.introduction_service import (
    IntroductionService,
    StockIntroductionNotFoundError,
    _normalize_stock_code,
)


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(
        self,
        *,
        metadata_rows=None,
        factor_rows=None,
        plural_factor_rows=None,
        fail_singular_factor_table=False,
        fail_factor_value_column=False,
        price_rows=None,
        issuer_columns=None,
        description_rows=None,
    ):
        self.metadata_rows = metadata_rows or []
        self.factor_rows = factor_rows or []
        self.plural_factor_rows = plural_factor_rows or []
        self.fail_singular_factor_table = fail_singular_factor_table
        self.fail_factor_value_column = fail_factor_value_column
        self.price_rows = price_rows or []
        self.issuer_columns = issuer_columns or []
        self.description_rows = description_rows or []
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "FROM security_master" in query:
            return FakeFrame(self.metadata_rows)
        if "FROM fact_daily_factor AS f" in query:
            if self.fail_singular_factor_table:
                raise RuntimeError("missing table")
            if self.fail_factor_value_column and "argMax(f.factor_value" in query:
                raise RuntimeError("missing factor_value column")
            return FakeFrame(self.factor_rows)
        if "FROM fact_daily_factors AS f" in query:
            if self.fail_factor_value_column and "argMax(f.factor_value" in query:
                raise RuntimeError("missing factor_value column")
            return FakeFrame(self.plural_factor_rows)
        if "FROM stock_shares" in query:
            raise AssertionError("introduction metrics must come from fact_daily_factor")
        if "FROM stock_dividend" in query:
            raise AssertionError("introduction metrics must come from fact_daily_factor")
        if "FROM price_daily" in query:
            return FakeFrame(self.price_rows)
        if "FROM system.columns" in query:
            return FakeFrame([{"name": name} for name in self.issuer_columns])
        if "FROM issuers" in query:
            return FakeFrame(self.description_rows)
        return FakeFrame([])

    def close(self):
        self.closed = True


class IntroductionServiceTest(unittest.TestCase):
    def test_get_introduction_returns_metrics_company_and_business_area(self):
        client = FakeClickHouseClient(
            metadata_rows=[
                {
                    "security_id": "SEC_KR_005930",
                    "issuer_id": "ISSUER_ID_005930",
                    "ticker": "005930",
                    "stock_name": "삼성전자",
                    "stock_name_en": "Samsung Electronics",
                    "country": "KR",
                    "sector_schema": "GICS",
                    "sector_code": "45",
                }
            ],
            factor_rows=[
                {"factor_id": "per", "factor_value": 12.4, "currency": "KRW"},
                {"factor_id": "mcap_mil", "factor_value": 500.0, "currency": "KRW"},
                {"factor_id": "dividend_yield", "factor_value": 2.1, "currency": "KRW"},
            ],
            price_rows=[
                {
                    "row_count": 252,
                    "high_52w": 120.0,
                    "low_52w": 80.0,
                    "latest_close": 100.0,
                    "latest_trade_date": date(2026, 5, 21),
                    "currency": "KRW",
                }
            ],
            issuer_columns=["company_description"],
            description_rows=[{"description": "반도체와 전자제품을 제조합니다."}],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            rules_path = Path(temp_dir) / "gics_rules.yaml"
            rules_path.write_text("sectors:\n  '45': Information Technology\n", encoding="utf-8")

            result = IntroductionService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                gics_rules_path=rules_path,
            ).get_introduction("5930")

        self.assertEqual(result.stock.stock_code, "005930")
        self.assertEqual(result.stock.stock_name, "삼성전자")
        self.assertEqual(result.metrics.market_cap, 500_000_000.0)
        self.assertEqual(result.metrics.trailing_per, 12.4)
        self.assertEqual(result.metrics.dividend_yield, 2.1)
        self.assertEqual(result.metrics.fifty_two_week_range_pct, 50.0)
        self.assertEqual(result.metrics.latest_trade_date, date(2026, 5, 21))
        self.assertEqual(result.company.description, "반도체와 전자제품을 제조합니다.")
        self.assertEqual(result.business_areas[0].sector_code, "45")
        self.assertEqual(result.business_areas[0].sector_name, "Information Technology")
        self.assertTrue(any("FROM fact_daily_factor" in query for query, _ in client.queries))
        self.assertFalse(any("max(trade_date) AS trade_date" in query for query, _ in client.queries))
        self.assertTrue(any("isFinite(f.factor_value)" in query for query, _ in client.queries))
        self.assertFalse(any("FROM stock_shares" in query for query, _ in client.queries))
        self.assertFalse(any("FROM stock_dividend" in query for query, _ in client.queries))
        self.assertTrue(client.closed)

    def test_get_introduction_uses_metric_factor_aliases(self):
        client = FakeClickHouseClient(
            factor_rows=[
                {"factor_id": "per", "factor_value": 9.5},
                {"factor_id": "market_cap", "factor_value": 123_000_000.0},
                {"factor_id": "sharehold_div_yield", "factor_value": 1.7},
            ]
        )

        result = IntroductionService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 21),
        ).get_introduction("005930")

        self.assertEqual(result.metrics.market_cap, 123_000_000.0)
        self.assertEqual(result.metrics.trailing_per, 9.5)
        self.assertEqual(result.metrics.dividend_yield, 1.7)

    def test_get_introduction_falls_back_to_plural_factor_table(self):
        client = FakeClickHouseClient(
            fail_singular_factor_table=True,
            plural_factor_rows=[
                {"factor_id": "per", "factor_value": 8.1},
                {"factor_id": "mcap_mil", "factor_value": 321.0},
                {"factor_id": "dividend_yield", "factor_value": 2.3},
            ],
        )

        result = IntroductionService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 21),
        ).get_introduction("005930")

        self.assertEqual(result.factor_source, "fact_daily_factors")
        self.assertEqual(result.metrics.market_cap, 321_000_000.0)
        self.assertEqual(result.metrics.trailing_per, 8.1)
        self.assertEqual(result.metrics.dividend_yield, 2.3)

    def test_get_introduction_accepts_uppercase_factor_ids_and_value_column(self):
        client = FakeClickHouseClient(
            fail_factor_value_column=True,
            factor_rows=[
                {"factor_id": "PER", "factor_value": 7.2},
                {"factor_id": "MARKET_CAP", "factor_value": 456_000_000.0},
                {"factor_id": "DIV_YIELD", "factor_value": 1.9},
            ],
        )

        result = IntroductionService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 21),
        ).get_introduction("005930")

        self.assertEqual(result.metrics.market_cap, 456_000_000.0)
        self.assertEqual(result.metrics.trailing_per, 7.2)
        self.assertEqual(result.metrics.dividend_yield, 1.9)

    def test_get_introduction_leaves_company_description_empty_when_column_is_missing(self):
        client = FakeClickHouseClient(
            metadata_rows=[
                {
                    "security_id": "SEC_KR_005930",
                    "issuer_id": "ISSUER_ID_005930",
                    "ticker": "005930",
                    "stock_name": "삼성전자",
                }
            ],
            factor_rows=[{"factor_id": "per", "factor_value": 10.0}],
        )

        result = IntroductionService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 21),
        ).get_introduction("005930")

        self.assertEqual(result.company.description, "")
        self.assertFalse(any("any(company_description)" in query for query, _ in client.queries))

    def test_get_introduction_raises_not_found_when_every_source_is_empty(self):
        client = FakeClickHouseClient()

        with self.assertRaises(StockIntroductionNotFoundError):
            IntroductionService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
            ).get_introduction("005930")

    def test_normalize_stock_code_pads_and_rejects_unsafe_values(self):
        self.assertEqual(_normalize_stock_code("5930"), "005930")

        with self.assertRaises(ValueError):
            _normalize_stock_code("005930;DROP")


if __name__ == "__main__":
    unittest.main()
