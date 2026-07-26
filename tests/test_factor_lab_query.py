import unittest

from api.repository.factor_lab_query import (
    build_factor_lab_insert_query,
    build_invalid_reason_counts_query,
    build_quality_summary_query,
    build_run_ranking_query,
    compile_factor_lab_graph,
    node_type_specs,
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
    def test_single_day_experiment_is_valid_for_screening(self):
        graph = nested_graph()
        graph["experiment"]["start_date"] = "2026-12-30"
        graph["experiment"]["end_date"] = "2026-12-30"

        result = validate_factor_lab_graph(
            graph,
            known_factor_ids={"per", "pbr", "roe"},
        )

        self.assertTrue(result.valid)

    def test_final_quality_queries_use_factor_sort_key(self):
        factor_id = "lab_11111111111111111111111111111111"
        summary_query, summary_params = build_quality_summary_query(
            run_id="11111111-1111-1111-1111-111111111111",
            factor_id=factor_id,
        )
        reason_query, reason_params = build_invalid_reason_counts_query(
            run_id="11111111-1111-1111-1111-111111111111",
            factor_id=factor_id,
        )

        for query, params in [
            (summary_query, summary_params),
            (reason_query, reason_params),
        ]:
            self.assertEqual(params["factor_id"], factor_id)
            self.assertIn("AND factor_id = {factor_id:String}", query)

    def test_node_quality_rejects_final_factor_filter(self):
        with self.assertRaises(ValueError):
            build_quality_summary_query(
                run_id="11111111-1111-1111-1111-111111111111",
                node_id="z_per",
                factor_id="lab_11111111111111111111111111111111",
            )

    def test_run_ranking_uses_effective_date_and_factor_sort_key(self):
        query, params = build_run_ranking_query(
            run_id="11111111-1111-1111-1111-111111111111",
            factor_id="lab_11111111111111111111111111111111",
            effective_trade_date="2026-01-10",
            limit=25,
        )

        self.assertEqual(params["effective_trade_date"], "2026-01-10")
        self.assertEqual(params["factor_id"], "lab_11111111111111111111111111111111")
        self.assertIn("SELECT {effective_trade_date:Date} AS trade_date", query)
        self.assertNotIn("SELECT max(trade_date) AS trade_date", query)
        self.assertIn("AND factor_id = {factor_id:String}", query)
        self.assertEqual(params["limit"], 25)

    def test_run_ranking_falls_back_to_scoped_latest_date(self):
        query, params = build_run_ranking_query(
            run_id="11111111-1111-1111-1111-111111111111",
            factor_id="lab_11111111111111111111111111111111",
        )

        self.assertNotIn("effective_trade_date", params)
        self.assertIn("SELECT max(trade_date) AS trade_date", query)
        self.assertGreaterEqual(query.count("AND factor_id = {factor_id:String}"), 2)

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

    def test_history_compile_can_limit_factor_inputs_to_signal_dates(self):
        result = compile_factor_lab_graph(
            nested_graph(),
            known_factor_ids={"per", "pbr", "roe"},
            trade_dates=["2026-06-30", "2026-03-31"],
        )

        self.assertEqual(result.parameters["trade_dates"], ["2026-03-31", "2026-06-30"])
        self.assertEqual(result.query.count("f.trade_date IN {trade_dates:Array(Date)}"), 3)

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

    def test_shrunk_zscore_blends_small_groups_with_market_score(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {
                "id": "shrunk_per",
                "type": "shrunk_zscore",
                "config": {
                    "group_key": "sector",
                    "min_market_count": 20,
                    "min_group_count": 20,
                    "shrinkage_strength": 20,
                    "direction": "lower_better",
                    "clip": 3.0,
                },
            },
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "shrunk_per", "target_handle": "input"},
        ]
        graph["outputs"] = {"final_node_id": "shrunk_per"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per"})

        self.assertTrue(validate_factor_lab_graph(graph, known_factor_ids={"per"}).valid)
        self.assertIn("node_shrunk_per AS", result.query)
        self.assertIn("market_n", result.query)
        self.assertIn("group_n", result.query)
        self.assertEqual(result.parameters["node_shrunk_per_min_group_count"], 20)

    def test_weighted_score_preserves_output_columns_and_validity_filter(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "factor_pbr", "type": "factor_input", "config": {"factor_id": "pbr"}},
            {
                "id": "composite",
                "type": "weighted_score",
                "config": {"weights": {"value": 0.6, "quality": 0.4}},
            },
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "composite", "target_handle": "value"},
            {"source": "factor_pbr", "target": "composite", "target_handle": "quality"},
        ]
        graph["outputs"] = {"final_node_id": "composite"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per", "pbr"})

        self.assertIn("value.trade_date AS trade_date", result.query)
        self.assertIn("value.security_id AS security_id", result.query)
        self.assertTrue(result.query.endswith("WHERE toUInt8(is_valid) = 1"))
        self.assertEqual(result.parameters["node_composite_value_weight"], 0.6)
        self.assertEqual(result.parameters["node_composite_quality_weight"], 0.4)

    def test_new_node_types_expose_expected_handles(self):
        specs = {spec.type: spec for spec in node_type_specs()}

        self.assertEqual(specs["greater_than"].inputs, ["left", "right"])
        self.assertEqual(specs["condition"].inputs, ["condition", "if_true", "if_false"])
        self.assertEqual(specs["condition_score"].inputs, ["condition", "score"])
        self.assertEqual(specs["lag"].config_schema, {"period": "positive integer"})
        self.assertEqual(specs["rolling_max"].config_schema, {"window": "positive integer"})
        self.assertFalse(specs["weighted_score"].config_schema["missing_weight_renormalize"])

    def test_lab_factor_input_reads_persisted_factor_lab_values(self):
        graph = nested_graph()
        graph["nodes"] = [
            {
                "id": "materialized",
                "type": "factor_input",
                "config": {
                    "factor_id": "lab_11111111111111111111111111111111",
                    "financial_basis": "ttm",
                },
            },
        ]
        graph["edges"] = []
        graph["outputs"] = {"final_node_id": "materialized"}

        result = compile_factor_lab_graph(
            graph,
            known_factor_ids={"lab_11111111111111111111111111111111"},
        )

        self.assertIn("FROM factor_lab_values AS f", result.query)
        self.assertIn("f.is_valid AND f.factor_value IS NOT NULL", result.query)
        self.assertEqual(result.parameters["node_materialized_financial_basis"], "lab")
        self.assertNotIn("FROM fact_daily_factors AS f", result.query)

    def test_logic_condition_and_condition_score_compile_with_exact_handles(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_a", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "factor_b", "type": "factor_input", "config": {"factor_id": "pbr"}},
            {"id": "greater", "type": "greater_than", "config": {}},
            {"id": "choose", "type": "condition", "config": {}},
            {"id": "gate", "type": "condition_score", "config": {}},
            {
                "id": "blended",
                "type": "weighted_score",
                "config": {
                    "weights": {"direction": 0.6, "magnitude": 0.4},
                    "missing_weight_renormalize": True,
                },
            },
        ]
        graph["edges"] = [
            {"source": "factor_a", "target": "greater", "target_handle": "left"},
            {"source": "factor_b", "target": "greater", "target_handle": "right"},
            {"source": "greater", "target": "choose", "target_handle": "condition"},
            {"source": "factor_a", "target": "choose", "target_handle": "if_true"},
            {"source": "factor_b", "target": "choose", "target_handle": "if_false"},
            {"source": "greater", "target": "gate", "target_handle": "condition"},
            {"source": "choose", "target": "gate", "target_handle": "score"},
            {"source": "gate", "target": "blended", "target_handle": "direction"},
            {"source": "choose", "target": "blended", "target_handle": "magnitude"},
        ]
        graph["outputs"] = {"final_node_id": "blended"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per", "pbr"})

        self.assertIn("toFloat64(l.value > r.value)", result.query)
        self.assertIn("node_choose AS", result.query)
        self.assertIn("c.value != 0 AND NOT ifNull(t.is_valid, false)", result.query)
        self.assertGreaterEqual(result.query.count("c.trade_date AS trade_date"), 2)
        self.assertIn("LEFT JOIN node_factor_b AS f", result.query)
        self.assertIn("LEFT JOIN node_gate AS direction", result.query)
        self.assertIn("LEFT JOIN node_choose AS magnitude", result.query)
        self.assertIn("'condition_not_met'", result.query)

    def test_temporal_nodes_use_history_but_limit_final_dates(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "lag_per", "type": "lag", "config": {"period": 2}},
            {"id": "rolling_per", "type": "rolling_max", "config": {"window": 3}},
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "lag_per", "target_handle": "input"},
            {"source": "lag_per", "target": "rolling_per", "target_handle": "input"},
        ]
        graph["outputs"] = {"final_node_id": "rolling_per"}

        result = compile_factor_lab_graph(
            graph,
            known_factor_ids={"per"},
            trade_dates=["2026-06-30", "2026-03-31"],
        )

        self.assertEqual(result.parameters["temporal_end_date"], "2026-06-30")
        self.assertIn("f.trade_date <= {temporal_end_date:Date}", result.query)
        self.assertIn("lagInFrame(i.value", result.query)
        self.assertIn("rolling_insufficient_history", result.query)
        self.assertIn("ROWS BETWEEN {node_rolling_per_window_preceding:UInt32} PRECEDING", result.query)
        self.assertIn("AND trade_date IN {trade_dates:Array(Date)}", result.query)

    def test_temporal_history_is_limited_to_its_upstream_subgraph(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "temporal_input", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "lagged", "type": "lag", "config": {"period": 20}},
            {"id": "static_input", "type": "factor_input", "config": {"factor_id": "pbr"}},
            {"id": "final", "type": "add", "config": {}},
        ]
        graph["edges"] = [
            {"source": "temporal_input", "target": "lagged", "target_handle": "input"},
            {"source": "lagged", "target": "final", "target_handle": "left"},
            {"source": "static_input", "target": "final", "target_handle": "right"},
        ]
        graph["outputs"] = {"final_node_id": "final"}

        result = compile_factor_lab_graph(
            graph,
            known_factor_ids={"per", "pbr"},
            trade_dates=["2026-06-30"],
        )

        temporal_cte = result.query.split("node_lagged AS", 1)[0]
        static_cte = result.query.split("node_static_input AS", 1)[1].split("node_final AS", 1)[0]
        lag_cte = result.query.split("node_lagged AS", 1)[1].split("node_static_input AS", 1)[0]
        self.assertIn("f.trade_date <= {temporal_end_date:Date}", temporal_cte)
        self.assertIn("f.trade_date IN {trade_dates:Array(Date)}", static_cte)
        self.assertIn("WHERE trade_date IN {trade_dates:Array(Date)}", lag_cte)

    def test_direct_temporal_factor_uses_exact_screen_lookback(self):
        graph = nested_graph()
        graph["experiment"]["start_date"] = "2026-06-30"
        graph["experiment"]["end_date"] = "2026-06-30"
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "lag_per", "type": "lag", "config": {"period": 20}},
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "lag_per", "target_handle": "input"},
        ]
        graph["outputs"] = {"final_node_id": "lag_per"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per"})

        self.assertEqual(result.parameters["node_factor_per_history_row_limit"], 21)
        self.assertIn(
            "LIMIT {node_factor_per_history_row_limit:UInt32} BY security_id",
            result.query,
        )
        insert_query, _ = build_factor_lab_insert_query(
            result,
            factor_id="lab_temporal_screen",
            run_id="11111111-1111-1111-1111-111111111111",
        )
        self.assertIn("SETTINGS max_threads = 2", insert_query)

    def test_temporal_config_requires_positive_integers(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "lag_per", "type": "lag", "config": {"period": 0}},
            {"id": "rolling_per", "type": "rolling_min", "config": {"window": True}},
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "lag_per", "target_handle": "input"},
            {"source": "lag_per", "target": "rolling_per", "target_handle": "input"},
        ]
        graph["outputs"] = {"final_node_id": "rolling_per"}

        result = validate_factor_lab_graph(graph, known_factor_ids={"per"})

        self.assertFalse(result.valid)
        self.assertEqual({"invalid_period", "invalid_window"}, {error.code for error in result.errors})

    def test_weighted_score_can_renormalize_available_inputs(self):
        graph = nested_graph()
        graph["nodes"] = [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per"}},
            {"id": "factor_pbr", "type": "factor_input", "config": {"factor_id": "pbr"}},
            {
                "id": "composite",
                "type": "weighted_score",
                "config": {
                    "weights": {"value": 0.6, "quality": 0.4},
                    "missing_weight_renormalize": True,
                },
            },
        ]
        graph["edges"] = [
            {"source": "factor_per", "target": "composite", "target_handle": "value"},
            {"source": "factor_pbr", "target": "composite", "target_handle": "quality"},
        ]
        graph["outputs"] = {"final_node_id": "composite"}

        result = compile_factor_lab_graph(graph, known_factor_ids={"per", "pbr"})

        self.assertIn("lab_base_universe AS", result.query)
        self.assertIn("LEFT JOIN node_factor_per AS value", result.query)
        self.assertIn("missing_weight_input", result.query)
        self.assertIn("renormalize_zero_weight", result.query)
        self.assertEqual(result.parameters["node_composite_total_weight"], 1.0)

    def test_weighted_score_renormalize_option_must_be_boolean(self):
        graph = nested_graph()
        graph["nodes"][-1] = {
            "id": "div_final",
            "type": "weighted_score",
            "config": {
                "weights": {"left": 1.0, "right": 1.0},
                "missing_weight_renormalize": "yes",
            },
        }
        graph["edges"][-2]["target_handle"] = "left"
        graph["edges"][-1]["target_handle"] = "right"

        result = validate_factor_lab_graph(graph, known_factor_ids={"per", "pbr", "roe"})

        self.assertFalse(result.valid)
        self.assertIn("invalid_missing_weight_renormalize", {error.code for error in result.errors})

    def test_cycle_is_a_hard_error(self):
        graph = nested_graph()
        graph["edges"].append({"source": "div_final", "target": "add_ab", "target_handle": "left"})

        result = validate_factor_lab_graph(graph, known_factor_ids={"per", "pbr", "roe"})

        self.assertFalse(result.valid)
        self.assertIn("duplicate_handle", {error.code for error in result.errors})


if __name__ == "__main__":
    unittest.main()
