import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from engine.transformers.factors import add_annual_financial_factors, add_wacc_factors
from engine.transformers.wacc import (
    calculate_rolling_beta,
    equity_risk_premium_series_for_market,
    normalize_benchmark_weekly_returns,
    normalize_market_benchmark_weekly_returns,
    normalize_weekly_returns_from_prices,
)


class WaccInputsTest(unittest.TestCase):
    def test_wacc_preserves_small_disclosed_tax_percentages(self):
        # The financial transformer exposes tax_rate in percent, including
        # rates below 1%. Passing through WACC must not multiply the tax shield.
        financial = add_annual_financial_factors(pd.DataFrame({
            "financial_period": pd.date_range("2018-12-31", periods=4, freq="YE"),
            "PBT": [1000.] * 4, "TAX_EXPENSE": [0., 5., 10., 250.],
        }))
        self.assertEqual(financial.tax_rate.tolist(), [0., .5, 1., 25.])
        for market in ("kr", "us"):
            for observed_interest in (True, False):
                with self.subTest(market=market, observed_interest=observed_interest):
                    daily = pd.DataFrame({
                        "trade_date": pd.date_range("2024-01-02", periods=4),
                        "market_cap": [800.] * 4, "debt": [200.] * 4,
                        "avg_debt": [200.] * 4,
                        "xint": [8. if observed_interest else float("nan")] * 4,
                        "tax_rate": financial.tax_rate,
                    })
                    assumptions = pd.DataFrame({"market": [market],
                        "risk_free_rate": [2.], "equity_risk_premium": [5.],
                        "credit_spread": [2.], "default_beta": [1.]})
                    actual = add_wacc_factors(daily, market=market,
                        market_data_cache=_WaccCache(risk_free=pd.DataFrame(),
                            erp=pd.DataFrame(), assumptions=assumptions))
                    for i, expected in enumerate([4., 3.98, 3.96, 3.]):
                        self.assertAlmostEqual(actual.cost_of_debt_after_tax.iloc[i], expected)
                        self.assertAlmostEqual(actual.wacc.iloc[i], .8 * 7 + .2 * expected)

    def test_country_erp_is_point_in_time_and_rejects_implausible_percent_values(self):
        erp = pd.DataFrame(
            {
                "country_code": ["KR", "KR"],
                "source_date": ["2025-01-01", "2026-01-01"],
                "equity_risk_premium": [6.0, 65.75527310879217],
            }
        )
        assumptions = pd.DataFrame(
            {
                "market": ["kr"],
                "country_code": ["KR"],
                "risk_free_rate": [3.0],
                "equity_risk_premium": [5.0],
                "credit_spread": [2.0],
                "default_beta": [1.0],
            }
        )
        dates = pd.Series(
            pd.to_datetime(["2005-01-03", "2025-06-01", "2026-06-01"])
        )

        result = equity_risk_premium_series_for_market(
            erp,
            "kr",
            dates.index,
            dates,
            assumptions,
        )

        self.assertEqual(result.tolist(), [5.0, 6.0, 6.0])

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

    def test_missing_debt_does_not_impute_an_all_equity_capital_structure(self):
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_KR_005930"],
                "trade_date": pd.to_datetime(["2005-01-03"]),
                "close": [10.0],
                "market_cap": [1_000.0],
            }
        )
        risk_free = pd.DataFrame(
            {
                "market": ["kr"],
                "date": pd.to_datetime(["2005-01-01"]),
                "risk_free_rate": [4.0],
            }
        )
        erp = pd.DataFrame(
            {"country_code": ["KR"], "equity_risk_premium": [5.0]}
        )
        assumptions = pd.DataFrame(
            {
                "market": ["kr"],
                "country_code": ["KR"],
                "risk_free_rate": [4.0],
                "equity_risk_premium": [5.0],
                "credit_spread": [2.0],
                "default_beta": [1.0],
            }
        )

        result = add_wacc_factors(
            daily,
            market="kr",
            market_data_cache=_WaccCache(
                risk_free=risk_free,
                erp=erp,
                assumptions=assumptions,
            ),
        )

        self.assertTrue(result[["wacc_equity_weight", "wacc_debt_weight", "wacc"]].isna().all(axis=None))

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

    def test_benchmark_weekly_returns_normalizer_accepts_raw_yfinance_columns(self):
        raw = pd.DataFrame(
            {
                "Date": ["2026-01-02", "2026-01-09"],
                "Adj Close": [100.0, 110.0],
                "Close": [99.0, 100.0],
            }
        )

        result = normalize_benchmark_weekly_returns(raw, market="us", benchmark_id="US_SP500")

        self.assertEqual(result["week_end_date"].astype(str).tolist(), ["2026-01-02", "2026-01-09"])
        self.assertEqual(result["weekly_close"].tolist(), [100.0, 110.0])
        self.assertAlmostEqual(result["weekly_return"].iat[1], 0.10)

    def test_market_benchmark_normalization_preserves_other_markets(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            benchmark_path = root / "kr_benchmarks.csv"
            output_path = root / "benchmark_weekly_returns.csv"
            pd.DataFrame(
                {
                    "benchmark_id": ["KOSPI200"] * 3,
                    "trade_date": ["2026-01-02", "2026-01-09", "2026-01-16"],
                    "close": [100.0, 102.0, 101.0],
                }
            ).to_csv(benchmark_path, index=False)
            pd.DataFrame(
                {
                    "market": ["us"],
                    "benchmark_id": ["US_SP500"],
                    "week_end_date": ["2026-01-02"],
                    "weekly_close": [200.0],
                    "weekly_return": [None],
                }
            ).to_csv(output_path, index=False)

            result = normalize_market_benchmark_weekly_returns(
                "kr",
                benchmark_path=benchmark_path,
                output_path=output_path,
            )
            saved = pd.read_csv(output_path)

        self.assertEqual(set(result["market"]), {"kr", "us"})
        self.assertEqual(set(saved["market"]), {"kr", "us"})
        kr_rows = result.loc[result["market"] == "kr"]
        self.assertEqual(set(kr_rows["benchmark_id"]), {"KOSPI200"})
        self.assertAlmostEqual(kr_rows["weekly_return"].iloc[1], 0.02)

    def test_kr_wacc_prefers_kospi200_and_uses_rolling_beta(self):
        weeks = pd.date_range("2024-01-05", periods=60, freq="W-FRI")
        benchmark_returns = [None] + [
            0.01 if index % 2 == 0 else -0.005 for index in range(1, 60)
        ]
        stock_closes = [100.0]
        for weekly_return in benchmark_returns[1:]:
            stock_closes.append(stock_closes[-1] * (1 + 1.5 * weekly_return))
        daily = pd.DataFrame(
            {
                "security_id": ["SEC_KR_005930"] * len(weeks),
                "trade_date": weeks,
                "close": stock_closes,
            }
        )
        benchmark = pd.concat(
            [
                pd.DataFrame(
                    {
                        "market": "kr",
                        "benchmark_id": "KOSPI200",
                        "week_end_date": weeks,
                        "weekly_return": benchmark_returns,
                    }
                ),
                pd.DataFrame(
                    {
                        "market": "kr",
                        "benchmark_id": "KOSPI",
                        "week_end_date": weeks,
                        "weekly_return": [None] + [0.004] * 59,
                    }
                ),
            ],
            ignore_index=True,
        )
        risk_free = pd.DataFrame(
            {
                "market": ["kr"],
                "date": [weeks[0]],
                "risk_free_rate": [3.0],
            }
        )
        erp = pd.DataFrame({"country_code": ["KR"], "equity_risk_premium": [6.0]})
        assumptions = pd.DataFrame(
            {
                "market": ["kr"],
                "country_code": ["KR"],
                "risk_free_rate": [3.0],
                "equity_risk_premium": [6.0],
                "credit_spread": [2.0],
                "default_beta": [1.0],
            }
        )

        result = add_wacc_factors(
            daily,
            market="kr",
            market_data_cache=_WaccCache(
                risk_free=risk_free,
                erp=erp,
                assumptions=assumptions,
                benchmark=benchmark,
            ),
        )

        expected_beta = 0.67 * 1.5 + 0.33
        self.assertAlmostEqual(result["beta"].iat[-1], expected_beta, places=8)
        self.assertNotEqual(result["beta"].iat[-1], 1.0)
        self.assertAlmostEqual(
            result["cost_of_equity"].iat[-1],
            3.0 + expected_beta * 6.0,
            places=8,
        )


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
