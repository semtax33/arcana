import unittest

from engine.transformers.factors import (
    market_applicable_factor_columns,
    preferred_factor_columns,
)


class FactorMarketApplicabilityTest(unittest.TestCase):
    def test_kr_contract_excludes_only_explicit_us_factors(self):
        global_contract = preferred_factor_columns()
        kr_contract = market_applicable_factor_columns("kr")
        explicit_us_only = {
            "us_eps_consensus",
            "us_revenue_consensus",
            "us_eps_revision_7d_pct",
            "us_eps_revision_30d_pct",
            "us_eps_revision_60d_pct",
            "us_eps_revision_90d_pct",
            "us_eps_revision_breadth_30d_pct",
            "us_eps_revision_acceleration_30d_pct",
            "us_eps_dispersion_pct",
            "us_revenue_dispersion_pct",
            "us_eps_surprise_pct",
            "us_price_to_target_price",
            "eps_implied_operating_income_surprise_pct",
            "us_consensus_analyst_count",
        }

        self.assertEqual(len(global_contract), 293)
        self.assertEqual(len(kr_contract), 279)
        self.assertEqual(set(global_contract) - set(kr_contract), explicit_us_only)
        self.assertEqual(len(kr_contract), len(set(kr_contract)))

    def test_unknown_market_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported market"):
            market_applicable_factor_columns("moon")


if __name__ == "__main__":
    unittest.main()
