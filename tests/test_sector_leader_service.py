import tempfile
import unittest
from datetime import date
from pathlib import Path

from api.main import app
from api.service.sector_leader_service import (
    SectorLeaderService,
    _build_sector_leader_query,
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
        metric_rows=None,
        eps_catalog_rows=None,
        singular_metric_rows=None,
        plural_metric_rows=None,
    ):
        self.metric_rows = metric_rows or []
        self.eps_catalog_rows = eps_catalog_rows or []
        self.singular_metric_rows = singular_metric_rows
        self.plural_metric_rows = plural_metric_rows
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        params = parameters or {}
        self.queries.append((query, params))
        if "FROM factor_catalog" in query:
            return FakeFrame(self.eps_catalog_rows)
        if "FROM fact_daily_factor AS f" in query:
            if self.singular_metric_rows is not None:
                return FakeFrame(self.singular_metric_rows)
            return FakeFrame(self.metric_rows)
        if "FROM fact_daily_factors AS f" in query:
            if self.plural_metric_rows is not None:
                return FakeFrame(self.plural_metric_rows)
            return FakeFrame([])
        return FakeFrame([])

    def close(self):
        self.closed = True


class SectorLeaderServiceTest(unittest.TestCase):
    def test_get_sector_leaders_sorts_by_default_strong_stock_ratio_and_formats_na(self):
        client = FakeClickHouseClient(
            metric_rows=[
                _sector_row("10", stock_count=2, strong_stock_count=1, strong_stock_ratio=50, per=20),
                _sector_row("20", stock_count=3, strong_stock_count=3, strong_stock_ratio=100, per=10),
            ],
            eps_catalog_rows=[{"factor_id": "expected_eps_growth"}],
        )

        with _rules_file() as rules_path:
            result = SectorLeaderService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 23),
                gics_rules_path=rules_path,
            ).get_sector_leaders()

        self.assertEqual(result.as_of_date, date(2026, 5, 23))
        self.assertEqual(result.sort_by, "strong_stock_ratio")
        self.assertEqual(result.direction, "desc")
        self.assertEqual(result.eps_growth_factor_id, "expected_eps_growth")
        self.assertEqual([row.sector_code for row in result.rows], ["20", "10", "30"])
        self.assertEqual(result.rows[0].rank, 1)
        self.assertEqual(result.rows[0].strong_stock_ratio.display_value, "+100.00%")
        self.assertEqual(result.rows[2].strong_stock_ratio.display_value, "N/A")
        self.assertIsNone(result.rows[2].strong_stock_ratio.value)
        self.assertTrue(client.closed)

        metric_query_params = next(
            params for query, params in client.queries if "FROM fact_daily_factor AS f" in query
        )
        self.assertEqual(metric_query_params["as_of_date"], "2026-05-23")
        self.assertEqual(metric_query_params["near_high_ratio"], 0.97)
        self.assertIn("expected_eps_growth", metric_query_params["factor_ids"])

    def test_per_and_pbr_default_to_ascending_with_missing_values_last(self):
        client = FakeClickHouseClient(
            metric_rows=[
                _sector_row("10", per=20, pbr=2),
                _sector_row("20", per=10, pbr=None),
            ]
        )

        with _rules_file() as rules_path:
            result = SectorLeaderService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 23),
                gics_rules_path=rules_path,
            ).get_sector_leaders(sort_by="per")

        self.assertEqual(result.direction, "asc")
        self.assertEqual([row.sector_code for row in result.rows], ["20", "10", "30"])
        self.assertEqual(result.rows[0].per.display_value, "10.00x")

        with _rules_file() as rules_path:
            result = SectorLeaderService(
                client_factory=lambda: FakeClickHouseClient(metric_rows=[
                    _sector_row("10", per=20, pbr=2),
                    _sector_row("20", per=10, pbr=None),
                ]),
                today_factory=lambda: date(2026, 5, 23),
                gics_rules_path=rules_path,
            ).get_sector_leaders(sort_by="per", direction="desc")

        self.assertEqual([row.sector_code for row in result.rows], ["10", "20", "30"])

    def test_eps_growth_falls_back_to_eps_yoy_pct_when_expected_factor_is_absent(self):
        client = FakeClickHouseClient(metric_rows=[_sector_row("10", eps_expected_growth=12.5)])

        with _rules_file() as rules_path:
            result = SectorLeaderService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 23),
                gics_rules_path=rules_path,
            ).get_sector_leaders()

        self.assertEqual(result.eps_growth_factor_id, "eps_yoy_pct")
        metric_query_params = next(
            params for query, params in client.queries if "FROM fact_daily_factor AS f" in query
        )
        self.assertIn("eps_yoy_pct", metric_query_params["factor_ids"])

    def test_factor_metrics_fall_back_to_plural_factor_table_when_singular_is_empty(self):
        client = FakeClickHouseClient(
            singular_metric_rows=[],
            plural_metric_rows=[_sector_row("10", strong_stock_ratio=90)],
        )

        with _rules_file() as rules_path:
            result = SectorLeaderService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 23),
                gics_rules_path=rules_path,
            ).get_sector_leaders(limit=1)

        self.assertEqual(result.factor_source, "fact_daily_factors")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].sector_code, "10")

    def test_app_registers_new_sector_leaders_route_without_removing_sectors_route(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/api/sector-leaders", paths)
        self.assertIn("/api/sectors", paths)

    def test_strong_stock_ratio_query_uses_current_market_day_and_excludes_spacs(self):
        query = _build_sector_leader_query(
            factor_table="fact_daily_factors",
            value_column="factor_value",
        )

        self.assertIn("latest_market_date AS", query)
        self.assertIn(
            "lowerUTF8(coalesce(iss.legal_name_en, '')) NOT LIKE '%special purpose acquisition%'",
            query,
        )
        self.assertIn("max(trade_date) AS latest_trade_date", query)
        self.assertIn("trade_date = (SELECT latest_trade_date FROM latest_market_date)", query)
        self.assertIn("trade_date < (SELECT latest_trade_date FROM latest_market_date)", query)
        self.assertIn("h.prior_high_52w IS NOT NULL", query)
        self.assertIn("lp.latest_close IS NOT NULL", query)
        self.assertIn("lp.latest_high > h.prior_high_52w", query)
        self.assertIn(
            "lp.latest_close >= h.prior_high_52w * {near_high_ratio:Float64}",
            query,
        )
        self.assertIn("INNER JOIN latest_price AS lp", query)
        self.assertIn("countIf(strong_flag = 1) AS strong_stock_count", query)
        self.assertIn("countIf(strong_flag = 1) / count() * 100", query)
        self.assertNotIn("sum(strong_flag) AS strong_stock_count", query)


def _sector_row(
    sector_code,
    *,
    stock_count=1,
    strong_stock_count=0,
    strong_stock_ratio=None,
    return_1d=None,
    return_1w=None,
    roe=None,
    per=None,
    pbr=None,
    eps_expected_growth=None,
):
    return {
        "sector_code": sector_code,
        "stock_count": stock_count,
        "strong_stock_count": strong_stock_count,
        "strong_stock_ratio": strong_stock_ratio,
        "return_1d": return_1d,
        "return_1w": return_1w,
        "roe": roe,
        "per": per,
        "pbr": pbr,
        "eps_expected_growth": eps_expected_growth,
    }


class _rules_file:
    def __enter__(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        path = Path(self._temp_dir.name) / "gics_rules.yaml"
        path.write_text(
            "sectors:\n"
            "  '10': Energy\n"
            "  '20': Industrials\n"
            "  '30': Consumer Staples\n",
            encoding="utf-8",
        )
        return path

    def __exit__(self, exc_type, exc, tb):
        self._temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
