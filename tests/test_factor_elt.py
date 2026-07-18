import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.loaders.factors import (
    FACT_DAILY_FACTOR_COLUMNS,
    _resolve_stock_codes,
    create_daily_factor_rows,
    create_factor_catalog_dataframe,
    insert_daily_factors,
    prepare_daily_factor_rows,
)


class FactorEltTest(unittest.TestCase):
    def test_resolve_kr_stock_codes_uses_local_price_universe(self):
        with TemporaryDirectory() as temp_dir:
            price_path = f"{temp_dir}/kr_normalized_price.csv"
            pd.DataFrame(
                {
                    "security_id": [
                        "SEC_KR_005930",
                        "SEC_KR_000660",
                        "SEC_KR_005930",
                        "SEC_US_AAPL",
                    ]
                }
            ).to_csv(price_path, index=False)

            with patch("engine.loaders.factors.resolve_price_path", return_value=Path(price_path)):
                result = _resolve_stock_codes(None, market="kr")

        self.assertEqual(result, ["000660", "005930"])

    def test_earnings_payout_factors_are_lower_better(self):
        catalog_df = create_factor_catalog_dataframe(
            ["payout_ratio", "earnings_payout_ratio", "tdpr"]
        ).set_index("factor_id")

        self.assertTrue(
            (catalog_df["value_direction"] == "LOWER_BETTER").all()
        )

    def test_prepare_daily_factor_rows_melts_only_factor_columns(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "stock_code": "005930",
                    "fiscal_year": 2025,
                    "financial_period": "2025-12-31",
                    "close": 75000,
                    "volume": 100,
                    "shares": 10,
                    "market_cap": 750000,
                    "trading_value": 7_500_000,
                    "currency": "KRW",
                    "updated_at": "2026-01-03 09:00:00",
                    "roe": 0.12,
                    "per": 11.5,
                    "na_20": None,
                    "ev_nopat_quality_flag": "negative_nopat",
                    "nopat_quality_flag": "reported_operating_income",
                    "operating_income_source": "reported_operating_income",
                }
            ]
        )

        result = prepare_daily_factor_rows(wide_df, financial_basis="ttm")

        self.assertEqual(list(result.columns), FACT_DAILY_FACTOR_COLUMNS)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result["factor_id"]), {"per", "roe"})
        self.assertNotIn("close", set(result["factor_id"]))
        self.assertNotIn("volume", set(result["factor_id"]))
        self.assertNotIn("ev_nopat_quality_flag", set(result["factor_id"]))
        self.assertNotIn("nopat_quality_flag", set(result["factor_id"]))
        self.assertNotIn("operating_income_source", set(result["factor_id"]))
        self.assertTrue((result["financial_basis"] == "ttm").all())
        self.assertEqual(result.loc[result["factor_id"] == "roe", "factor_value"].iat[0], 0.12)

    def test_prepare_daily_factor_rows_returns_empty_when_all_values_null(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "roe": None,
                }
            ]
        )

        result = prepare_daily_factor_rows(wide_df, factor_ids=["roe"])

        self.assertEqual(list(result.columns), FACT_DAILY_FACTOR_COLUMNS)
        self.assertTrue(result.empty)

    def test_prepare_daily_factor_rows_includes_wacc_factors(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_US_AAPL",
                    "trade_date": "2026-01-02",
                    "currency": "USD",
                    "wacc": 8.6,
                    "beta": 1.2,
                    "cost_of_equity": 10.0,
                    "roic_wacc_spread": 11.4,
                    "economic_profit": 57.0,
                    "economic_profit_yield": 5.7,
                    "delta_economic_profit": 10.0,
                    "roic_wacc_spread_growth_1y": 25.0,
                }
            ]
        )

        result = prepare_daily_factor_rows(wide_df)

        self.assertEqual(
            set(result["factor_id"]),
            {
                "wacc",
                "beta",
                "cost_of_equity",
                "roic_wacc_spread",
                "economic_profit",
                "economic_profit_yield",
                "delta_economic_profit",
                "roic_wacc_spread_growth_1y",
            },
        )

    def test_create_daily_factor_rows_reuses_market_data_cache(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "roe": 10.0,
                    "currency": "KRW",
                }
            ]
        )
        cache = object()

        with (
            patch("engine.loaders.factors.FactorMarketDataCache", return_value=cache) as cache_factory,
            patch("engine.loaders.factors._resolve_stock_codes", return_value=["005930", "000660"]),
            patch("engine.loaders.factors.create_stock_factor_dataframe", return_value=wide_df) as create_wide,
        ):
            result = create_daily_factor_rows(
                stock_codes=["005930", "000660"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                reader_mode="cached",
            )

        cache_factory.assert_called_once()
        self.assertEqual(create_wide.call_count, 2)
        self.assertTrue(
            all(call.kwargs["market_data_cache"] is cache for call in create_wide.call_args_list)
        )
        self.assertEqual(len(result), 2)

    def test_create_daily_factor_rows_can_use_csv_reader_mode(self):
        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "roe": 10.0,
                    "currency": "KRW",
                }
            ]
        )

        with (
            patch("engine.loaders.factors.FactorMarketDataCache") as cache_factory,
            patch("engine.loaders.factors._resolve_stock_codes", return_value=["005930"]),
            patch("engine.loaders.factors.create_stock_factor_dataframe", return_value=wide_df) as create_wide,
        ):
            result = create_daily_factor_rows(
                stock_codes=["005930"],
                reader_mode="csv",
            )

        cache_factory.assert_not_called()
        self.assertIsNone(create_wide.call_args.kwargs["market_data_cache"])
        self.assertEqual(len(result), 1)

    def test_insert_daily_factors_fails_fast_when_price_file_is_missing(self):
        with TemporaryDirectory() as temp_dir:
            missing_price_path = f"{temp_dir}/missing_prices.csv"

            with self.assertRaisesRegex(FileNotFoundError, "price data is required"):
                insert_daily_factors(
                    stock_codes=["AAPL"],
                    market="us",
                    price_path=missing_price_path,
                    insert_catalog=False,
                )

    def test_insert_daily_factors_can_prepare_stocks_in_parallel(self):
        class FakeClient:
            def __init__(self):
                self.inserted = []

            def insert_df(self, table_name, dataframe, column_names):
                self.inserted.append((table_name, dataframe.copy(), list(column_names)))

        def make_wide_df(stock_code, **_):
            return pd.DataFrame(
                [
                    {
                        "security_id": f"SEC_KR_{stock_code}",
                        "trade_date": "2026-01-02",
                        "stock_code": stock_code,
                        "fiscal_year": 2025,
                        "financial_period": "2025-12-31",
                        "currency": "KRW",
                        "updated_at": "2026-01-03 09:00:00",
                        "roe": 10.0,
                    }
                ]
            )

        client = FakeClient()
        with (
            patch("engine.loaders.factors._resolve_stock_codes", return_value=["005930", "000660"]),
            patch("engine.loaders.factors.create_stock_factor_dataframe", side_effect=make_wide_df) as create_wide,
        ):
            result = insert_daily_factors(
                stock_codes=["005930", "000660"],
                client=client,
                insert_catalog=False,
                reader_mode="csv",
                insert_batch_size=2,
                progress_interval=0,
                parallel_workers=2,
            )

        self.assertEqual(create_wide.call_count, 2)
        self.assertEqual(result.attrs["inserted_rows"], 2)
        self.assertEqual(result.attrs["factor_count"], 1)
        self.assertEqual(len(client.inserted), 1)
        table_name, inserted_df, column_names = client.inserted[0]
        self.assertEqual(table_name, "fact_daily_factors")
        self.assertEqual(column_names, FACT_DAILY_FACTOR_COLUMNS)
        self.assertEqual(set(inserted_df["security_id"]), {"SEC_KR_005930", "SEC_KR_000660"})

    def test_insert_daily_factors_can_filter_to_wacc_bundle(self):
        class FakeClient:
            def __init__(self):
                self.inserted = []

            def insert_df(self, table_name, dataframe, column_names):
                self.inserted.append((table_name, dataframe.copy(), list(column_names)))

        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "currency": "KRW",
                    "roe": 10.0,
                    "per": 11.0,
                    "wacc": 8.5,
                    "cost_of_equity": 9.4,
                    "cost_of_debt_pre_tax": 5.2,
                    "cost_of_debt_after_tax": 3.9,
                    "wacc_equity_weight": 80.0,
                    "wacc_debt_weight": 20.0,
                    "beta": 1.1,
                    "roic_wacc_spread": 11.5,
                    "economic_profit": 57.5,
                    "economic_profit_yield": 5.75,
                    "delta_economic_profit": 10.0,
                    "roic_wacc_spread_growth_1y": 30.0,
                }
            ]
        )

        client = FakeClient()
        with (
            patch("engine.loaders.factors._resolve_stock_codes", return_value=["005930"]),
            patch("engine.loaders.factors.create_stock_factor_dataframe", return_value=wide_df),
        ):
            result = insert_daily_factors(
                stock_codes=["005930"],
                client=client,
                reader_mode="csv",
                factor_ids="wacc_bundle",
            )

        self.assertEqual(result.attrs["inserted_rows"], 12)
        self.assertEqual(result.attrs["factor_count"], 12)
        self.assertEqual(client.inserted[0][0], "factor_catalog")
        self.assertEqual(
            set(client.inserted[0][1]["factor_id"]),
            {
                "wacc",
                "cost_of_equity",
                "cost_of_debt_pre_tax",
                "cost_of_debt_after_tax",
                "wacc_equity_weight",
                "wacc_debt_weight",
                "beta",
                "roic_wacc_spread",
                "economic_profit",
                "economic_profit_yield",
                "delta_economic_profit",
                "roic_wacc_spread_growth_1y",
            },
        )
        inserted_df = client.inserted[1][1]
        self.assertEqual(
            set(inserted_df["factor_id"]),
            {
                "wacc",
                "cost_of_equity",
                "cost_of_debt_pre_tax",
                "cost_of_debt_after_tax",
                "wacc_equity_weight",
                "wacc_debt_weight",
                "beta",
                "roic_wacc_spread",
                "economic_profit",
                "economic_profit_yield",
                "delta_economic_profit",
                "roic_wacc_spread_growth_1y",
            },
        )
        self.assertNotIn("roe", set(inserted_df["factor_id"]))
        self.assertNotIn("per", set(inserted_df["factor_id"]))

    def test_insert_daily_factors_can_filter_to_individual_factor_ids(self):
        class FakeClient:
            def __init__(self):
                self.inserted = []

            def insert_df(self, table_name, dataframe, column_names):
                self.inserted.append((table_name, dataframe.copy(), list(column_names)))

        wide_df = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-01-02",
                    "currency": "KRW",
                    "roe": 10.0,
                    "per": 11.0,
                    "pbr": 1.2,
                    "wacc": 8.5,
                }
            ]
        )

        client = FakeClient()
        with (
            patch("engine.loaders.factors._resolve_stock_codes", return_value=["005930"]),
            patch("engine.loaders.factors.create_stock_factor_dataframe", return_value=wide_df),
        ):
            result = insert_daily_factors(
                stock_codes=["005930"],
                client=client,
                reader_mode="csv",
                factor_ids="roe,per",
            )

        self.assertEqual(result.attrs["inserted_rows"], 2)
        self.assertEqual(result.attrs["factor_count"], 2)
        self.assertEqual(set(client.inserted[0][1]["factor_id"]), {"roe", "per"})
        inserted_df = client.inserted[1][1]
        self.assertEqual(set(inserted_df["factor_id"]), {"roe", "per"})
        self.assertNotIn("pbr", set(inserted_df["factor_id"]))
        self.assertNotIn("wacc", set(inserted_df["factor_id"]))

    def test_insert_daily_factors_rejects_unknown_factor_id(self):
        with self.assertRaisesRegex(ValueError, "unknown factor id"):
            insert_daily_factors(
                stock_codes=["005930"],
                reader_mode="csv",
                factor_ids="not_a_factor",
                dry_run=True,
            )

    def test_create_factor_catalog_dataframe_marks_technical_factors(self):
        catalog_df = create_factor_catalog_dataframe(
            [
                "roe",
                "roic_financial",
                "roic_operational",
                "na_20",
                "ma_50",
                "ma_120",
                "ma_150",
                "ma_200",
                "rsi_14",
                "macd",
                "bb_upper",
                "ati",
                "williams_r_14",
                "cmf_20",
                "mfi_14",
            ]
        )

        row_by_id = catalog_df.set_index("factor_id")
        self.assertEqual(row_by_id.loc["roe", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roe", "unit"], "percent")
        self.assertEqual(row_by_id.loc["roic_financial", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roic_financial", "unit"], "percent")
        self.assertEqual(row_by_id.loc["roic_operational", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roic_operational", "unit"], "percent")
        self.assertEqual(row_by_id.loc["na_20", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["na_20", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_50", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["ma_50", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_50", "unit"], "krw")
        self.assertEqual(row_by_id.loc["ma_120", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_150", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["ma_200", "factor_group"], "trend")
        self.assertEqual(row_by_id.loc["rsi_14", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["rsi_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["rsi_14", "unit"], "percent")
        self.assertEqual(row_by_id.loc["rsi_14", "value_direction"], "NEUTRAL")
        self.assertEqual(row_by_id.loc["macd", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["macd", "unit"], "krw")
        self.assertEqual(row_by_id.loc["bb_upper", "factor_group"], "volatility")
        self.assertEqual(row_by_id.loc["bb_upper", "unit"], "krw")
        self.assertEqual(row_by_id.loc["ati", "factor_type"], "technical")
        self.assertEqual(row_by_id.loc["ati", "factor_group"], "volume")
        self.assertEqual(row_by_id.loc["ati", "unit"], "shares")
        self.assertEqual(row_by_id.loc["williams_r_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["williams_r_14", "unit"], "percent")
        self.assertEqual(row_by_id.loc["cmf_20", "factor_group"], "volume")
        self.assertEqual(row_by_id.loc["cmf_20", "unit"], "ratio")
        self.assertEqual(row_by_id.loc["mfi_14", "factor_group"], "momentum")
        self.assertEqual(row_by_id.loc["mfi_14", "unit"], "percent")

    def test_create_factor_catalog_dataframe_registers_requested_factors(self):
        factor_ids = [
            "rnd_margin",
            "fcf_margin",
            "fcf_payout_ratio",
            "fcf_dividend_coverage",
            "fcf_after_dividends",
            "fcf_yield",
            "fcf_yield_dividend_yield_spread",
            "fcf_negative_freq_5y_pct",
            "capex_to_sales_pct",
            "net_debt_to_ebitda",
            "net_debt_to_fcf",
            "fcf_interest_coverage",
            "eps_dividend_coverage",
            "dps_cagr_5y",
            "dividend_consistency_streak",
            "dividend_cut",
            "shareholder_yield",
            "sales_cagr_3y",
            "rnd_to_sales",
            "operating_profit_margin",
            "net_income_growth_1y",
            "net_income_growth_3y",
            "net_income_growth_5y",
            "operating_income_growth_1y",
            "operating_income_growth_3y",
            "operating_income_growth_5y",
            "sales_growth_1y",
            "sales_growth_3y",
            "sales_growth_5y",
            "roe_growth_1y",
            "roe_growth_3y",
            "roe_growth_5y",
            "roic_operational_growth_1y",
            "operating_margin_growth_1y",
            "fcf_margin_growth_1y",
            "roic_wacc_spread",
            "economic_profit",
            "economic_profit_yield",
            "delta_economic_profit",
            "roic_wacc_spread_growth_1y",
            "net_margin",
            "total_asset_turnover",
            "accrual_ratio",
            "rnd_to_market_cap",
            "wacc",
            "cost_of_equity",
            "cost_of_debt_pre_tax",
            "cost_of_debt_after_tax",
            "wacc_equity_weight",
            "wacc_debt_weight",
            "beta",
        ]

        catalog_df = create_factor_catalog_dataframe(factor_ids)

        row_by_id = catalog_df.set_index("factor_id")
        self.assertEqual(row_by_id.loc["rnd_margin", "factor_name"], "R&D Margin")
        self.assertEqual(row_by_id.loc["rnd_margin", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["rnd_margin", "unit"], "percent")
        self.assertEqual(row_by_id.loc["fcf_margin", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["fcf_payout_ratio", "factor_type"], "shareholder")
        self.assertEqual(row_by_id.loc["fcf_payout_ratio", "unit"], "percent")
        self.assertEqual(row_by_id.loc["fcf_payout_ratio", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["fcf_dividend_coverage", "unit"], "times")
        self.assertEqual(row_by_id.loc["fcf_after_dividends", "unit"], "krw")
        self.assertEqual(row_by_id.loc["fcf_yield", "factor_type"], "valuation")
        self.assertEqual(row_by_id.loc["fcf_yield", "unit"], "percent")
        self.assertEqual(row_by_id.loc["fcf_negative_freq_5y_pct", "factor_type"], "risk")
        self.assertEqual(row_by_id.loc["capex_to_sales_pct", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["net_debt_to_ebitda", "factor_name"], "Net Debt / EBITDA")
        self.assertEqual(row_by_id.loc["net_debt_to_ebitda", "unit"], "times")
        self.assertEqual(row_by_id.loc["net_debt_to_ebitda", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["net_debt_to_fcf", "unit"], "times")
        self.assertEqual(row_by_id.loc["fcf_interest_coverage", "unit"], "times")
        self.assertEqual(row_by_id.loc["eps_dividend_coverage", "factor_type"], "shareholder")
        self.assertEqual(row_by_id.loc["dps_cagr_5y", "unit"], "percent")
        self.assertEqual(row_by_id.loc["dividend_consistency_streak", "unit"], "years")
        self.assertEqual(row_by_id.loc["dividend_cut", "unit"], "flag")
        self.assertEqual(row_by_id.loc["shareholder_yield", "unit"], "percent")
        self.assertEqual(row_by_id.loc["sales_cagr_3y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["sales_cagr_3y", "unit"], "percent")
        self.assertEqual(row_by_id.loc["net_income_growth_5y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["operating_income_growth_3y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["sales_growth_5y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["roe_growth_5y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["roe_growth_5y", "unit"], "percent")
        self.assertEqual(row_by_id.loc["roic_operational_growth_1y", "factor_name"], "ROIC Growth 1Y")
        self.assertEqual(row_by_id.loc["roic_operational_growth_1y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["operating_margin_growth_1y", "unit"], "percent")
        self.assertEqual(row_by_id.loc["fcf_margin_growth_1y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["roic_wacc_spread", "factor_name"], "ROIC - WACC Spread")
        self.assertEqual(row_by_id.loc["roic_wacc_spread", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["roic_wacc_spread", "unit"], "percent")
        self.assertEqual(row_by_id.loc["economic_profit", "factor_name"], "Economic Profit")
        self.assertEqual(row_by_id.loc["economic_profit", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["economic_profit", "unit"], "krw")
        self.assertEqual(row_by_id.loc["economic_profit_yield", "factor_name"], "Economic Profit Yield")
        self.assertEqual(row_by_id.loc["economic_profit_yield", "factor_type"], "valuation")
        self.assertEqual(row_by_id.loc["economic_profit_yield", "unit"], "percent")
        self.assertEqual(row_by_id.loc["delta_economic_profit", "factor_name"], "Delta Economic Profit")
        self.assertEqual(row_by_id.loc["delta_economic_profit", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["delta_economic_profit", "unit"], "krw")
        self.assertEqual(row_by_id.loc["roic_wacc_spread_growth_1y", "factor_type"], "growth")
        self.assertEqual(row_by_id.loc["total_asset_turnover", "unit"], "times")
        self.assertEqual(row_by_id.loc["accrual_ratio", "factor_name"], "Accrual Ratio")
        self.assertEqual(row_by_id.loc["accrual_ratio", "factor_type"], "quality")
        self.assertEqual(row_by_id.loc["accrual_ratio", "unit"], "ratio")
        self.assertEqual(row_by_id.loc["accrual_ratio", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["rnd_to_market_cap", "factor_type"], "valuation")
        self.assertEqual(row_by_id.loc["rnd_to_market_cap", "unit"], "percent")
        self.assertEqual(row_by_id.loc["wacc", "factor_type"], "risk")
        self.assertEqual(row_by_id.loc["wacc", "unit"], "percent")
        self.assertEqual(row_by_id.loc["wacc", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["cost_of_equity", "unit"], "percent")
        self.assertEqual(row_by_id.loc["cost_of_debt_after_tax", "value_direction"], "LOWER_BETTER")
        self.assertEqual(row_by_id.loc["wacc_equity_weight", "value_direction"], "NEUTRAL")
        self.assertEqual(row_by_id.loc["beta", "unit"], "ratio")
        self.assertEqual(row_by_id.loc["beta", "value_direction"], "NEUTRAL")
        higher_better_factor_ids = [
            factor_id
            for factor_id in factor_ids
            if factor_id
            not in {
                "fcf_payout_ratio",
                "fcf_negative_freq_5y_pct",
                "capex_to_sales_pct",
                "net_debt_to_ebitda",
                "net_debt_to_fcf",
                "dividend_cut",
                "wacc",
                "cost_of_equity",
                "cost_of_debt_pre_tax",
                "cost_of_debt_after_tax",
                "wacc_equity_weight",
                "wacc_debt_weight",
                "beta",
                "accrual_ratio",
            }
        ]
        self.assertTrue(
            (row_by_id.loc[higher_better_factor_ids, "value_direction"] == "HIGHER_BETTER").all()
        )

    def test_create_factor_catalog_dataframe_registers_consensus_factors(self):
        factor_ids = [
            "eps_expected_growth",
            "revenue_expected_growth",
            "operating_income_expected_growth",
            "net_income_expected_growth",
            "eps_surprise_pct",
            "revenue_surprise_pct",
            "operating_income_surprise_pct",
            "net_income_surprise_pct",
        ]

        catalog_df = create_factor_catalog_dataframe(factor_ids)
        row_by_id = catalog_df.set_index("factor_id")

        self.assertEqual(set(row_by_id.index), set(factor_ids))
        self.assertTrue((row_by_id["factor_type"] == "growth").all())
        self.assertTrue((row_by_id["unit"] == "percent").all())
        self.assertTrue((row_by_id["value_direction"] == "HIGHER_BETTER").all())


if __name__ == "__main__":
    unittest.main()
