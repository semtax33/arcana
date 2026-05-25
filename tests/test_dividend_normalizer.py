import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from engine.transformers import dividends as dividend_normalizer


class DividendNormalizerTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
