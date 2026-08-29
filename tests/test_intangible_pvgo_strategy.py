from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

from api.repository.factor_lab_query import validate_factor_lab_graph
from scripts.build_us_intangible_adjusted_pvgo import (
    FACTOR_NODE_IDS,
    FINAL_WEIGHTS,
    MIN_MARKET_CAP_USD_MILLIONS,
    MODEL_NAME,
    START_DATE,
    _ablation_graph,
    build_graph,
)
from scripts.build_us_clean_raw_pvgo import (
    MODEL_NAME as CLEAN_RAW_MODEL_NAME,
    build_graph as build_clean_raw_graph,
)
from scripts.build_us_intangible_pvgo_qvir_balanced_hybrid import (
    ADJUSTED_PVGO_SOURCE_NAME,
    QVIR_SOURCE_NAME,
    START_DATE as HYBRID_START_DATE,
    build_graph as build_hybrid_graph,
)
from scripts.factor_lab_research_diagnostics import (
    _parse_french_daily_text,
    factor_model_regression,
    newey_west_mean_test,
)
from scripts.analyze_us_pvgo_level_cross_section import _fama_macbeth
from scripts.run_us_pvgo_clean_control_experiment import (
    _paired_diagnostic,
    build_frozen_graphs,
    build_matched_sample_graphs,
)


KNOWN_FACTOR_IDS = {
    "normalized_intangible_adjusted_pvgo_pct",
    "intangible_adjusted_roe_spread_pct",
    "intangible_adjusted_pvgo_compression_pct",
    "normalized_intangible_adjusted_eps",
    "mcap_mil",
    "equity_pvgo_pct",
    "roe_cost_of_equity_spread_pct",
    "equity_pvgo_compression_pct",
    "normalized_earnings_5y",
}


def _nodes(graph):
    return {node.id: node for node in graph.nodes}


