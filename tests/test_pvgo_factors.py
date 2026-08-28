import unittest

import pandas as pd

from engine.loaders._internal.clickhouse_factors import (
    create_factor_catalog_dataframe,
)
from engine.transformers.factors import (
    add_annual_financial_factors,
    add_pvgo_factors,
    perpetual_intangible_capital,
    preferred_factor_columns,
)


PVGO_FACTOR_IDS = {
    "normalized_operating_margin_5y",
    "normalized_nopat_5y",
    "normalized_earnings_5y",
    "normalized_nopat_growth_3y_pct",
    "incremental_investment_rate_pct",
    "roiic_pct",
    "roiic_wacc_spread",
    "pvgo_pct",
    "pvgo_ev_pct",
    "pvgo_expectation_factor",
    "normalized_pvgo_pct",
    "equity_pvgo_pct",
    "justified_pvgo_pct",
    "pvgo_gap_pct",
    "pvgo_compression_pct",
    "pvgo_change_1y_pctp",
    "knowledge_capital",
    "organization_capital",
    "intangible_capital",
    "intangible_investment",
    "intangible_amortization",
    "net_intangible_investment",
    "intangible_adjusted_net_income",
    "normalized_intangible_adjusted_earnings_5y",
    "intangible_adjusted_eps",
    "normalized_intangible_adjusted_eps",
    "intangible_adjusted_equity",
    "avg_intangible_adjusted_equity",
    "intangible_adjusted_roe_pct",
    "intangible_adjusted_roe_spread_pct",
    "intangible_adjusted_pvgo_pct",
    "normalized_intangible_adjusted_pvgo_pct",
    "intangible_adjusted_pvgo_gap_pct",
    "intangible_adjusted_pvgo_compression_pct",
    "intangible_adjusted_pvgo_change_1y_pctp",
}


