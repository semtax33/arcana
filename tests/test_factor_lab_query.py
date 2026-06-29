import unittest

from api.repository.factor_lab_query import (
    compile_factor_lab_graph,
    validate_factor_lab_graph,
)


def nested_graph():
    return {
        "version": 1,
        "experiment": {
            "name": "nested",
            "market": "KR",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "universe": {"type": "market", "sector_codes": [], "industry_group_codes": []},
            "rebalance": {"frequency": "quarterly", "signal_lag_days": 1, "transaction_cost_bps": 0},
        },
        "nodes": [
            {"id": "factor_a", "type": "factor_input", "config": {"factor_id": "per", "financial_basis": "annual"}},
            {"id": "factor_b", "type": "factor_input", "config": {"factor_id": "pbr", "financial_basis": "annual"}},
            {"id": "factor_c", "type": "factor_input", "config": {"factor_id": "roe", "financial_basis": "annual"}},
            {"id": "add_ab", "type": "add", "config": {}},
            {"id": "div_final", "type": "div", "config": {}},
        ],
        "edges": [
            {"source": "factor_a", "target": "add_ab", "target_handle": "left"},
            {"source": "factor_b", "target": "add_ab", "target_handle": "right"},
            {"source": "add_ab", "target": "div_final", "target_handle": "left"},
            {"source": "factor_c", "target": "div_final", "target_handle": "right"},
        ],
        "outputs": {"final_node_id": "div_final"},
    }


class FactorLabQueryTest(unittest.TestCase):
    def test_validate_rejects_unknown_factor_and_bad_quantile(self):
        graph = nested_graph()
        graph["nodes"][0]["config"]["factor_id"] = "missing_factor"
        graph["nodes"].append(
            {
                "id": "bad_winsor",
                "type": "winsorize",
                "config": {"lower_quantile": 0.9, "upper_quantile": 0.1},
            }
        )
        graph["edges"].append(
            {"source": "div_final", "target": "bad_winsor", "target_handle": "input"}
        )
        graph["outputs"]["final_node_id"] = "bad_winsor"

        result = validate_factor_lab_graph(graph, known_factor_ids={"per", "pbr", "roe"})

        self.assertFalse(result.valid)
        self.assertIn("unknown_factor_id", {error.code for error in result.errors})
        self.assertIn("invalid_quantile", {error.code for error in result.errors})

    def test_nested_arithmetic_graph_controls_precedence_by_edges(self):
        result = compile_factor_lab_graph(
            nested_graph(),
            known_factor_ids={"per", "pbr", "roe"},
        )

        self.assertEqual(
            result.execution_order,
            ["factor_a", "factor_b", "add_ab", "factor_c", "div_final"],
        )
        self.assertLess(result.query.index("node_add_ab AS"), result.query.index("node_div_final AS"))
        self.assertIn("FROM node_add_ab AS l", result.query)
        self.assertIn("INNER JOIN node_factor_c AS r", result.query)
        self.assertIn("division_by_zero", result.query)

    def test_zscore_and_winsorize_compile_invalid_value_policy(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {
                "id": "winsor_per",
                "type": "winsorize",
                "config": {"group_by": ["trade_date"], "lower_quantile": 0.01, "upper_quantile": 0.99},
            },
            {
                "id": "z_per",
                "type": "zscore",
                "config": {
                    "group_by": ["trade_date", "sector"],
                    "stddev_method": "population",
                    "min_count": 20,
                    "zero_std_policy": "invalid",
                    "direction": "lower_better",
                    "clip": 3.0,
                },
            },
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "winsor_per", "target_handle": "input"},
            {"source": "winsor_per", "target": "z_per", "target_handle": "input"},
        ]
        graph["outputs"] = {"final_node_id": "z_per"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per"})

        self.assertIn("quantileExact({node_winsor_per_lower_quantile:Float64})(value)", result.query)
        self.assertIn("winsor_empty_group", result.query)
        self.assertIn("countIf(s.is_valid) OVER", result.query)
        self.assertIn("zscore_min_count", result.query)
        self.assertIn("zscore_zero_std", result.query)
        self.assertIn("greatest(least(((-1) * ((value - mu) / sigma))", result.query)
        self.assertEqual(result.parameters["node_z_per_clip"], 3.0)

    def test_cycle_is_a_hard_error(self):
        graph = nested_graph()
        graph["edges"].append({"source": "div_final", "target": "add_ab", "target_handle": "left"})

        result = validate_factor_lab_graph(graph, known_factor_ids={"per", "pbr", "roe"})

        self.assertFalse(result.valid)
        self.assertIn("duplicate_handle", {error.code for error in result.errors})


if __name__ == "__main__":
    unittest.main()
