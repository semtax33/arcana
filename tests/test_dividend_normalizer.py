import unittest
import contextlib
import io
import json
import os
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.loaders import dividends as dividend_loader
from engine.transformers import dividends as dividend_normalizer


class DividendNormalizerTest(unittest.TestCase):
    def test_default_dividend_edgartools_provider_sets_identity(self):
        identities = []

        class FakeCompany:
            def __init__(self, company_arg):
                if not identities:
                    raise AssertionError("edgartools identity must be set before Company")
                self.company_arg = company_arg

            def facts(self):
                return pd.DataFrame(
                    [
                        {
                            "tag": "DividendsPayableAmountPerShare",
                            "value": 0.7,
                        }
                    ]
                )

        fake_edgar = types.SimpleNamespace(
            Company=FakeCompany,
            set_identity=lambda identity: identities.append(identity),
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"edgar": fake_edgar}),
        ):
            rows = dividend_normalizer._default_us_dividend_edgartools_provider(
                "ZYME",
                "1937653",
                "Zymeworks Inc.",
                [],
            )

        self.assertEqual(identities, ["Arcana contact@example.com"])
        self.assertEqual(rows[0]["tag"], "DividendsPayableAmountPerShare")

    def test_default_dividend_edgartools_provider_suppresses_empty_companyfacts_noise(self):
        class FakeCompany:
            def __init__(self, company_arg):
                print("No company facts found on url https://data.sec.gov/api/xbrl/companyfacts/CIK0002065741.json")
                raise RuntimeError("No company facts found on url https://data.sec.gov/api/xbrl/companyfacts/CIK0002065741.json")

        fake_edgar = types.SimpleNamespace(
            Company=FakeCompany,
            set_identity=lambda identity: None,
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"edgar": fake_edgar}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            rows = dividend_normalizer._default_us_dividend_edgartools_provider(
                "TEST",
                "2065741",
                "Test Inc.",
                [],
            )

        self.assertEqual(rows, [])
        self.assertEqual(stdout.getvalue(), "")

    def test_normalize_dividend_amount_preserves_decimal_places(self):
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("1,000 원"), 1000)
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("45.21 원"), 45.21)
        self.assertEqual(dividend_normalizer.normalize_dividend_amount("-"), 0)

    def test_calculate_payout_ratio_allows_values_above_one(self):
        original_total = dividend_normalizer.calculate_silver_total_dividend_amount
        original_income = dividend_normalizer.calculate_net_income
        try:
            dividend_normalizer.calculate_silver_total_dividend_amount = lambda stock_code, year: 25
            dividend_normalizer.calculate_net_income = lambda stock_code, year, month: 100
            self.assertEqual(dividend_normalizer.calculate_payout_ratio("999999", 2025), 0.25)

            dividend_normalizer.calculate_silver_total_dividend_amount = lambda stock_code, year: 125
            self.assertEqual(dividend_normalizer.calculate_payout_ratio("999999", 2025), 1.25)

            dividend_normalizer.calculate_silver_total_dividend_amount = lambda stock_code, year: 25
            dividend_normalizer.calculate_net_income = lambda stock_code, year, month: -100
            self.assertIsNone(dividend_normalizer.calculate_payout_ratio("999999", 2025))
        finally:
            dividend_normalizer.calculate_silver_total_dividend_amount = original_total
            dividend_normalizer.calculate_net_income = original_income

    def test_calculate_net_income_reads_consolidated_statement_file(self):
        original_base_dir = dividend_normalizer.base_dir
        with TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "kr_normalized_005930.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,canonical_account_name,original_account_name,statement_type,period,normalized_amount,fiscal_year,fiscal_month,fiscal_quarter",
                        "NET_INCOME,Net Income,Net Income,IS,2025.12,1234,2025,12,4",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                dividend_normalizer.base_dir = base_dir
                result = dividend_normalizer.calculate_net_income("005930", 2025, 12)
            finally:
                dividend_normalizer.base_dir = original_base_dir

        self.assertEqual(result, 1234)

    def test_silver_dividend_calculations_use_latest_common_report(self):
        original_by_kind_path = dividend_normalizer.silver_dividend_by_stock_kind_path
        original_summary_path = dividend_normalizer.silver_dividend_company_summary_path

        with TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            by_kind_path = temp_dir / "dividend_by_stock_kind.csv"
            summary_path = temp_dir / "dividend_company_summary.csv"

            by_kind_path.write_text(
                "\n".join(
                    [
                        "stock_code,bsns_year,reprt_code,report_name,rcept_no,stlm_dt,stock_knd,per_share_cash_dividend_krw,source_file",
                        "005930,2024,11012,half,20240801000001,2024-06-30,보통주,70,half.json",
                        "005930,2024,11011,annual,20250301000001,2024-12-31,우선주,151,annual.json",
                        "005930,2024,11011,annual,20250301000001,2024-12-31,보통주,150,annual.json",
                    ]
                ),
                encoding="utf-8",
            )
            summary_path.write_text(
                "\n".join(
                    [
                        "stock_code,bsns_year,reprt_code,report_name,rcept_no,stlm_dt,dividend_payment_amount_krw,dividend_payout_ratio_pct,source_file",
                        "005930,2024,11012,half,20240801000001,2024-06-30,1000000,10,half.json",
                        "005930,2024,11011,annual,20250301000001,2024-12-31,3000000,37.5,annual.json",
                    ]
                ),
                encoding="utf-8",
            )

            try:
                dividend_normalizer.silver_dividend_by_stock_kind_path = by_kind_path
                dividend_normalizer.silver_dividend_company_summary_path = summary_path
                dividend_normalizer.clear_silver_dividend_cache()

                self.assertEqual(
                    dividend_normalizer.calculate_silver_total_dividend_per_share(
                        "005930",
                        2024,
                    ),
                    150,
                )
                self.assertEqual(
                    dividend_normalizer.calculate_silver_total_dividend_per_share_with_fallback(
                        "005930",
                        2025,
                    ),
                    150,
                )
                self.assertEqual(
                    dividend_normalizer.calculate_silver_total_dividend_amount("005930", 2024),
                    3_000_000,
                )
                self.assertEqual(
                    dividend_normalizer.calculate_silver_payout_ratio("005930", 2024),
                    0.375,
                )
            finally:
                dividend_normalizer.silver_dividend_by_stock_kind_path = original_by_kind_path
                dividend_normalizer.silver_dividend_company_summary_path = original_summary_path
                dividend_normalizer.clear_silver_dividend_cache()

    def test_silver_dividend_events_use_source_file_date_when_rcept_no_missing(self):
        original_by_kind_path = dividend_normalizer.silver_dividend_by_stock_kind_path
        original_summary_path = dividend_normalizer.silver_dividend_company_summary_path

        with TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            by_kind_path = temp_dir / "dividend_by_stock_kind.csv"
            summary_path = temp_dir / "dividend_company_summary.csv"
            source_file = (
                "data-lake/bronze/dart/dividend/005930/"
                "finance_statement_dividend_2025-03-15.json"
            )

            by_kind_path.write_text(
                "\n".join(
                    [
                        "stock_code,bsns_year,reprt_code,report_name,rcept_no,stlm_dt,stock_knd,per_share_cash_dividend_krw,source_file",
                        f"005930,2024,decision,decision_annual_sum,,2024-12-31,common,1000,{source_file}",
                    ]
                ),
                encoding="utf-8",
            )
            summary_path.write_text(
                "\n".join(
                    [
                        "stock_code,bsns_year,reprt_code,report_name,rcept_no,stlm_dt,dividend_payment_amount_krw,dividend_payout_ratio_pct,source_file",
                        f"005930,2024,decision,decision_annual_sum,,2024-12-31,3000000,25,{source_file}",
                    ]
                ),
                encoding="utf-8",
            )

            try:
                dividend_normalizer.silver_dividend_by_stock_kind_path = by_kind_path
                dividend_normalizer.silver_dividend_company_summary_path = summary_path
                dividend_normalizer.clear_silver_dividend_cache()

                result = dividend_normalizer.silver_dividend_asof_events("005930")
            finally:
                dividend_normalizer.silver_dividend_by_stock_kind_path = original_by_kind_path
                dividend_normalizer.silver_dividend_company_summary_path = original_summary_path
                dividend_normalizer.clear_silver_dividend_cache()

        self.assertEqual(len(result), 1)
        self.assertEqual(result["report_date"].dt.strftime("%Y-%m-%d").iat[0], "2025-03-15")
        self.assertEqual(result["annual_dividend_per_share"].iat[0], 1000)
        self.assertEqual(result["payout_ratio"].iat[0], 0.25)
        self.assertEqual(result["total_dividend_amount"].iat[0], 3_000_000)

    def test_build_silver_dividend_summary_from_bronze_json(self):
        with TemporaryDirectory() as temp_dir:
            bronze_root = Path(temp_dir) / "bronze"
            old_dir = bronze_root / "005930"
            api_dir = bronze_root / "000020" / "2024"
            old_dir.mkdir(parents=True)
            api_dir.mkdir(parents=True)

            (old_dir / "finance_statement_dividend_2025-01-31.json").write_text(
                json.dumps(
                    {
                        "배당구분": "결산배당",
                        "1주당배당금": {"보통주식": "1,000 원", "종류주식": "1,001 원"},
                        "배당금총액": "10,000,000 원",
                        "배당기준일": "2024-12-31",
                        "배당공시일": "2025-01-31",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (api_dir / "11011_annual.json").write_text(
                json.dumps(
                    {
                        "status": "000",
                        "list": [
                            {
                                "corp_code": "00119195",
                                "corp_name": "동화약품",
                                "stock_code": "000020",
                                "bsns_year": "2024",
                                "reprt_code": "11011",
                                "rcept_no": "20250301000001",
                                "stlm_dt": "2024-12-31",
                                "stock_knd": "보통주",
                                "se": "주당 현금배당금(원)",
                                "thstrm": "180",
                            },
                            {
                                "corp_code": "00119195",
                                "corp_name": "동화약품",
                                "stock_code": "000020",
                                "bsns_year": "2024",
                                "reprt_code": "11011",
                                "rcept_no": "20250301000001",
                                "stlm_dt": "2024-12-31",
                                "stock_knd": "보통주",
                                "se": "현금배당수익률(%)",
                                "thstrm": "1.2",
                            },
                            {
                                "corp_code": "00119195",
                                "corp_name": "동화약품",
                                "stock_code": "000020",
                                "bsns_year": "2024",
                                "reprt_code": "11011",
                                "rcept_no": "20250301000001",
                                "stlm_dt": "2024-12-31",
                                "stock_knd": "",
                                "se": "현금배당금총액(백만원)",
                                "thstrm": "4989",
                            },
                            {
                                "corp_code": "00119195",
                                "corp_name": "동화약품",
                                "stock_code": "000020",
                                "bsns_year": "2024",
                                "reprt_code": "11011",
                                "rcept_no": "20250301000001",
                                "stlm_dt": "2024-12-31",
                                "stock_knd": "",
                                "se": "현금배당성향(%)",
                                "thstrm": "33.3",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            by_kind_df, company_df, failed_df = (
                dividend_normalizer.build_silver_dividend_summary_dataframes(bronze_root)
            )

        self.assertTrue(failed_df.empty)
        self.assertEqual(
            by_kind_df.loc[
                (by_kind_df["stock_code"] == "005930")
                & (by_kind_df["stock_knd"] == "보통주"),
                "per_share_cash_dividend_krw",
            ].iat[0],
            1000,
        )
        self.assertEqual(
            by_kind_df.loc[
                (by_kind_df["stock_code"] == "000020")
                & (by_kind_df["stock_knd"] == "보통주"),
                "market_dividend_yield_pct",
            ].iat[0],
            1.2,
        )
        self.assertEqual(
            company_df.loc[company_df["stock_code"] == "000020", "dividend_payment_amount_krw"].iat[0],
            4_989_000_000,
        )
        self.assertEqual(
            company_df.loc[company_df["stock_code"] == "000020", "dividend_payout_ratio_pct"].iat[0],
            33.3,
        )

    def test_deduplicate_dividend_records_keeps_latest_disclosure_per_event(self):
        records = [
            {
                "dividend_base_date": "2025-12-31",
                "dividend_type": "결산배당",
                "dividend_disclosure_date": "2026-02-01",
                "source_file": "old.json",
                "dividend_per_share": 100,
            },
            {
                "dividend_base_date": "2025-12-31",
                "dividend_type": "결산배당",
                "dividend_disclosure_date": "2026-02-03",
                "source_file": "new.json",
                "dividend_per_share": 120,
            },
            {
                "dividend_base_date": "2025-06-30",
                "dividend_type": "중간배당",
                "dividend_disclosure_date": "2025-07-15",
                "source_file": "mid.json",
                "dividend_per_share": 50,
            },
        ]

        result = dividend_normalizer.deduplicate_dividend_records(records)

        self.assertEqual(len(result), 2)
        self.assertEqual(sum(row["dividend_per_share"] for row in result), 170)

    def test_build_us_sec_dividend_events_from_notes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_dir = root / "notes" / "2025q1_notes"
            notes_dir.mkdir(parents=True)
            ticker_map = root / "sec_company_tickers.csv"
            financial_dir = root / "financials"
            financial_dir.mkdir()

            ticker_map.write_text(
                "\n".join(
                    [
                        "cik,ticker,title",
                        "4127,SWKS,Skyworks Solutions Inc.",
                    ]
                ),
                encoding="utf-8",
            )
            (financial_dir / "us_normalized_SWKS.csv").write_text(
                "\n".join(
                    [
                        "canonical_account_id,normalized_amount,fiscal_year,fiscal_month",
                        "BASIC_EPS,1.5,2025,12",
                        "DILUTED_EPS,2.0,2025,12",
                        "DIV_PAID,70,2025,12",
                        "NET_INCOME,1000,2025,12",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "sub.tsv").write_text(
                "\n".join(
                    [
                        "adsh\tcik\tname\tform\tfiled",
                        "0000004127-25-000010\t4127\tSKYWORKS SOLUTIONS INC\t10-Q\t20250205",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "pre.tsv").write_text(
                "\n".join(
                    [
                        "adsh\treport\tline\tstmt\tinpth\ttag\tversion\tprole\tplabel\tnegating",
                        "0000004127-25-000010\t46\t8\t\t0\tDividendsPayableAmountPerShare\tus-gaap/2024\tterseLabel\tDividend amount\t0",
                        "0000004127-25-000010\t46\t9\t\t0\tDividendsPayableDateDeclaredDayMonthAndYear\tus-gaap/2024\tterseLabel\tDeclared\t0",
                        "0000004127-25-000010\t46\t10\t\t0\tDividendsPayableDateOfRecordDayMonthAndYear\tus-gaap/2024\tterseLabel\tRecord\t0",
                        "0000004127-25-000010\t46\t11\t\t0\tDividendPayableDateToBePaidDayMonthAndYear\tus-gaap/2024\tterseLabel\tPayment\t0",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "num.tsv").write_text(
                "\n".join(
                    [
                        "adsh\ttag\tversion\tddate\tqtrs\tuom\tdimh\tiprx\tvalue",
                        "0000004127-25-000010\tDividendsPayableAmountPerShare\tus-gaap/2024\t20250131\t0\tUSD\t0xabc\t0\t0.7000",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "txt.tsv").write_text(
                "\n".join(
                    [
                        "adsh\ttag\tversion\tddate\tqtrs\tiprx\tlang\tdcml\tdurp\tdatp\tdimh\tdimn\tcoreg\tescaped\tsrclen\ttxtlen\tfootnote\tfootlen\tcontext\tvalue",
                        "0000004127-25-000010\tSecurityExchangeName\tdei/2024\t20250131\t0\t0\ten-US\t32767\t0\t0\t0x00000000\t0\t\t0\t6\t6\t\t0\tc-1\tNASDAQ",
                        "0000004127-25-000010\tTradingSymbol\tdei/2024\t20250131\t0\t0\ten-US\t32767\t0\t0\t0x00000000\t0\t\t0\t4\t4\t\t0\tc-1\tSWKS",
                        "0000004127-25-000010\tDividendsPayableDateDeclaredDayMonthAndYear\tus-gaap/2024\t20250131\t0\t0\ten-US\t32767\t0\t0\t0xabc\t2\t\t0\t10\t10\t\t0\tc-1\t2025-02-05",
                        "0000004127-25-000010\tDividendsPayableDateOfRecordDayMonthAndYear\tus-gaap/2024\t20250228\t0\t0\ten-US\t32767\t0\t0\t0xabc\t2\t\t0\t10\t10\t\t0\tc-2\t2025-02-24",
                        "0000004127-25-000010\tDividendPayableDateToBePaidDayMonthAndYear\tus-gaap/2024\t20250331\t0\t0\ten-US\t32767\t0\t0\t0xabc\t2\t\t0\t10\t10\t\t0\tc-3\t2025-03-17",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "dim.tsv").write_text(
                "dimhash\tsegments\tsegt\n0xabc\tDividends=O2025Q2Dividends;\t0\n",
                encoding="utf-8",
            )

            result = dividend_normalizer.build_us_sec_dividend_events_dataframe(
                notes_root=root / "notes",
                ticker_map_path=ticker_map,
                financial_dir=financial_dir,
                use_edgartools=False,
                use_yfinance_fallback=False,
            )

        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row["ticker"], "SWKS")
        self.assertEqual(str(row["cik"]), "4127")
        self.assertEqual(row["exchange"], "NASDAQ")
        self.assertEqual(row["dividend_declared_date"], "2025-02-05")
        self.assertEqual(row["dividend_record_date"], "2025-02-24")
        self.assertEqual(row["dividend_payment_date"], "2025-03-17")
        self.assertAlmostEqual(row["dividend_amount_per_share"], 0.7)
        self.assertAlmostEqual(row["annual_dps"], 0.7)
        self.assertAlmostEqual(row["annual_eps"], 2.0)
        self.assertAlmostEqual(row["payout_ratio_dps_over_eps"], 0.35)
        self.assertAlmostEqual(row["payout_ratio_total_dividends_over_net_income"], 0.07)

    def test_us_sec_dividend_events_skip_ambiguous_group_values(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            ticker_map = root / "sec_company_tickers.csv"
            ticker_map.write_text("cik,ticker,title\n4127,SWKS,Skyworks\n", encoding="utf-8")
            (notes_dir / "sub.tsv").write_text(
                "adsh\tcik\tname\tform\tfiled\n0000004127-25-000010\t4127\tSKYWORKS\t10-Q\t20250205\n",
                encoding="utf-8",
            )
            (notes_dir / "pre.tsv").write_text(
                "\n".join(
                    [
                        "adsh\treport\tline\tstmt\tinpth\ttag\tversion\tprole\tplabel\tnegating",
                        "0000004127-25-000010\t46\t1\t\t0\tDividendsPayableAmountPerShare\tus-gaap/2024\tterseLabel\tAmount\t0",
                        "0000004127-25-000010\t46\t2\t\t0\tDividendPayableDateToBePaidDayMonthAndYear\tus-gaap/2024\tterseLabel\tPayment\t0",
                    ]
                ),
                encoding="utf-8",
            )
            (notes_dir / "num.tsv").write_text(
                "adsh\ttag\tversion\tddate\tqtrs\tuom\tdimh\tiprx\tvalue\n0000004127-25-000010\tDividendsPayableAmountPerShare\tus-gaap/2024\t20250131\t0\tUSD\t0xabc\t0\t0.70\n",
                encoding="utf-8",
            )
            (notes_dir / "txt.tsv").write_text(
                "\n".join(
                    [
                        "adsh\ttag\tversion\tddate\tqtrs\tiprx\tlang\tdcml\tdurp\tdatp\tdimh\tdimn\tcoreg\tescaped\tsrclen\ttxtlen\tfootnote\tfootlen\tcontext\tvalue",
                        "0000004127-25-000010\tDividendPayableDateToBePaidDayMonthAndYear\tus-gaap/2024\t20250331\t0\t0\ten-US\t32767\t0\t0\t0xabc\t2\t\t0\t10\t10\t\t0\tc-1\t2025-03-17",
                        "0000004127-25-000010\tDividendPayableDateToBePaidDayMonthAndYear\tus-gaap/2024\t20250331\t0\t0\ten-US\t32767\t0\t0\t0xabc\t2\t\t0\t10\t10\t\t0\tc-2\t2025-03-18",
                    ]
                ),
                encoding="utf-8",
            )

            result = dividend_normalizer.build_us_sec_dividend_events_dataframe(
                notes_root=notes_dir,
                ticker_map_path=ticker_map,
                use_edgartools=False,
                use_yfinance_fallback=False,
            )

        self.assertTrue(result.empty)

    def test_edgartools_fallback_does_not_override_sec_notes_event(self):
        sec_rows = pd.DataFrame(
            [
                {
                    "ticker": "SWKS",
                    "cik": "4127",
                    "company_name": "Skyworks",
                    "exchange": "NASDAQ",
                    "dividend_declared_date": "2025-02-05",
                    "dividend_record_date": "2025-02-24",
                    "dividend_payment_date": "2025-03-17",
                    "dividend_amount_per_share": 0.7,
                    "sec_filing_date": "2025-02-05",
                    "source_form": "10-Q",
                    "_source": "sec_notes",
                }
            ]
        )
        edgar_rows = pd.DataFrame(
            [
                {
                    "ticker": "SWKS",
                    "cik": "4127",
                    "company_name": "Skyworks",
                    "exchange": "",
                    "dividend_declared_date": "1900-01-01",
                    "dividend_record_date": "2025-02-24",
                    "dividend_payment_date": "2025-03-17",
                    "dividend_amount_per_share": 0.7,
                    "sec_filing_date": "2025-02-06",
                    "source_form": "8-K",
                    "_source": "edgartools",
                }
            ]
        )

        result = dividend_normalizer._add_us_annual_dividend_metrics(
            dividend_normalizer._dedupe_us_dividend_events(pd.concat([sec_rows, edgar_rows]))
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result["dividend_declared_date"].iat[0], "2025-02-05")
        self.assertEqual(result["source_form"].iat[0], "10-Q")

    def test_yfinance_fallback_fills_tickers_missing_sec_dividend_events(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_dir = root / "notes"
            price_dir = root / "price"
            notes_dir.mkdir()
            price_dir.mkdir()
            ticker_map = root / "sec_company_tickers.csv"
            financial_dir = root / "financials"
            financial_dir.mkdir()

            ticker_map.write_text(
                "\n".join(
                    [
                        "cik,ticker,title",
                        "320193,AAPL,Apple Inc.",
                        "789019,MSFT,Microsoft Corp.",
                    ]
                ),
                encoding="utf-8",
            )
            (price_dir / "AAPL.csv").write_text(
                "\n".join(
                    [
                        "Date,Close,Dividends",
                        "2025-01-20,100,0.25",
                        "2025-02-20,110,0",
                    ]
                ),
                encoding="utf-8",
            )
            (price_dir / "MSFT.csv").write_text(
                "\n".join(
                    [
                        "Date,Close,Dividends",
                        "2025-01-20,100,0.40",
                    ]
                ),
                encoding="utf-8",
            )

            result = dividend_normalizer.build_us_sec_dividend_events_dataframe(
                notes_root=notes_dir,
                ticker_map_path=ticker_map,
                financial_dir=financial_dir,
                symbols=["AAPL"],
                use_edgartools=False,
                yfinance_price_dir=price_dir,
            )

        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row["ticker"], "AAPL")
        self.assertEqual(str(row["cik"]), "320193")
        self.assertEqual(row["dividend_payment_date"], "2025-01-20")
        self.assertAlmostEqual(row["dividend_amount_per_share"], 0.25)
        self.assertEqual(row["source_form"], "YFINANCE")

    def test_yfinance_fallback_does_not_override_sec_dividend_event_ticker(self):
        sec_rows = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "cik": "320193",
                    "company_name": "Apple Inc.",
                    "exchange": "NASDAQ",
                    "dividend_declared_date": "2025-01-01",
                    "dividend_record_date": "2025-01-10",
                    "dividend_payment_date": "2025-01-20",
                    "dividend_amount_per_share": 0.25,
                    "sec_filing_date": "2025-01-02",
                    "source_form": "8-K",
                    "_source": "sec_notes",
                }
            ]
        )

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            price_dir = root / "price"
            price_dir.mkdir()
            ticker_map = root / "sec_company_tickers.csv"
            ticker_map.write_text("cik,ticker,title\n320193,AAPL,Apple Inc.\n", encoding="utf-8")
            (price_dir / "AAPL.csv").write_text(
                "Date,Close,Dividends\n2025-01-20,100,0.99\n",
                encoding="utf-8",
            )

            yfinance_rows = dividend_normalizer._extract_yfinance_dividend_events(
                ticker_map_path=ticker_map,
                price_dir=price_dir,
                exclude_tickers={"AAPL"},
            )
            result = dividend_normalizer._add_us_annual_dividend_metrics(
                dividend_normalizer._dedupe_us_dividend_events(
                    pd.concat([sec_rows, yfinance_rows], ignore_index=True)
                ),
                financial_dir=root / "financials",
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result["source_form"].iat[0], "8-K")
        self.assertAlmostEqual(result["dividend_amount_per_share"].iat[0], 0.25)

    def test_us_daily_dividend_dataframe_reads_sec_event_csv_by_default(self):
        original_path = dividend_normalizer.us_dividend_events_path
        with TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "us_dividend_events.csv"
            pd.DataFrame(
                [
                    {
                        "ticker": "AAPL",
                        "cik": "320193",
                        "company_name": "Apple Inc.",
                        "exchange": "NASDAQ",
                        "dividend_declared_date": "2025-01-01",
                        "dividend_record_date": "2025-01-10",
                        "dividend_payment_date": "2025-01-20",
                        "dividend_amount_per_share": 0.25,
                        "sec_filing_date": "2025-01-02",
                        "source_form": "8-K",
                        "annual_dps": 1.0,
                        "annual_eps": 5.0,
                        "payout_ratio_dps_over_eps": 0.2,
                        "payout_ratio_total_dividends_over_net_income": 0.18,
                    }
                ]
            ).to_csv(event_path, index=False)
            try:
                dividend_normalizer.us_dividend_events_path = event_path
                result = dividend_normalizer.create_us_stock_dividend_dataframe("AAPL")
            finally:
                dividend_normalizer.us_dividend_events_path = original_path

        self.assertEqual(result["security_id"].iat[0], "SEC_US_AAPL")
        self.assertEqual(result["trade_date"].dt.strftime("%Y-%m-%d").iat[0], "2025-01-20")
        self.assertEqual(result["dividend"].iat[0], 0.25)
        self.assertEqual(result["payout_ratio"].iat[0], 0.2)

    def test_us_refresh_writes_sec_events_before_daily_dividends(self):
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2025-01-20"]),
                "dividend": [0.25],
                "payout_ratio": [0.2],
                "dividend_percent": [pd.NA],
                "currency": ["USD"],
                "updated_at": [pd.Timestamp("2026-01-01")],
            }
        )
        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "us_dividend_normalized.csv"
            with (
                patch.object(dividend_loader, "write_us_sec_dividend_events_file", return_value=pd.DataFrame({"ticker": ["AAPL"]})) as events_mock,
                patch.object(dividend_loader, "create_all_stock_dividend_dataframe", return_value=daily) as daily_mock,
                patch.object(dividend_loader, "dividend_output_path", return_value=output_path),
            ):
                result = dividend_loader.refresh_silver_dividend_files(market="us")
            output_exists = output_path.exists()

        events_mock.assert_called_once_with(
            use_edgartools=True,
            use_yfinance_fallback=True,
        )
        daily_mock.assert_called_once_with(market="us", path=None)
        self.assertEqual(len(result), 1)
        self.assertTrue(output_exists)

    def test_kr_refresh_does_not_call_us_sec_dividend_events(self):
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_KR_005930"],
                "trade_date": pd.to_datetime(["2025-01-20"]),
                "dividend": [1000],
                "payout_ratio": [0.25],
                "dividend_percent": [1.2],
                "currency": ["KRW"],
                "updated_at": [pd.Timestamp("2026-01-01")],
            }
        )
        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "dividend_normalized.csv"
            with (
                patch.object(
                    dividend_loader,
                    "write_silver_dividend_summary_files",
                    return_value=(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()),
                ) as kr_summary_mock,
                patch.object(dividend_loader, "write_us_sec_dividend_events_file") as us_events_mock,
                patch.object(dividend_loader, "create_all_stock_dividend_dataframe", return_value=daily) as daily_mock,
                patch.object(dividend_loader, "dividend_output_path", return_value=output_path),
            ):
                result = dividend_loader.refresh_silver_dividend_files(market="kr")

        kr_summary_mock.assert_called_once()
        us_events_mock.assert_not_called()
        daily_mock.assert_called_once_with(market="kr", path=None)
        self.assertEqual(len(result), 1)

    def test_insert_dividends_prepares_clickhouse_stock_dividend_schema(self):
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL", "SEC_US_MSFT"],
                "trade_date": pd.to_datetime(["2025-01-20", None]),
                "dividend": [0.25, 0.4],
                "payout_ratio": [0.2, pd.NA],
                "dividend_percent": [pd.NA, 1.0],
                "currency": ["USD", "USD"],
                "updated_at": [pd.Timestamp("1999-01-01"), pd.Timestamp("1999-01-01")],
            }
        )

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.closed = False

            def insert_df(self, table, frame, column_names):
                self.calls.append((table, frame.copy(), list(column_names)))

            def close(self):
                self.closed = True

        client = FakeClient()
        with patch.object(dividend_loader, "refresh_silver_dividend_files", return_value=daily):
            inserted = dividend_loader.insert_dividends(market="us", client=client)

        self.assertEqual(inserted, 1)
        self.assertEqual(len(client.calls), 1)
        table, frame, columns = client.calls[0]
        self.assertEqual(table, "stock_dividend")
        self.assertEqual(columns, dividend_loader.STOCK_DIVIDEND_COLUMNS)
        self.assertEqual(frame.columns.tolist(), dividend_loader.STOCK_DIVIDEND_COLUMNS)
        self.assertEqual(frame["trade_date"].iat[0], date(2025, 1, 20))
        self.assertEqual(frame["dividend"].iat[0], Decimal("0.250000"))
        self.assertEqual(frame["payout_ratio"].iat[0], 0.2)
        self.assertIsNone(frame["dividend_percent"].iat[0])
        self.assertEqual(frame["currency"].iat[0], "USD")
        self.assertFalse(client.closed)


if __name__ == "__main__":
    unittest.main()
