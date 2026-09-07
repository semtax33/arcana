from __future__ import annotations

import unittest

import pandas as pd

from api.repository.factor_lab_query import validate_factor_lab_graph
from scripts.build_kr_pvgo_expectations_alpha import (
    EARLY_PERIOD,
    END_DATE,
    FINAL_WEIGHTS,
    LATE_PERIOD,
    MODEL_NAME,
    START_DATE,
    UNAVAILABLE_PERIOD,
    US_SOURCE_MODEL_NAME,
    build_graph,
)
from scripts.build_us_pvgo_expectations_alpha import build_graph as build_us_graph
from scripts.run_kr_pvgo_expectations_alpha import _stitched_diagnostic


KNOWN_FACTOR_IDS = {
    "pvgo_gap_pct",
    "pvgo_pct",
    "roiic_wacc_spread",
    "pvgo_compression_pct",
}


class KrPvgoExpectationsAlphaTest(unittest.TestCase):
    def test_korean_port_has_a_distinct_identity_and_cannot_reuse_us_name(self):
        self.assertNotEqual(MODEL_NAME, US_SOURCE_MODEL_NAME)
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            build_graph(name=US_SOURCE_MODEL_NAME)

    def test_korean_port_preserves_source_score_topology_and_weights(self):
        kr = build_graph().model_dump(mode="json")
        us = build_us_graph().model_dump(mode="json")

        self.assertEqual(kr["nodes"], us["nodes"])
        self.assertEqual(kr["edges"], us["edges"])
        self.assertEqual(kr["outputs"], us["outputs"])
        score = next(node for node in kr["nodes"] if node["id"] == "expectations_alpha")
        self.assertEqual(score["config"]["weights"], FINAL_WEIGHTS)
        self.assertTrue(score["config"]["missing_weight_renormalize"])

    def test_korean_port_is_pit_quarterly_and_excludes_financials(self):
        graph = build_graph()

        self.assertEqual(graph.experiment.market, "KR")
        self.assertEqual(graph.experiment.start_date, START_DATE)
        self.assertEqual(graph.experiment.end_date, END_DATE)
        self.assertEqual(graph.experiment.factor_data_mode, "point_in_time_snapshot")
        self.assertEqual(
            graph.experiment.snapshot_coverage_policy,
            "allow_missing_inputs",
        )
        self.assertEqual(graph.experiment.rebalance.frequency, "quarterly")
        self.assertEqual(graph.experiment.rebalance.signal_lag_days, 1)
        self.assertNotIn("40", graph.experiment.universe.sector_codes)

    def test_korean_port_uses_only_unadjusted_pvgo_inputs(self):
        factor_ids = {
            str(node.config["factor_id"])
            for node in build_graph().nodes
            if node.type == "factor_input"
        }

        self.assertEqual(factor_ids, KNOWN_FACTOR_IDS)
        self.assertFalse(any("intangible" in factor_id for factor_id in factor_ids))

    def test_declared_periods_are_disjoint_and_expose_the_data_gap(self):
        self.assertEqual(EARLY_PERIOD[0], START_DATE)
        self.assertLess(EARLY_PERIOD[1], UNAVAILABLE_PERIOD[0])
        self.assertLess(UNAVAILABLE_PERIOD[1], LATE_PERIOD[0])
        self.assertEqual(LATE_PERIOD[1], END_DATE)

    def test_graph_validates_against_the_factor_contract(self):
        validation = validate_factor_lab_graph(
            build_graph().model_dump(mode="json"),
            known_factor_ids=KNOWN_FACTOR_IDS,
        )

        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(validation.warnings, [])

    def test_stitched_diagnostic_links_real_segments_and_labels_cash_gap(self):
        early = pd.Series(
            [1.0, 1.5],
            index=pd.to_datetime(["2002-04-01", "2012-12-31"]),
        )
        late = pd.Series(
            [1.0, 2.0],
            index=pd.to_datetime(["2016-04-01", "2026-08-24"]),
        )

        result = _stitched_diagnostic(early, late, transaction_cost_bps=20.0)

        self.assertEqual(result["status"], "diagnostic_only")
        self.assertAlmostEqual(result["cumulative_return"], 2.0)
        self.assertEqual(
            result["cash_gap"],
            [str(UNAVAILABLE_PERIOD[0]), str(UNAVAILABLE_PERIOD[1])],
        )
        self.assertEqual(result["cash_gap_return_assumption"], 0.0)


if __name__ == "__main__":
    unittest.main()