class IntangiblePvgoStrategyTest(unittest.TestCase):
    def test_french_factor_parser_uses_decimal_returns_and_rejects_sentinels(self):
        result = _parse_french_daily_text(
            "header\n"
            "20200102, 1.00, -0.50, 0.25\n"
            "20200103, 99.99, 0.00, 0.00\n",
            value_columns=["MKT_RF", "SMB", "RF"],
        )

        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result.iloc[0]["MKT_RF"], 0.01)
        self.assertAlmostEqual(result.iloc[0]["SMB"], -0.005)
        self.assertAlmostEqual(result.iloc[0]["RF"], 0.0025)

    def test_ff5_momentum_regression_recovers_synthetic_alpha_and_betas(self):
        rng = np.random.default_rng(20260829)
        index = pd.bdate_range("2020-01-02", periods=800)
        factor_values = rng.normal(0.0, 0.008, size=(len(index), 6))
        beta = np.array([1.05, 0.30, -0.15, 0.20, -0.10, 0.40])
        alpha = 0.0002
        risk_free = np.full(len(index), 0.0001)
        noise = rng.normal(0.0, 0.001, size=len(index))
        factors = pd.DataFrame(
            factor_values,
            index=index,
            columns=["MKT_RF", "SMB", "HML", "RMW", "CMA", "MOM"],
        )
        factors["RF"] = risk_free
        portfolio = pd.Series(
            risk_free + alpha + factor_values @ beta + noise,
            index=index,
        )

        result = factor_model_regression(portfolio, factors, max_lags=5)

        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(
            result["estimates"]["alpha"]["coefficient"],
            alpha,
            delta=0.0001,
        )
        for name, expected in zip(
            ["MKT_RF", "SMB", "HML", "RMW", "CMA", "MOM"],
            beta,
            strict=True,
        ):
            self.assertAlmostEqual(
                result["estimates"][name]["coefficient"],
                expected,
                delta=0.02,
            )

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
        self.assertEqual(graph.experiment.start_date, START_DATE)
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
            nodes["market_cap_input"].config["financial_basis"],
            "annual",
        )
        self.assertEqual(
            nodes["normalized_adjusted_eps_input"].config["factor_id"],
            "normalized_intangible_adjusted_eps",
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

    def test_clean_raw_is_a_frozen_topology_accounting_control(self):
        adjusted = build_graph().model_dump(mode="json")
        raw = build_clean_raw_graph().model_dump(mode="json")
        adjusted_nodes = {node["id"]: node for node in adjusted["nodes"]}
        raw_nodes = {node["id"]: node for node in raw["nodes"]}

        self.assertEqual(raw["experiment"]["name"], CLEAN_RAW_MODEL_NAME)
        self.assertEqual(
            {key: value for key, value in raw["experiment"].items() if key != "name"},
            {key: value for key, value in adjusted["experiment"].items() if key != "name"},
        )
        self.assertEqual(set(raw_nodes), set(adjusted_nodes))
        self.assertEqual(raw["edges"], adjusted["edges"])
        self.assertEqual(
            raw_nodes["expectation_level_input"]["config"]["factor_id"],
            "equity_pvgo_pct",
        )
        self.assertEqual(
            raw_nodes["quality_input"]["config"]["factor_id"],
            "roe_cost_of_equity_spread_pct",
        )
        self.assertEqual(
            raw_nodes["expectation_change_input"]["config"]["factor_id"],
            "equity_pvgo_compression_pct",
        )
        self.assertEqual(
            raw_nodes["normalized_adjusted_eps_input"]["config"]["factor_id"],
            "normalized_earnings_5y",
        )
        validation = validate_factor_lab_graph(
            raw,
            known_factor_ids=KNOWN_FACTOR_IDS,
        )
        self.assertTrue(validation.valid, validation.errors)

    def test_frozen_control_suite_contains_all_four_requested_models(self):
        graphs = build_frozen_graphs()

        self.assertEqual(
            set(graphs),
            {
                "A_legacy_raw",
                "B_clean_raw",
                "C_clean_intangible",
                "D_adjusted_level_only",
            },
        )
        self.assertEqual(
            graphs["B_clean_raw"].experiment.start_date,
            graphs["C_clean_intangible"].experiment.start_date,
        )
        self.assertEqual(
            graphs["D_adjusted_level_only"].outputs.final_node_id,
            "expectation_level_rank_score",
        )

    def test_matched_controls_share_one_complete_case_topology(self):
        graphs = build_matched_sample_graphs()
        self.assertEqual(
            set(graphs),
            {
                "B_clean_raw_matched",
                "C_clean_intangible_matched",
                "D_adjusted_level_only_matched",
            },
        )
        canonical = None
        for graph in graphs.values():
            payload = graph.model_dump(mode="json")
            validation = validate_factor_lab_graph(
                payload,
                known_factor_ids=KNOWN_FACTOR_IDS,
            )
            self.assertTrue(validation.valid, validation.errors)
            score = next(
                node
                for node in payload["nodes"]
                if node["id"] == "intangible_expectations_alpha"
            )
            self.assertEqual(set(score["config"]["weights"]), {
                "expectation_level",
                "quality",
                "expectation_change",
                "raw_expectation_level",
                "raw_quality",
                "raw_expectation_change",
            })
            score["config"]["weights"] = "accounting-treatment-placeholder"
            payload["experiment"]["name"] = "matched-name-placeholder"
            if canonical is None:
                canonical = payload
            else:
                self.assertEqual(payload, canonical)

    def test_paired_diagnostic_uses_aligned_active_returns(self):
        dates = pd.bdate_range("2025-01-02", periods=8)
        left = pd.Series([0.02] * 8, index=dates)
        right = pd.Series([0.01] * 8, index=dates)

        result = _paired_diagnostic(
            left,
            right,
            {"2025-03-31": {"A", "B"}, "2025-06-30": {"B", "C"}},
            {"2025-03-31": {"A", "C"}, "2025-06-30": {"B", "D"}},
        )

        self.assertEqual(result["observations"], 8)
        self.assertAlmostEqual(result["annualized_arithmetic_active_return"], 2.52)
        self.assertAlmostEqual(result["mean_rebalance_jaccard_overlap"], 1 / 3)

    def test_fama_macbeth_joint_regression_recovers_incremental_signs(self):
        rng = np.random.default_rng(20260829)
        rows = []
        for signal_date in pd.date_range("2020-03-31", periods=16, freq="QE"):
            raw = rng.normal(size=120)
            adjusted = 0.5 * raw + rng.normal(scale=0.8, size=120)
            future = -0.02 * raw - 0.03 * adjusted + rng.normal(scale=0.01, size=120)
            for index in range(120):
                rows.append(
                    {
                        "signal_date": signal_date,
                        "raw_pvgo": raw[index],
                        "adjusted_pvgo": adjusted[index],
                        "forward_return": future[index],
                        "market_cap_mil": 1_000 + index * 10,
                        "sector_code": str(10 + (index % 5) * 5),
                    }
                )
        result = _fama_macbeth(
            pd.DataFrame(rows),
            ["raw_pvgo", "adjusted_pvgo"],
            controls=False,
        )

        self.assertEqual(result["periods"], 16)
        self.assertLess(result["coefficients"]["raw_pvgo_z"]["mean_coefficient"], 0)
        self.assertLess(
            result["coefficients"]["adjusted_pvgo_z"]["mean_coefficient"],
            0,
        )

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
        self.assertEqual(graph.experiment.start_date, HYBRID_START_DATE)
        self.assertEqual(
            graph.experiment.snapshot_coverage_policy,
            "allow_missing_inputs",
        )
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
