import unittest
from datetime import date

from api.service.financial_ratios_service import FinancialRatiosService


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(self, ratio_rows, catalog_rows=None, metadata_rows=None):
        self.ratio_rows = ratio_rows
        self.catalog_rows = catalog_rows or []
        self.metadata_rows = metadata_rows or []
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        params = parameters or {}
        self.queries.append((query, params))
        if "FROM system.columns" in query:
            table_name = params.get("table_name")
            if table_name == "fact_daily_factor":
                return FakeFrame(
                    [
                        {"name": "security_id"},
                        {"name": "trade_date"},
                        {"name": "factor_id"},
                        {"name": "financial_basis"},
                        {"name": "factor_value"},
                        {"name": "fiscal_year"},
                        {"name": "financial_period"},
                        {"name": "currency"},
                        {"name": "updated_at"},
                    ]
                )
            return FakeFrame([])
        if "FROM fact_daily_factor" in query:
            basis = set(params.get("financial_basis", []))
            return FakeFrame(
                [
                    row
                    for row in self.ratio_rows
                    if not basis or row.get("financial_basis") in basis
                ]
            )
        if "FROM factor_catalog" in query:
            return FakeFrame(self.catalog_rows)
        if "FROM security_master" in query:
            return FakeFrame(self.metadata_rows)
        return FakeFrame([])

    def close(self):
        self.closed = True


class FinancialRatiosServiceTest(unittest.TestCase):
    def test_get_ratios_returns_statement_sections_groups_trend_and_growth(self):
        client = FakeClickHouseClient(
            [
                _row("gpm", "annual", 2024, "2024-12-31", 30),
                _row("gpm", "annual", 2025, "2025-12-31", 36),
                _row("opm", "annual", 2025, "2025-12-31", 12),
                _row("current_ratio", "annual", 2025, "2025-12-31", 1.2),
                _row("cfo_yoy_pct", "annual", 2025, "2025-12-31", 15),
            ],
            [
                _catalog("gpm", "Gross Margin", "percent"),
                _catalog("opm", "Operating Margin", "percent"),
                _catalog("current_ratio", "Current Ratio", "times"),
                _catalog("cfo_yoy_pct", "CFO Growth", "percent"),
            ],
            [{"ticker": "236200", "stock_name": "Supermicro", "country": "KR"}],
        )

        result = FinancialRatiosService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 22),
        ).get_ratios("236200", period="annual")

        self.assertEqual(result.stock.stock_code, "236200")
        self.assertEqual(result.period, "annual")
        self.assertEqual(result.financial_basis, "annual")
        self.assertEqual([column.key for column in result.columns], ["2024-12-31", "2025-12-31"])
        self.assertEqual([section.statement_type for section in result.sections], ["IS", "BS", "CF"])

        income_group = result.sections[0].groups[0]
        self.assertEqual(income_group.group_key, "profitability")
        gross_margin = income_group.ratios[0]
        self.assertEqual(gross_margin.factor_id, "gpm")
        self.assertEqual([cell.display_value for cell in gross_margin.values], ["30.00%", "36.00%"])
        self.assertEqual(gross_margin.trend[1].value, 36)
        self.assertEqual(gross_margin.growth_chart[1].value, 20)

        opm = income_group.ratios[1]
        self.assertEqual(opm.values[0].display_value, "N/A")
        self.assertIsNone(opm.trend[0].value)

        self.assertTrue(client.closed)

    def test_quarter_period_uses_quarterly_basis_and_limits_to_latest_20_columns(self):
        rows = [
            _row("gpm", "quarterly", 2020 + index // 4, f"{2020 + index // 4}-{(index % 4 + 1) * 3:02d}-28", index)
            for index in range(24)
        ]
        client = FakeClickHouseClient(rows, [_catalog("gpm", "Gross Margin", "percent")])

        result = FinancialRatiosService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 22),
        ).get_ratios("5930", period="quarter")

        self.assertEqual(result.financial_basis, "quarterly")
        self.assertEqual(len(result.columns), 20)
        self.assertEqual(result.columns[0].key, "2021-03-28")
        ratio_query_params = next(params for query, params in client.queries if "FROM fact_daily_factor" in query)
        self.assertEqual(ratio_query_params["stock_code"], "005930")
        self.assertEqual(ratio_query_params["financial_basis"], ["quarterly", "quarter"])

    def test_get_ratios_supports_us_market_security_id_and_currency(self):
        client = FakeClickHouseClient(
            [
                {
                    "factor_id": "gpm",
                    "financial_basis": "annual",
                    "fiscal_year": 2025,
                    "fiscal_month": 12,
                    "period_end_date": "2025-12-31",
                    "value": 42,
                    "currency": "USD",
                }
            ],
            [_catalog("gpm", "Gross Margin", "percent")],
            [{"ticker": "AAPL", "stock_name_en": "Apple Inc.", "security_country": "US", "security_currency": "USD"}],
        )

        result = FinancialRatiosService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 22),
        ).get_ratios("aapl", period="annual", market="us")

        ratio_query_params = next(params for query, params in client.queries if "FROM fact_daily_factor" in query)
        self.assertEqual(ratio_query_params["stock_code"], "AAPL")
        self.assertEqual(ratio_query_params["security_id"], "SEC_US_AAPL")
        self.assertEqual(ratio_query_params["default_currency"], "USD")
        self.assertEqual(result.stock.stock_code, "AAPL")
        self.assertEqual(result.stock.security_id, "SEC_US_AAPL")
        self.assertEqual(result.stock.stock_name, "Apple Inc.")
        self.assertEqual(result.stock.country, "US")
        self.assertEqual(result.stock.currency, "USD")


def _row(factor_id, financial_basis, fiscal_year, financial_period, value):
    return {
        "factor_id": factor_id,
        "financial_basis": financial_basis,
        "fiscal_year": fiscal_year,
        "fiscal_month": int(str(financial_period)[5:7]),
        "period_end_date": financial_period,
        "value": value,
        "currency": "KRW",
    }


def _catalog(factor_id, factor_name, unit):
    return {
        "factor_id": factor_id,
        "factor_name": factor_name,
        "unit": unit,
        "value_direction": "HIGHER_BETTER",
        "description": "",
    }


if __name__ == "__main__":
    unittest.main()