class PvgoFactorTest(unittest.TestCase):
    def test_perpetual_inventory_initialization_and_amortization(self):
        capital, amortization = perpetual_intangible_capital(
            pd.Series([25.0, 25.0]),
            annual_depreciation_rate=0.15,
            initial_growth_rate=0.10,
        )

        self.assertAlmostEqual(capital.iloc[0], 110.0)
        self.assertAlmostEqual(amortization.iloc[0], 15.0)
        self.assertAlmostEqual(capital.iloc[1], 118.5)
        self.assertAlmostEqual(amortization.iloc[1], 16.5)

    def test_intangible_adjusted_earnings_eps_and_roe(self):
        rows = []
        for year in range(2018, 2024):
            rows.append(
                {
                    "TOTAL_ASSETS": 2_000.0,
                    "TOTAL_EQUITY": 1_000.0,
                    "PPE": 500.0,
                    "CURRENT_ASSETS": 500.0,
                    "CURRENT_LIABILITIES": 250.0,
                    "INVENTORIES": 50.0,
                    "TRADE_RECEIVABLES": 100.0,
                    "TRADE_PAYABLES": 75.0,
                    "CASH_AND_EQUIVALENTS": 100.0,
                    "REVENUE": 1_000.0,
                    "OPERATING_INCOME": 200.0,
                    "NET_INCOME": 160.0,
                    "NET_INCOME_PARENT": 160.0,
                    "PBT": 200.0,
                    "TAX_EXPENSE": 40.0,
                    "RND": 100.0,
                    "SGNA": 50.0,
                    "DILUTED_SHARES": 100.0,
                    "fiscal_year": year,
                }
            )

        result = add_annual_financial_factors(pd.DataFrame(rows))
        first = result.iloc[0]
        latest = result.iloc[-1]

        # After tax: R&D investment=80, organization investment=12.
        # Initial amortization: 48 + 8; adjusted NI=160+92-56=196.
        self.assertAlmostEqual(first["knowledge_capital"], 352.0)
        self.assertAlmostEqual(first["organization_capital"], 44.0)
        self.assertAlmostEqual(first["intangible_amortization"], 56.0)
        self.assertAlmostEqual(first["intangible_adjusted_net_income"], 196.0)
        self.assertAlmostEqual(first["intangible_adjusted_eps"], 1.96)
        self.assertAlmostEqual(
            latest["iroe"],
            latest["intangible_adjusted_roe_pct"],
        )
        self.assertTrue(
            pd.notna(latest["normalized_intangible_adjusted_earnings_5y"])
        )

    def test_financial_inputs_apply_pqci_mapping_at_statement_frequency(self):
        rows = []
        for index in range(24):
            revenue = 1_000.0 + index * 25.0
            operating_income = revenue * 0.20
            rows.append(
                {
                    "TOTAL_ASSETS": 2_000.0 + index * 25.0,
                    "TOTAL_EQUITY": 1_000.0 + index * 12.5,
                    "PPE": 100.0 + index * 12.5,
                    "CURRENT_ASSETS": 500.0,
                    "CURRENT_LIABILITIES": 250.0,
                    "INVENTORIES": 0.0,
                    "TRADE_RECEIVABLES": 0.0,
                    "TRADE_PAYABLES": 0.0,
                    "CASH_AND_EQUIVALENTS": 0.0,
                    "REVENUE": revenue,
                    "OPERATING_INCOME": operating_income,
                    "NET_INCOME": operating_income * 0.80,
                    "NET_INCOME_PARENT": operating_income * 0.80,
                    "PBT": operating_income,
                    "TAX_EXPENSE": operating_income * 0.20,
                }
            )

        result = add_annual_financial_factors(
            pd.DataFrame(rows),
            periods_per_year=4,
        )
        latest = result.iloc[-1]

        self.assertAlmostEqual(latest["normalized_operating_margin_5y"], 20.0)
        self.assertAlmostEqual(latest["normalized_nopat_5y"], latest["sale"] * 0.16)
        self.assertAlmostEqual(latest["incremental_investment_rate_pct"], 50.0)
        self.assertAlmostEqual(latest["roiic_pct"], 32.0)

    def test_market_and_justified_pvgo_formulas(self):
        result = add_pvgo_factors(
            pd.DataFrame(
                {
                    "market_cap": [1_000.0],
                    "enterprise_value": [1_200.0],
                    "net_debt": [200.0],
                    "nopat": [80.0],
                    "normalized_nopat_5y": [100.0],
                    "normalized_earnings_5y": [80.0],
                    "sale": [1_000.0],
                    "wacc": [10.0],
                    "cost_of_equity": [10.0],
                    "roiic_pct": [20.0],
                    "normalized_nopat_growth_3y_pct": [10.0],
                    "incremental_investment_rate_pct": [50.0],
                }
            )
        ).iloc[0]

        expected_justified = sum(
            ((100.0 * (1.1 ** (year - 1)) * 0.10) / 0.10
             - (1_000.0 * (1.1 ** (year - 1)) * 0.10) * 0.50)
            / (1.1**year)
            for year in range(1, 11)
        )
        self.assertAlmostEqual(result["pvgo_pct"], 40.0)
        self.assertAlmostEqual(result["pvgo_ev_pct"], 100 / 3)
        self.assertAlmostEqual(result["pvgo_expectation_factor"], -40.0)
        self.assertAlmostEqual(result["normalized_pvgo_pct"], 20.0)
        self.assertAlmostEqual(result["equity_pvgo_pct"], 20.0)
        self.assertAlmostEqual(result["justified_pvgo_pct"], expected_justified / 10)
        self.assertAlmostEqual(
            result["pvgo_gap_pct"],
            expected_justified / 10 - 40.0,
        )
        self.assertAlmostEqual(result["roiic_wacc_spread"], 10.0)

    def test_intangible_adjusted_pvgo_uses_adjusted_earning_power(self):
        result = add_pvgo_factors(
            pd.DataFrame(
                {
                    "market_cap": [1_000.0],
                    "enterprise_value": [1_200.0],
                    "net_debt": [200.0],
                    "nopat": [80.0],
                    "normalized_nopat_5y": [100.0],
                    "normalized_earnings_5y": [70.0],
                    "intangible_adjusted_net_income": [60.0],
                    "normalized_intangible_adjusted_earnings_5y": [80.0],
                    "intangible_adjusted_roe_pct": [15.0],
                    "sale": [1_000.0],
                    "wacc": [10.0],
                    "cost_of_equity": [10.0],
                    "roiic_pct": [20.0],
                    "normalized_nopat_growth_3y_pct": [10.0],
                    "incremental_investment_rate_pct": [50.0],
                }
            )
        ).iloc[0]

        self.assertAlmostEqual(result["intangible_adjusted_pvgo_pct"], 40.0)
        self.assertAlmostEqual(
            result["normalized_intangible_adjusted_pvgo_pct"],
            20.0,
        )
        self.assertAlmostEqual(result["intangible_adjusted_roe_spread_pct"], 5.0)
        self.assertAlmostEqual(
            result["intangible_adjusted_pvgo_gap_pct"],
            result["justified_pvgo_pct"] - 20.0,
        )

    def test_justified_pvgo_requires_roiic_above_wacc(self):
        result = add_pvgo_factors(
            pd.DataFrame(
                {
                    "market_cap": [1_000.0],
                    "enterprise_value": [1_200.0],
                    "nopat": [80.0],
                    "normalized_nopat_5y": [100.0],
                    "sale": [1_000.0],
                    "wacc": [10.0],
                    "roiic_pct": [8.0],
                    "normalized_nopat_growth_3y_pct": [10.0],
                    "incremental_investment_rate_pct": [50.0],
                }
            )
        )

        self.assertTrue(pd.isna(result.loc[0, "justified_pvgo_pct"]))
        self.assertTrue(pd.isna(result.loc[0, "pvgo_gap_pct"]))

    def test_pvgo_factors_are_registered_in_catalog(self):
        self.assertTrue(PVGO_FACTOR_IDS.issubset(set(preferred_factor_columns())))
        catalog = create_factor_catalog_dataframe(sorted(PVGO_FACTOR_IDS)).set_index(
            "factor_id"
        )
        self.assertEqual(catalog.loc["pvgo_pct", "value_direction"], "LOWER_BETTER")
        self.assertEqual(catalog.loc["pvgo_gap_pct", "value_direction"], "HIGHER_BETTER")
        self.assertEqual(
            catalog.loc["pvgo_compression_pct", "value_direction"],
            "HIGHER_BETTER",
        )
        self.assertEqual(
            catalog.loc["intangible_adjusted_pvgo_pct", "value_direction"],
            "LOWER_BETTER",
        )
        self.assertEqual(
            catalog.loc["normalized_intangible_adjusted_pvgo_pct", "value_direction"],
            "LOWER_BETTER",
        )
        self.assertEqual(
            catalog.loc["intangible_adjusted_pvgo_gap_pct", "value_direction"],
            "HIGHER_BETTER",
        )


if __name__ == "__main__":
    unittest.main()
