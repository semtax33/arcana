from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from engine.loaders.factors import (
    create_factor_catalog_dataframe,
    prepare_daily_factor_rows,
)
from engine.transformers.consensus import build_hankyung_daily_consensus
from engine.transformers.factors import (
    add_equity_valuation_factors,
    add_price_momentum_factors,
    add_rim_historical_roe_fallback,
    calculate_k_ratio,
    merge_forward_consensus_inputs,
)


class AdvancedFactorMetricsTest(unittest.TestCase):
    def test_k_ratio_requires_504_observations_and_matches_reference_ols(self):
        count = 504
        x = np.arange(count, dtype="float64")
        log_vami = 0.001 * x + 0.01 * np.sin(x / 17)
        prices = pd.Series(np.exp(log_vami))

        result = calculate_k_ratio(prices, window=756, min_periods=504)

        self.assertTrue(result.iloc[:503].isna().all())
        slope, intercept = np.polyfit(x, log_vami, 1)
        residual = log_vami - (intercept + slope * x)
        sxx = np.square(x - x.mean()).sum()
        slope_standard_error = math.sqrt(
            (np.square(residual).sum() / (count - 2)) / sxx
        )
        expected = slope / (count * slope_standard_error)
        self.assertAlmostEqual(result.iloc[-1], expected, places=8)

    def test_k_ratio_uses_adjusted_close_when_available(self):
        count = 504
        x = np.arange(count, dtype="float64")
        adjusted_close = np.exp(0.001 * x + 0.01 * np.sin(x / 13))
        close = adjusted_close.copy()
        close[250:] = close[250:] / 2
        daily = pd.DataFrame(
            {
                "trade_date": pd.date_range("2024-01-01", periods=count, freq="D"),
                "close": close,
                "adj_close": adjusted_close,
                "high": close,
                "low": close,
                "volume": 100,
                "shares": 1_000,
            }
        )

        result = add_price_momentum_factors(daily)
        expected = calculate_k_ratio(pd.Series(adjusted_close))

        self.assertAlmostEqual(result["k_ratio_3y"].iloc[-1], expected.iloc[-1], places=10)

    def test_forward_consensus_uses_nearest_future_period_without_lookahead(self):
        daily = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    ["2026-06-01", "2026-07-01", "2027-01-02", "2027-07-01"]
                )
            }
        )
        consensus = pd.DataFrame(
            [
                {
                    "as_of_date": "2026-05-30",
                    "target_period": "2026.12",
                    "metric_id": "forward_per",
                    "consensus_mean": 10,
                },
                {
                    "as_of_date": "2026-05-30",
                    "target_period": "2027.12",
                    "metric_id": "forward_per",
                    "consensus_mean": 20,
                },
                {
                    "as_of_date": "2026-12-15",
                    "target_period": "2027.12",
                    "metric_id": "forward_per",
                    "consensus_mean": 21,
                },
                {
                    "as_of_date": "2026-05-30",
                    "target_period": "2026.12",
                    "metric_id": "forward_roe",
                    "consensus_mean": 12,
                },
                {
                    "as_of_date": "2026-12-15",
                    "target_period": "2027.12",
                    "metric_id": "forward_roe",
                    "consensus_mean": 13,
                },
            ]
        )

        result = merge_forward_consensus_inputs(daily, consensus, stale_days=180)

        self.assertEqual(result["forward_per"].iloc[:2].tolist(), [10, 10])
        self.assertEqual(result["forward_roe"].iloc[:2].tolist(), [12, 12])
        self.assertEqual(result["forward_per"].iloc[2], 21)
        self.assertEqual(result["forward_roe"].iloc[2], 13)
        self.assertTrue(pd.isna(result["forward_per"].iloc[3]))
        self.assertTrue(pd.isna(result["forward_roe"].iloc[3]))

    def test_forward_consensus_averages_latest_estimate_per_broker(self):
        estimates = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "stock_code": "005930",
                    "target_period": "2026.12",
                    "metric_id": "forward_per",
                    "as_of_date": "2026-06-01",
                    "estimate_value": 8.0,
                    "broker_code": "A",
                    "broker_name": "Broker A",
                    "analyst_name": "Analyst A",
                    "report_idx": 1,
                },
                {
                    "security_id": "SEC_KR_005930",
                    "stock_code": "005930",
                    "target_period": "2026.12",
                    "metric_id": "forward_per",
                    "as_of_date": "2026-06-01",
                    "estimate_value": 12.0,
                    "broker_code": "B",
                    "broker_name": "Broker B",
                    "analyst_name": "Analyst B",
                    "report_idx": 2,
                },
            ]
        )

        result = build_hankyung_daily_consensus(estimates, stale_days=180)

        self.assertEqual(len(result), 1)
        self.assertEqual(result["consensus_mean"].iat[0], 10.0)
        self.assertEqual(result["broker_count"].iat[0], 2)

    def test_equity_duration_and_rim_match_reference_values(self):
        daily = pd.DataFrame(
            {
                "forward_per": [10.0],
                "forward_roe": [15.0],
                "cost_of_equity": [10.0],
                "bps": [100.0],
                "close": [80.0],
            }
        )

        result = add_equity_valuation_factors(daily, rim_decay_factor=0.8)

        required_return = 0.10
        growth = required_return - 1 / 10
        eps_first_year = 1 / 10
        present_values = [
            eps_first_year * (1 + growth) ** (year - 1) / (1 + required_return) ** year
            for year in range(1, 20)
        ]
        terminal_present_value = 1 - sum(present_values)
        expected_duration = (
            sum(year * value for year, value in enumerate(present_values, start=1))
            + 20 * terminal_present_value
        ) / (1 + required_return)
        expected_target = 100 + 100 * (0.15 - 0.10) * 0.8 / (1 - 0.8 + 0.10)

        self.assertAlmostEqual(result["equity_duration_20y"].iat[0], expected_duration)
        self.assertGreater(result["equity_duration_20y"].iat[0], 0)
        self.assertLessEqual(result["equity_duration_20y"].iat[0], 20)
        self.assertAlmostEqual(expected_target, 113.33333333333333)
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[0],
            expected_target / 80 - 1,
        )

    def test_rim_uses_three_year_historical_roe_when_forward_roe_is_missing(self):
        daily = pd.DataFrame(
            {
                "forward_per": [10.0],
                "forward_roe": [math.nan],
                "historical_roe_3y_avg": [15.0],
                "cost_of_equity": [10.0],
                "bps": [100.0],
                "close": [80.0],
            }
        )

        result = add_equity_valuation_factors(daily, rim_decay_factor=0.8)

        expected_target = 100 + 100 * (0.15 - 0.10) * 0.8 / (1 - 0.8 + 0.10)
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[0],
            expected_target / 80 - 1,
        )

    def test_rim_uses_historical_roe_when_forward_roe_is_nonfinite(self):
        daily = pd.DataFrame(
            {
                "forward_per": [10.0],
                "forward_roe": [math.inf],
                "historical_roe_3y_avg": [15.0],
                "cost_of_equity": [10.0],
                "bps": [100.0],
                "close": [80.0],
            }
        )

        result = add_equity_valuation_factors(daily, rim_decay_factor=0.8)

        expected_target = 100 + 100 * (0.15 - 0.10) * 0.8 / (1 - 0.8 + 0.10)
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[0],
            expected_target / 80 - 1,
        )

    def test_rim_prefers_forward_roe_over_historical_fallback(self):
        daily = pd.DataFrame(
            {
                "forward_per": [10.0],
                "forward_roe": [20.0],
                "historical_roe_3y_avg": [15.0],
                "cost_of_equity": [10.0],
                "bps": [100.0],
                "close": [80.0],
            }
        )

        result = add_equity_valuation_factors(daily, rim_decay_factor=0.8)

        expected_target = 100 + 100 * (0.20 - 0.10) * 0.8 / (1 - 0.8 + 0.10)
        self.assertAlmostEqual(
            result["rim_upside_potential"].iat[0],
            expected_target / 80 - 1,
        )

    def test_historical_roe_fallback_is_three_consecutive_years_and_point_in_time(self):
        daily = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    ["2024-03-30", "2024-03-31", "2025-03-31"]
                )
            }
        )
        financials = pd.DataFrame(
            {
                "fiscal_year": [2021, 2022, 2023, 2024],
                "financial_period": pd.to_datetime(
                    ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31"]
                ),
                "report_date": pd.to_datetime(
                    ["2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31"]
                ),
                "roe": [10.0, 20.0, 30.0, 40.0],
            }
        )

        result = add_rim_historical_roe_fallback(daily, financials)

        self.assertTrue(pd.isna(result["historical_roe_3y_avg"].iat[0]))
        self.assertAlmostEqual(result["historical_roe_3y_avg"].iat[1], 20.0)
        self.assertAlmostEqual(result["historical_roe_3y_avg"].iat[2], 30.0)

        nonconsecutive = financials.iloc[[0, 2, 3]].copy()
        missing_year_result = add_rim_historical_roe_fallback(daily, nonconsecutive)
        self.assertTrue(missing_year_result["historical_roe_3y_avg"].isna().all())

    def test_ttm_historical_roe_fallback_uses_three_year_average_point_in_time(self):
        periods = pd.to_datetime(
            [
                "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
                "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
                "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
                "2025-03-31",
            ]
        )
        report_dates = periods + pd.Timedelta(days=30)
        financials = pd.DataFrame(
            {
                "fiscal_year": [2022] * 4 + [2023] * 4 + [2024] * 4 + [2025],
                "fiscal_month": [3, 6, 9, 12] * 3 + [3],
                "financial_period": periods,
                "report_date": report_dates,
                "roe": list(range(10, 23)),
            }
        )
        daily = pd.DataFrame(
            {
                "trade_date": [report_dates[10], report_dates[11], report_dates[12]],
                "forward_per": [10.0, 10.0, 10.0],
                "forward_roe": [math.nan, math.nan, math.nan],
                "cost_of_equity": [10.0, 10.0, 10.0],
                "bps": [100.0, 100.0, 100.0],
                "close": [100.0, 100.0, 100.0],
            }
        )

        result = add_rim_historical_roe_fallback(
            daily,
            financials,
            periods_per_year=4,
        )
        result = add_equity_valuation_factors(result, rim_decay_factor=0.8)

        self.assertTrue(pd.isna(result["historical_roe_3y_avg"].iat[0]))
        self.assertAlmostEqual(result["historical_roe_3y_avg"].iat[1], 15.5)
        self.assertAlmostEqual(result["historical_roe_3y_avg"].iat[2], 16.5)
        expected_target = 100 + 100 * (0.165 - 0.10) * 0.8 / (1 - 0.8 + 0.10)
        self.assertAlmostEqual(result["rim_upside_potential"].iat[2], expected_target / 100 - 1)

        missing_quarter = financials.drop(index=6)
        missing_result = add_rim_historical_roe_fallback(
            daily,
            missing_quarter,
            periods_per_year=4,
        )
        self.assertTrue(missing_result["historical_roe_3y_avg"].isna().all())

    def test_invalid_valuation_inputs_and_decay_are_rejected(self):
        daily = pd.DataFrame(
            {
                "forward_per": [-1.0],
                "forward_roe": [15.0],
                "cost_of_equity": [10.0],
                "bps": [0.0],
                "close": [80.0],
            }
        )

        result = add_equity_valuation_factors(daily)

        self.assertTrue(pd.isna(result["equity_duration_20y"].iat[0]))
        self.assertTrue(pd.isna(result["rim_upside_potential"].iat[0]))
        with self.assertRaisesRegex(ValueError, "0 <= value < 1"):
            add_equity_valuation_factors(daily, rim_decay_factor=1.0)

    def test_factor_catalog_registers_advanced_factor_metadata(self):
        catalog = create_factor_catalog_dataframe(
            [
                "k_ratio_3y",
                "equity_duration_20y",
                "rim_upside_potential",
                "us_price_to_target_price",
                "gross_profitability_pct",
                "asset_yoy_pct",
                "inventory_growth_1y_pct",
                "net_external_financing_pct",
            ]
        ).set_index("factor_id")

        self.assertEqual(catalog.loc["k_ratio_3y", "factor_type"], "technical")
        self.assertEqual(catalog.loc["k_ratio_3y", "factor_group"], "momentum")
        self.assertEqual(catalog.loc["k_ratio_3y", "value_direction"], "HIGHER_BETTER")
        self.assertEqual(catalog.loc["equity_duration_20y", "factor_type"], "risk")
        self.assertEqual(catalog.loc["equity_duration_20y", "factor_group"], "duration")
        self.assertEqual(catalog.loc["equity_duration_20y", "unit"], "years")
        self.assertEqual(catalog.loc["equity_duration_20y", "value_direction"], "LOWER_BETTER")
        self.assertEqual(catalog.loc["rim_upside_potential", "factor_type"], "valuation")
        self.assertEqual(catalog.loc["rim_upside_potential", "unit"], "ratio")
        self.assertEqual(catalog.loc["rim_upside_potential", "value_direction"], "HIGHER_BETTER")
        self.assertEqual(catalog.loc["us_price_to_target_price", "factor_type"], "valuation")
        self.assertEqual(catalog.loc["us_price_to_target_price", "unit"], "ratio")
        self.assertEqual(catalog.loc["us_price_to_target_price", "value_direction"], "HIGHER_BETTER")
        self.assertEqual(catalog.loc["gross_profitability_pct", "factor_type"], "quality")
        self.assertEqual(catalog.loc["gross_profitability_pct", "factor_group"], "profitability")
        self.assertEqual(catalog.loc["gross_profitability_pct", "unit"], "percent")
        self.assertEqual(catalog.loc["gross_profitability_pct", "value_direction"], "HIGHER_BETTER")
        self.assertEqual(catalog.loc["asset_yoy_pct", "factor_group"], "investment")
        self.assertEqual(catalog.loc["asset_yoy_pct", "value_direction"], "LOWER_BETTER")
        self.assertEqual(catalog.loc["inventory_growth_1y_pct", "factor_group"], "investment")
        self.assertEqual(catalog.loc["inventory_growth_1y_pct", "value_direction"], "LOWER_BETTER")
        self.assertEqual(catalog.loc["net_external_financing_pct", "factor_group"], "external_financing")
        self.assertEqual(catalog.loc["net_external_financing_pct", "value_direction"], "LOWER_BETTER")
        self.assertTrue(catalog["description"].str.len().gt(0).all())

    def test_advanced_factors_use_existing_long_format_loader(self):
        factor_ids = [
            "k_ratio_3y",
            "equity_duration_20y",
            "rim_upside_potential",
            "gross_profitability_pct",
            "asset_yoy_pct",
            "inventory_growth_1y_pct",
            "net_external_financing_pct",
        ]
        wide = pd.DataFrame(
            [
                {
                    "security_id": "SEC_KR_005930",
                    "trade_date": "2026-07-24",
                    "currency": "KRW",
                    "k_ratio_3y": 0.025,
                    "equity_duration_20y": 8.5,
                    "rim_upside_potential": 0.3,
                    "gross_profitability_pct": 42.0,
                    "asset_yoy_pct": 5.0,
                    "inventory_growth_1y_pct": -2.0,
                    "net_external_financing_pct": -1.5,
                }
            ]
        )

        result = prepare_daily_factor_rows(
            wide,
            financial_basis="annual",
            factor_ids=factor_ids,
        )

        self.assertEqual(set(result["factor_id"]), set(factor_ids))
        self.assertTrue((result["financial_basis"] == "annual").all())
        self.assertEqual(
            result.set_index("factor_id").loc["rim_upside_potential", "factor_value"],
            0.3,
        )


if __name__ == "__main__":
    unittest.main()
