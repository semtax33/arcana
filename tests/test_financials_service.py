import tempfile
import unittest
from datetime import date
from pathlib import Path

from api.service.financials_service import FinancialStatementsService


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(self, statement_rows, metadata_rows=None):
        self.statement_rows = statement_rows
        self.metadata_rows = metadata_rows or []
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "FROM system.columns" in query:
            return FakeFrame(
                [
                    {"name": "security_id"},
                    {"name": "statement_type"},
                    {"name": "canonical_account_id"},
                    {"name": "canonical_account_name"},
                    {"name": "fiscal_year"},
                    {"name": "fiscal_month"},
                    {"name": "normalized_amount"},
                    {"name": "currency"},
                    {"name": "updated_at"},
                ]
            )
        if "FROM fact_canonical_statements" in query:
            return FakeFrame(self.statement_rows)
        if "FROM security_master" in query:
            return FakeFrame(self.metadata_rows)
        return FakeFrame([])

    def close(self):
        self.closed = True


class FinancialStatementsServiceTest(unittest.TestCase):
    def test_get_statements_returns_sections_values_trend_and_growth(self):
        client = FakeClickHouseClient(
            [
                _row("IS", "REVENUE", 2024, 12, 1000),
                _row("IS", "REVENUE", 2025, 12, 1200),
                _row("IS", "COGS", 2025, 12, 500),
                _row("BS", "TOTAL_ASSETS", 2024, 12, 3000),
                _row("BS", "TOTAL_ASSETS", 2025, 12, 3600),
                _row("CF", "CFO", 2025, 12, 700),
            ],
            [{"ticker": "236200", "stock_name": "슈프리마", "country": "KR"}],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            catalog_path = _catalog_path(temp_dir)
            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=catalog_path,
            ).get_statements("236200", period="annual")

        self.assertEqual(result.stock.stock_code, "236200")
        self.assertEqual([section.statement_type for section in result.sections], ["IS", "BS", "CF"])
        self.assertEqual([column.key for column in result.columns], ["2024-12-31", "2025-12-31"])

        income_accounts = result.sections[0].accounts
        revenue = income_accounts[0]
        cogs = income_accounts[1]
        self.assertEqual(revenue.canonical_id, "REVENUE")
        self.assertEqual(revenue.values[1].value, 1200)
        self.assertEqual(revenue.growth_chart[1].value, 20)
        self.assertEqual(cogs.values[0].display_value, "N/A")
        self.assertIsNone(cogs.trend[0].value)
        self.assertTrue(client.closed)

    def test_quarter_and_ttm_derive_flow_values_from_ytd_rows(self):
        client = FakeClickHouseClient(
            [
                _row("IS", "REVENUE", 2025, 3, 100),
                _row("IS", "REVENUE", 2025, 6, 250),
                _row("IS", "REVENUE", 2025, 9, 450),
                _row("IS", "REVENUE", 2025, 12, 700),
                _row("CF", "CFO", 2025, 3, 80),
                _row("CF", "CFO", 2025, 6, 170),
                _row("CF", "CFO", 2025, 9, 260),
                _row("CF", "CFO", 2025, 12, 360),
                _row("CF", "CAPEX_PPE", 2025, 3, -10),
                _row("CF", "CAPEX_PPE", 2025, 6, -30),
                _row("CF", "CAPEX_PPE", 2025, 9, -60),
                _row("CF", "CAPEX_PPE", 2025, 12, -100),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
            )
            quarter = service.get_statements("236200", period="quarter", statement="IS")
            ttm = service.get_statements("236200", period="ttm", statement="CF")

        revenue = quarter.sections[0].accounts[0]
        self.assertEqual([cell.value for cell in revenue.values], [100, 150, 200, 250])

        cash_accounts = {account.canonical_id: account for account in ttm.sections[0].accounts}
        self.assertEqual(cash_accounts["CFO"].values[-1].value, 360)
        self.assertEqual(cash_accounts["FCF"].values[-1].value, 260)
        self.assertTrue(cash_accounts["FCF"].is_derived)

    def test_unit_scale_outlier_is_repaired_before_display(self):
        client = FakeClickHouseClient(
            [
                _row("IS", "REVENUE", 2020, 12, 57_769_865_908),
                _row("IS", "REVENUE", 2021, 12, 72_572_220_105_000),
                _row("IS", "REVENUE", 2022, 12, 89_397_131_101),
                _row("BS", "TOTAL_ASSETS", 2020, 12, 153_834_347_663),
                _row("BS", "TOTAL_ASSETS", 2021, 12, 175_742_273_198_000),
                _row("BS", "TOTAL_ASSETS", 2022, 12, 193_787_246_304),
                _row("CF", "CFO", 2020, 12, 14_997_049_074),
                _row("CF", "CFO", 2021, 12, 17_714_321_978_000),
                _row("CF", "CFO", 2022, 12, 18_866_076_557),
                _row("IS", "BASIC_EPS", 2020, 12, 1317),
                _row("IS", "BASIC_EPS", 2021, 12, 3172),
                _row("IS", "BASIC_EPS", 2022, 12, 2563),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
            ).get_statements("236200", period="annual")

        accounts = {
            account.canonical_id: account
            for section in result.sections
            for account in section.accounts
        }
        self.assertEqual(accounts["REVENUE"].values[1].value, 72_572_220_105)
        self.assertEqual(accounts["TOTAL_ASSETS"].values[1].value, 175_742_273_198)
        self.assertEqual(accounts["CFO"].values[1].value, 17_714_321_978)
        self.assertEqual(accounts["BASIC_EPS"].values[1].value, 3172)

    def test_latest_unit_scale_outlier_is_repaired_before_display(self):
        client = FakeClickHouseClient(
            [
                _row("IS", "REVENUE", 2023, 12, 94_630_270_174),
                _row("IS", "REVENUE", 2024, 12, 108_231_735_860),
                _row("IS", "REVENUE", 2025, 12, 137_302_003_098_000),
                _row("IS", "GROSS_PROFIT", 2023, 12, 58_113_467_430),
                _row("IS", "GROSS_PROFIT", 2024, 12, 70_487_526_778),
                _row("IS", "GROSS_PROFIT", 2025, 12, 90_788_212_457_000),
                _row("IS", "OPERATING_INCOME", 2023, 12, 16_661_341_796),
                _row("IS", "OPERATING_INCOME", 2024, 12, 23_285_437_816),
                _row("IS", "OPERATING_INCOME", 2025, 12, 32_744_704_137_000),
                _row("IS", "BASIC_EPS", 2023, 12, 3316),
                _row("IS", "BASIC_EPS", 2024, 12, 4709),
                _row("IS", "BASIC_EPS", 2025, 12, 4662),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
            ).get_statements("236200", period="annual")

        accounts = {
            account.canonical_id: account
            for section in result.sections
            for account in section.accounts
        }
        self.assertEqual(accounts["REVENUE"].values[-1].value, 137_302_003_098)
        self.assertEqual(accounts["REVENUE"].values[-1].display_value, "137.3K")
        self.assertEqual(accounts["REVENUE"].unit, "KRW_MILLION")
        self.assertEqual(accounts["GROSS_PROFIT"].values[-1].value, 90_788_212_457)
        self.assertEqual(accounts["GROSS_PROFIT"].values[-1].display_value, "90.8K")
        self.assertEqual(accounts["OPERATING_INCOME"].values[-1].value, 32_744_704_137)
        self.assertEqual(accounts["OPERATING_INCOME"].values[-1].display_value, "32.7K")
        self.assertEqual(accounts["BASIC_EPS"].values[-1].value, 4662)
        self.assertEqual(accounts["BASIC_EPS"].values[-1].display_value, "4.7K")
        self.assertEqual(accounts["BASIC_EPS"].unit, "KRW_PER_SHARE")

    def test_get_account_detail_returns_single_account(self):
        client = FakeClickHouseClient([_row("BS", "TOTAL_ASSETS", 2025, 12, 5000)])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
            ).get_account_detail("236200", "TOTAL_ASSETS")

        self.assertEqual(result.statement_type, "BS")
        self.assertEqual(result.account.account_name, "자산총계")
        self.assertEqual(result.account.statistics.latest, 5000)

    def test_local_csv_fallback_reads_consolidated_statement_file(self):
        client = FakeClickHouseClient([])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normalized_dir = root / "normalized"
            normalized_dir.mkdir()
            (normalized_dir / "kr_normalized_236200.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount,fiscal_year,fiscal_month,fiscal_quarter",
                        "REVENUE,Revenue,Revenue,IS,2025.12,1200,2025,12,4",
                    ]
                ),
                encoding="utf-8",
            )

            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
                normalized_statement_dir=normalized_dir,
            ).get_statements("236200", period="annual", statement="IS")

        self.assertEqual(result.columns[0].key, "2025-12-31")
        self.assertEqual(result.sections[0].accounts[0].values[0].value, 1200)

    def test_get_statements_supports_us_market_security_id_and_currency(self):
        client = FakeClickHouseClient(
            [
                {
                    "statement_type": "IS",
                    "canonical_id": "REVENUE",
                    "account_name": "Revenue",
                    "fiscal_year": 2025,
                    "fiscal_month": 12,
                    "period_end_date": None,
                    "value": 383_285_000_000,
                    "currency": "USD",
                }
            ],
            [{"ticker": "AAPL", "stock_name_en": "Apple Inc.", "security_country": "US", "security_currency": "USD"}],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
            ).get_statements("aapl", period="annual", statement="IS", market="us")

        fact_query_params = next(params for query, params in client.queries if "FROM fact_canonical_statements" in query)
        self.assertEqual(fact_query_params["stock_code"], "AAPL")
        self.assertEqual(fact_query_params["security_id"], "SEC_US_AAPL")
        self.assertEqual(fact_query_params["default_currency"], "USD")
        self.assertEqual(result.stock.stock_code, "AAPL")
        self.assertEqual(result.stock.security_id, "SEC_US_AAPL")
        self.assertEqual(result.stock.stock_name, "Apple Inc.")
        self.assertEqual(result.stock.country, "US")
        self.assertEqual(result.stock.currency, "USD")
        self.assertEqual(result.sections[0].accounts[0].unit, "USD_MILLION")

    def test_us_local_csv_fallback_reads_sec_normalized_statement_file(self):
        client = FakeClickHouseClient([])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normalized_dir = root / "normalized"
            normalized_dir.mkdir()
            (normalized_dir / "us_normalized_AAPL.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount,fiscal_year,fiscal_month,fiscal_quarter",
                        "REVENUE,Revenue,Revenue,IS,2025.12,383285000000,2025,12,4",
                    ]
                ),
                encoding="utf-8",
            )

            result = FinancialStatementsService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
                canonical_accounts_path=_catalog_path(temp_dir),
                normalized_statement_dir=normalized_dir,
            ).get_statements("AAPL", period="annual", statement="IS", market="us")

        self.assertEqual(result.stock.security_id, "SEC_US_AAPL")
        self.assertEqual(result.stock.currency, "USD")
        self.assertEqual(result.sections[0].accounts[0].currency, "USD")
        self.assertEqual(result.sections[0].accounts[0].values[0].value, 383_285_000_000)


def _row(statement_type, canonical_id, fiscal_year, fiscal_month, value):
    return {
        "statement_type": statement_type,
        "canonical_id": canonical_id,
        "account_name": canonical_id,
        "fiscal_year": fiscal_year,
        "fiscal_month": fiscal_month,
        "period_end_date": None,
        "value": value,
        "currency": "KRW",
    }


def _catalog_path(temp_dir):
    path = Path(temp_dir) / "CanonicalAccount.csv"
    path.write_text(
        "\n".join(
            [
                "canonical_id,canonical_nm,fs_type,is_derived,formula,description,비고",
                "REVENUE,매출액,IS,FALSE,,매출액,",
                "COGS,매출원가,IS,FALSE,,매출원가,",
                "TOTAL_ASSETS,자산총계,BS,FALSE,,자산총계,",
                "CFO,영업활동현금흐름,CF,FALSE,,영업활동현금흐름,",
                "CAPEX_PPE,유형자산의 취득,CF,FALSE,,유형자산의 취득,",
            ]
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
