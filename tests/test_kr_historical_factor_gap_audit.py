import unittest
from unittest.mock import Mock, patch

from scripts.audit_kr_historical_factor_gaps import (
    _baseline_evidence_table,
    _false_positive_cell_counts,
    classify_factor_primary_cause,
)


class KrHistoricalFactorGapAuditTest(unittest.TestCase):
    def test_baseline_evidence_uses_hash_scoped_backup_not_live_table(self):
        report = {
            "backup_table": "fact_daily_factors_kr_hist_backup_deadbeef",
        }
        self.assertEqual(
            _baseline_evidence_table(report),
            "fact_daily_factors_kr_hist_backup_deadbeef",
        )

    def test_baseline_evidence_rejects_unsafe_table_identifier(self):
        with self.assertRaisesRegex(ValueError, "backup table"):
            _baseline_evidence_table({"backup_table": "fact_daily_factors; DROP"})

    @patch("scripts.audit_kr_historical_factor_gaps.get_clickhouse_client")
    def test_false_positive_query_keeps_clickhouse_parameter_placeholder(
        self, get_client
    ):
        client = Mock()
        client.query.return_value.result_rows = [("bps", 3)]
        get_client.return_value = client

        self.assertEqual(
            _false_positive_cell_counts(
                ["bps"], table="fact_daily_factors_kr_hist_backup_deadbeef"
            ),
            {"bps": 3},
        )
        query = client.query.call_args.args[0]
        self.assertIn("IN {factor_ids:Array(String)}", query)
        self.assertIn("LEFT ANTI JOIN", query)

    def test_primary_cause_precedence_is_mutually_exclusive(self):
        common = {
            "materialized_ids": {"roe", "bps"},
            "false_positive_ids": {"bps"},
            "market_inapplicable_ids": {"us_eps_consensus"},
            "source_outside_period_ids": {"dvpsx"},
            "source_sparse_ids": {"real_eps_expected_growth"},
            "financial_dependency_ids": {"roe", "roa"},
        }

        cases = {
            "us_eps_consensus": "NOT_APPLICABLE_MARKET",
            "bps": "MATERIALIZED_WITH_ZERO_IMPUTATION_RISK",
            "roe": "MATERIALIZED_WITH_SOURCE_EVIDENCE",
            "dvpsx": "SOURCE_OUTSIDE_TARGET_PERIOD",
            "real_eps_expected_growth": "UPSTREAM_SOURCE_SPARSE",
            "roa": "FILING_PROVENANCE_OR_CANONICAL_INPUT_MISSING",
            "mystery": "FORMULA_OR_IMPLEMENTATION_GAP",
        }
        for factor_id, expected in cases.items():
            with self.subTest(factor_id=factor_id):
                self.assertEqual(
                    classify_factor_primary_cause(factor_id, **common),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
