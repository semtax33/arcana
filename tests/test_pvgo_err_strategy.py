from __future__ import annotations

import unittest

from api.repository.factor_lab_query import validate_factor_lab_graph
from scripts.build_us_intangible_pvgo_err import (
    DEFAULT_FOUNDATION_WEIGHTS,
    MODEL_NAME,
    ORIGINAL_LEVEL_ONLY_MODEL_NAME,
    ErrGraphSpec,
    build_graph,
)
from scripts.run_us_intangible_pvgo_err_optimization import (
    CANDIDATE_SPECS,
    HOLDOUT,
    TRAIN,
    VALIDATION,
    _selection_score,
)


KNOWN_FACTOR_IDS = {
    "normalized_intangible_adjusted_pvgo_pct",
    "intangible_adjusted_roe_spread_pct",
    "roiic_wacc_spread",
    "roic_wacc_spread",
    "fcf_yield",
    "us_eps_revision_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_surprise_pct",
    "us_price_to_target_price",
    "ret_1m",
    "tr_6_1",
    "normalized_intangible_adjusted_earnings_5y",
    "mcap_mil",
}


def _nodes(graph):
    return {node.id: node for node in graph.nodes}


class PvgoErrStrategyTest(unittest.TestCase):
    def test_new_strategy_cannot_reuse_frozen_name(self):
        self.assertNotEqual(MODEL_NAME, ORIGINAL_LEVEL_ONLY_MODEL_NAME)
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            build_graph(name=ORIGINAL_LEVEL_ONLY_MODEL_NAME)

    def test_err_uses_expected_pit_foundation_revision_and_recognition_inputs(self):
        graph = build_graph()
        factor_nodes = {
            node.config["factor_id"]: node
            for node in graph.nodes
            if node.type == "factor_input"
        }

        self.assertTrue(KNOWN_FACTOR_IDS.issubset(factor_nodes))
        self.assertEqual(graph.experiment.factor_data_mode, "point_in_time_snapshot")
        self.assertEqual(graph.experiment.snapshot_coverage_policy, "strict")
        self.assertEqual(graph.experiment.rebalance.signal_lag_days, 1)
        self.assertNotIn("40", graph.experiment.universe.sector_codes)
        self.assertNotIn("60", graph.experiment.universe.sector_codes)
        self.assertEqual(
            factor_nodes["us_price_to_target_price"].config["missing_policy"],
            "drop",
        )

    def test_target_price_is_lower_better_and_optional_only_inside_recognition(self):
        graph = build_graph()
        nodes = _nodes(graph)
        incoming = {
            (edge.source, edge.target, edge.target_handle) for edge in graph.edges
        }

        self.assertEqual(
            nodes["price_to_target_sector_z"].config["direction"],
            "lower_better",
        )
        self.assertTrue(
            nodes["recognition_score"].config["missing_weight_renormalize"]
        )
        self.assertIn(
            (
                "price_to_target_sector_z",
                "recognition_score",
                "price_to_target",
            ),
            incoming,
        )

    def test_pead_proxy_and_reflexivity_are_explicit_interactions(self):
        graph = build_graph()
        nodes = _nodes(graph)
        incoming = {
            (edge.source, edge.target, edge.target_handle) for edge in graph.edges
        }

        self.assertEqual(nodes["pead_positive_product"].type, "mul")
        self.assertIn(
            ("pead_surprise_positive", "pead_positive_product", "left"),
            incoming,
        )
        self.assertIn(
            ("pead_return_positive", "pead_positive_product", "right"),
            incoming,
        )
        self.assertEqual(nodes["foundation_times_revision"].type, "mul")
        self.assertEqual(nodes["strategy_score"].type, "mul")
        self.assertIn(
            ("recognition_boost_one_plus", "strategy_score", "right"),
            incoming,
        )

    def test_momentum_is_sector_neutralized_before_market_standardization(self):
        graph = build_graph()
        nodes = _nodes(graph)
        incoming = {
            (edge.source, edge.target, edge.target_handle) for edge in graph.edges
        }
        for stem in ("residual_return_1m", "residual_momentum_6m"):
            self.assertEqual(nodes[f"{stem}_sector_neutral"].type, "neutralize")
            self.assertIn(
                (
                    f"{stem}_sector_neutral",
                    f"{stem}_market_z",
                    "input",
                ),
                incoming,
            )

    def test_level_baseline_enforces_the_same_complete_case_inputs(self):
        graph = build_graph(
            name=f"{MODEL_NAME}__test_baseline",
            spec=ErrGraphSpec(
                style="level_baseline",
                foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
            ),
        )
        score = _nodes(graph)["strategy_score"]

        self.assertEqual(score.config["weights"]["expectation_gap"], 1.0)
        self.assertEqual(score.config["weights"]["foundation"], 0.0)
        self.assertEqual(score.config["weights"]["revision"], 0.0)
        self.assertEqual(score.config["weights"]["recognition"], 0.0)
        self.assertFalse(score.config["missing_weight_renormalize"])

    def test_all_predeclared_candidates_validate_without_graph_warnings(self):
        for label, spec in CANDIDATE_SPECS.items():
            with self.subTest(label=label):
                graph = build_graph(name=f"{MODEL_NAME}__test__{label}", spec=spec)
                result = validate_factor_lab_graph(
                    graph.model_dump(mode="json"),
                    known_factor_ids=KNOWN_FACTOR_IDS,
                )
                self.assertTrue(result.valid, result.errors)
                self.assertEqual(result.warnings, [])

    def test_selection_score_is_pre_holdout_and_rewards_regime_robustness(self):
        self.assertLess(TRAIN[1], VALIDATION[0])
        self.assertLess(VALIDATION[1], HOLDOUT[0])
        stable = _selection_score(
            {"metrics": {"sharpe": 1.0, "max_drawdown": -0.20}},
            {"metrics": {"sharpe": 1.0, "max_drawdown": -0.20}},
        )
        unstable = _selection_score(
            {"metrics": {"sharpe": 2.0, "max_drawdown": -0.20}},
            {"metrics": {"sharpe": 0.0, "max_drawdown": -0.20}},
        )
        self.assertGreater(stable, unstable)


if __name__ == "__main__":
    unittest.main()
