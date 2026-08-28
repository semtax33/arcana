from types import SimpleNamespace
import unittest

import pandas as pd

from api.repository.factor_lab_query import validate_factor_lab_graph
from scripts.build_us_intangible_adjusted_pvgo import (
    FACTOR_NODE_IDS,
    FINAL_WEIGHTS,
    MIN_MARKET_CAP_USD_MILLIONS,
    MODEL_NAME,
    _ablation_graph,
    build_graph,
)
from scripts.build_us_intangible_pvgo_qvir_balanced_hybrid import (
    ADJUSTED_PVGO_SOURCE_NAME,
    QVIR_SOURCE_NAME,
    build_graph as build_hybrid_graph,
)
from scripts.factor_lab_research_diagnostics import newey_west_mean_test


KNOWN_FACTOR_IDS = {
    "normalized_intangible_adjusted_pvgo_pct",
    "intangible_adjusted_roe_spread_pct",
    "intangible_adjusted_pvgo_compression_pct",
    "normalized_nopat_5y",
    "mcap_mil",
}


def _nodes(graph):
    return {node.id: node for node in graph.nodes}


class IntangiblePvgoStrategyTest(unittest.TestCase):
    def test_newey_west_inference_is_reported_separately_from_naive_summary(self):
        result = newey_west_mean_test(
            pd.Series([0.01, 0.004, -0.003, 0.006, -0.002, 0.008]),
            max_lags=2,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observations"], 6)
        self.assertEqual(result["max_lags"], 2)
        self.assertGreater(result["hac_standard_error_daily_mean"], 0)
        self.assertIn("does not correct for strategy selection", result["note"])

    def test_expectations_graph_is_strict_unit_consistent_and_complete_case(self):
        graph = build_graph()
        nodes = _nodes(graph)

        self.assertEqual(graph.experiment.snapshot_coverage_policy, "strict")
        self.assertNotIn("40", graph.experiment.universe.sector_codes)
        self.assertNotIn("60", graph.experiment.universe.sector_codes)
        self.assertEqual(
            nodes["intangible_expectations_alpha"].config["weights"],
            FINAL_WEIGHTS,
        )
        self.assertAlmostEqual(sum(FINAL_WEIGHTS.values()), 1.0)
        self.assertFalse(
            nodes["intangible_expectations_alpha"].config[
                "missing_weight_renormalize"
            ]
        )
        self.assertNotIn("quality_sum", nodes)
        self.assertNotIn("gap_input", nodes)
        self.assertEqual(
            nodes["market_cap_floor"].config["value"],
            MIN_MARKET_CAP_USD_MILLIONS,
        )
        self.assertEqual(
            nodes["final_rank_score"].config["semantic_label"],
            "cross_sectional_rank_not_probability",
        )

        validation = validate_factor_lab_graph(
            graph.model_dump(mode="json"),
            known_factor_ids=KNOWN_FACTOR_IDS,
        )
        self.assertTrue(validation.valid, validation.errors)

    def test_every_ablation_uses_the_same_eligibility_gate(self):
        graph = build_graph()
        for sleeve, source_node_id in FACTOR_NODE_IDS.items():
            ablation = _ablation_graph(
                graph,
                sleeve=sleeve,
                source_node_id=source_node_id,
            )
            incoming = {
                (edge.source, edge.target, edge.target_handle)
                for edge in ablation.edges
            }
            self.assertIn(
                ("eligibility_gate", f"{sleeve}_eligible_score", "condition"),
                incoming,
            )
            validation = validate_factor_lab_graph(
                ablation.model_dump(mode="json"),
                known_factor_ids=KNOWN_FACTOR_IDS,
            )
            self.assertTrue(validation.valid, validation.errors)

    def test_hybrid_requires_both_sleeves_and_inherits_operating_universe(self):
        service = _FakeFactorLabService()
        graph, _source_runs = build_hybrid_graph(
            service,
            pvgo_history_run_id="pvgo-history",
            qvir_history_run_id="qvir-history",
        )
        nodes = _nodes(graph)

        self.assertNotIn("40", graph.experiment.universe.sector_codes)
        self.assertNotIn("60", graph.experiment.universe.sector_codes)
        self.assertFalse(
            nodes["balanced_hybrid_score"].config[
                "missing_weight_renormalize"
            ]
        )
        self.assertEqual(
            nodes["final_rank_score"].config["semantic_label"],
            "cross_sectional_rank_not_probability",
        )

    def test_hybrid_can_measure_hard_entry_gate_as_a_separate_diagnostic(self):
        graph, _source_runs = build_hybrid_graph(
            _FakeFactorLabService(),
            pvgo_history_run_id="pvgo-history",
            qvir_history_run_id="qvir-history",
            qvir_output_node="final_score",
            model_name=f"{MODEL_NAME}__GatedDiagnostic",
        )
        source = _nodes(graph)["qvir_source_score"]

        self.assertEqual(source.config["source_output_node"], "final_score")
        self.assertEqual(source.config["hard_gate_policy"], "enforced")


class _FakeFactorLabService:
    def get_experiment_by_name(self, name):
        experiment_ids = {
            ADJUSTED_PVGO_SOURCE_NAME: "pvgo-experiment",
            QVIR_SOURCE_NAME: "qvir-experiment",
        }
        return SimpleNamespace(experiment_id=experiment_ids[name])

    def get_run(self, run_id):
        if run_id == "pvgo-history":
            return SimpleNamespace(
                run_id=run_id,
                status="completed",
                experiment_id="pvgo-experiment",
                factor_id="lab_pvgo_history",
                graph_hash="pvgo-hash",
            )
        if run_id == "qvir-history":
            return SimpleNamespace(
                run_id=run_id,
                status="completed",
                experiment_id=None,
                factor_id="lab_qvir_history",
                graph_hash="qvir-hash",
            )
        raise KeyError(run_id)


if __name__ == "__main__":
    unittest.main()
