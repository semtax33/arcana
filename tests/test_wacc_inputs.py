import unittest

import pandas as pd

from engine.transformers.factors import add_wacc_factors
from engine.transformers.wacc import (
    calculate_rolling_beta,
    normalize_benchmark_weekly_returns,
    normalize_weekly_returns_from_prices,
)


class WaccInputsTest(unittest.TestCase):
    def test_weekly_returns_use_friday_week_last_adjusted_close(self):
        prices = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"] * 4,
                "trade_date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-09"]),
                "close": [10, 11, 12, 13],
                "adj_close": [20, 22, 24, 26],
            }
        )

        result = normalize_weekly_returns_from_prices(prices)

        self.assertEqual(result["week_end_date"].astype(str).tolist(), ["2026-01-02", "2026-01-09"])
        self.assertEqual(result["weekly_close"].tolist(), [22, 26])
        self.assertAlmostEqual(result["weekly_return"].iat[1], 26 / 22 - 1)

    def test_calculate_rolling_beta_uses_two_year_weekly_returns_and_adjusts(self):
        weeks = pd.date_range("2024-01-05", periods=60, freq="W-FRI")
        benchmark_returns = [0.01 if i % 2 == 0 else -0.005 for i in range(60)]
        stock_returns = [value * 1.5 for value in benchmark_returns]
        stock = pd.DataFrame({"week_end_date": weeks, "weekly_return": stock_returns})
        benchmark = pd.DataFrame({"week_end_date": weeks, "weekly_return": benchmark_returns})

        result = calculate_rolling_beta(stock, benchmark, window=104, min_periods=52)

        self.assertFalse(result.empty)
        self.assertAlmostEqual(result["beta_raw"].iat[-1], 1.5)
        self.assertAlmostEqual(result["beta"].iat[-1], 0.67 * 1.5 + 0.33)

    def test_add_wacc_factors_calculates_cost_components_and_weights(self):
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"],
                "trade_date": pd.to_datetime(["2026-01-02"]),
                "close": [10],
                "market_cap": [800.0],
                "enterprise_value": [1_000.0],
                "avg_ic_operational": [500.0],
                "debt": [200.0],
                "avg_debt": [250.0],
                "xint": [10.0],
                "tax_rate": [25.0],
                "roic_operational": [12.0],
            }
        )
        risk_free = pd.DataFrame(
            {
                "market": ["us"],
                "date": pd.to_datetime(["2026-01-01"]),
                "risk_free_rate": [4.0],
            }
        )
        erp = pd.DataFrame({"country_code": ["US"], "equity_risk_premium": [5.0]})
        assumptions = pd.DataFrame(
            {
                "market": ["us"],
                "country_code": ["US"],
                "risk_free_rate": [4.0],
                "equity_risk_premium": [5.0],
                "credit_spread": [2.0],
                "default_beta": [1.2],
            }
        )

        result = add_wacc_factors(
            daily,
            market="us",
            market_data_cache=_WaccCache(risk_free=risk_free, erp=erp, assumptions=assumptions),
        )

        self.assertAlmostEqual(result["beta"].iat[0], 1.2)
        self.assertAlmostEqual(result["cost_of_equity"].iat[0], 10.0)
        self.assertAlmostEqual(result["cost_of_debt_pre_tax"].iat[0], 4.0)
        self.assertAlmostEqual(result["cost_of_debt_after_tax"].iat[0], 3.0)
        self.assertAlmostEqual(result["wacc_equity_weight"].iat[0], 80.0)
        self.assertAlmostEqual(result["wacc_debt_weight"].iat[0], 20.0)
        self.assertAlmostEqual(result["wacc"].iat[0], 8.6)
        self.assertAlmostEqual(result["economic_profit"].iat[0], (12.0 - 8.6) / 100 * 500.0)
        self.assertAlmostEqual(result["economic_profit_yield"].iat[0], (12.0 - 8.6) * 500.0 / 1_000.0)

    def test_add_wacc_factors_calculates_roic_wacc_spread_growth(self):
        row_count = 253
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_US_AAPL"] * row_count,
                "trade_date": pd.date_range("2025-01-01", periods=row_count, freq="D"),
                "close": [10.0] * row_count,
                "market_cap": [800.0] * row_count,
                "enterprise_value": [1_000.0] * row_count,
                "avg_ic_operational": [500.0] * row_count,
                "debt": [200.0] * row_count,
                "avg_debt": [250.0] * row_count,
                "xint": [10.0] * row_count,
                "tax_rate": [25.0] * row_count,
                "roic_operational": [10.0] + [20.0] * (row_count - 1),
            }
        )
        risk_free = pd.DataFrame(
            {
                "market": ["us"],
                "date": pd.to_datetime(["2025-01-01"]),
                "risk_free_rate": [4.0],
            }
        )
        erp = pd.DataFrame({"country_code": ["US"], "equity_risk_premium": [5.0]})
        assumptions = pd.DataFrame(
            {
                "market": ["us"],
                "country_code": ["US"],
                "risk_free_rate": [4.0],
                "equity_risk_premium": [5.0],
                "credit_spread": [2.0],
                "default_beta": [1.2],
            }
        )

        result = add_wacc_factors(
            daily,
            market="us",
            market_data_cache=_WaccCache(risk_free=risk_free, erp=erp, assumptions=assumptions),
        )

        self.assertAlmostEqual(result["roic_wacc_spread"].iat[0], 1.4)
        self.assertAlmostEqual(result["roic_wacc_spread"].iat[-1], 11.4)
        self.assertAlmostEqual(result["delta_economic_profit"].iat[-1], 50.0)
        self.assertAlmostEqual(
            result["roic_wacc_spread_growth_1y"].iat[-1],
            (11.4 - 1.4) / 1.4 * 100,
        )

    def test_benchmark_weekly_returns_normalizer_supports_sp500(self):
        raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-01-02", "2026-01-09"]),
                "close": [100.0, 105.0],
            }
        )

        result = normalize_benchmark_weekly_returns(raw, market="us", benchmark_id="US_SP500")

        self.assertEqual(result["market"].tolist(), ["us", "us"])
        self.assertEqual(result["benchmark_id"].tolist(), ["US_SP500", "US_SP500"])
        self.assertAlmostEqual(result["weekly_return"].iat[1], 0.05)


class _WaccCache:
    def __init__(self, *, risk_free, erp, assumptions, benchmark=None):
        self._risk_free = risk_free
        self._erp = erp
        self._assumptions = assumptions
        self._benchmark = benchmark if benchmark is not None else pd.DataFrame()

    def risk_free_rates(self):
        return self._risk_free.copy()

    def country_erps(self):
        return self._erp.copy()

    def wacc_assumptions(self):
        return self._assumptions.copy()

    def benchmark_weekly_returns(self):
        return self._benchmark.copy()


if __name__ == "__main__":
    unittest.main()
