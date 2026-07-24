import json
import unittest
import uuid
from datetime import date
from unittest.mock import patch

import pandas as pd

from api.model.backtest import BacktestSummary, FactorBacktestResult
from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService


def service_graph():
    return {
        "version": 1,
        "experiment": {
            "name": "service_lab",
            "market": "KR",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "universe": {"type": "market", "sector_codes": [], "industry_group_codes": []},
            "rebalance": {"frequency": "quarterly", "signal_lag_days": 1, "transaction_cost_bps": 0},
        },
        "nodes": [
            {"id": "factor_per", "type": "factor_input", "config": {"factor_id": "per", "financial_basis": "annual"}},
            {"id": "z_per", "type": "zscore", "config": {"group_by": ["trade_date"], "min_count": 2}},
        ],
        "edges": [
            {"source": "factor_per", "target": "z_per", "target_handle": "input"},
        ],
        "outputs": {"final_node_id": "z_per"},
    }


class FakeFactorLabClient:
    def __init__(self):
        self.commands = []
        self.queries = []
        self.closed = False
        self.experiment_exists = True
        self.experiment_name_exists = True
        self.experiment_id = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
        self.experiment_graph_json = json.dumps(service_graph())
        self.run_ids = [str(uuid.UUID("11111111-1111-1111-1111-111111111111"))]
        self.run_exists = True
        self.run_status = "completed"
        self.run_value_count = 12
        self.snapshot_available = True

    def command(self, query, parameters=None):
        self.commands.append((query, parameters or {}))

    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "SELECT DISTINCT trade_date" in query:
            return pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(
                        [
                            "2026-01-01",
                            "2026-01-02",
                            "2026-03-31",
                            "2026-04-01",
                            "2026-06-30",
                            "2026-07-01",
                            "2026-09-30",
                            "2026-10-01",
                            "2026-12-31",
                        ]
                    )
                }
            )
        if "FROM factor_catalog" in query:
            return pd.DataFrame({"factor_id": ["per"]})
        if "HAVING uniqExact(tuple(factor_id, financial_basis))" in query:
            return pd.DataFrame(
                {
                    "trade_date": [
                        pd.Timestamp(value).date()
                        for value in parameters["trade_dates"]
                    ]
                }
            )
        if "eligible_snapshot_dates AS" in query:
            if not self.snapshot_available:
                return pd.DataFrame(
                    {"effective_trade_date": [None], "snapshot_ready": [False]}
                )
            return pd.DataFrame(
                {
                    "effective_trade_date": [pd.Timestamp("2026-12-30").date()],
                    "snapshot_ready": [True],
                }
            )
        if "factor_pair_count" in query and "ORDER BY trade_date DESC" in query:
            return pd.DataFrame({"trade_date": [pd.Timestamp("2026-12-29").date()]})
        if "SELECT 1 AS found" in query and "FROM factor_lab_experiment" in query:
            return pd.DataFrame({"found": [1]}) if self.experiment_exists else pd.DataFrame()
        if "FROM factor_lab_experiment FINAL" in query and "WHERE name =" in query:
            if not self.experiment_name_exists:
                return pd.DataFrame()
            result = {"experiment_id": [self.experiment_id]}
            if "graph_json" in query:
                result["graph_json"] = [self.experiment_graph_json]
            return pd.DataFrame(result)
        if "SELECT run_id" in query and "FROM factor_lab_run" in query:
            return pd.DataFrame({"run_id": self.run_ids})
        if "SELECT status" in query and "FROM factor_lab_run" in query:
            return pd.DataFrame({"status": [self.run_status]}) if self.run_exists else pd.DataFrame()
        if "invalid_reason" in query and "GROUP BY invalid_reason" in query:
            return pd.DataFrame({"invalid_reason": ["zscore_zero_std"], "row_count": [2]})
        if "count() AS row_count" in query and "FROM factor_lab_values" in query:
            return pd.DataFrame({"row_count": [self.run_value_count]})
        if "ranked_values AS" in query:
            return pd.DataFrame(
                {
                    "rank": [1, 2],
                    "security_id": ["SEC_KR_A", "SEC_KR_B"],
                    "ticker": ["000001", "000002"],
                    "stock_name": ["Alpha", "Beta"],
                    "trade_date": [pd.Timestamp("2026-12-30").date(), pd.Timestamp("2026-12-30").date()],
                    "factor_id": ["lab_mock", "lab_mock"],
                    "factor_value": [98.5, 91.25],
                    "score": [98.5, 91.25],
                    "percentile_score": [100.0, 0.0],
                    "is_valid": [True, True],
                    "invalid_reason": ["", ""],
                }
            )
        if "count() AS input_rows" in query:
            return pd.DataFrame(
                {
                    "input_rows": [10],
                    "valid_rows": [8],
                    "invalid_rows": [2],
                    "min_trade_date": [pd.Timestamp("2026-01-01").date()],
                    "max_trade_date": [pd.Timestamp("2026-12-30").date()],
                    "security_count": [5],
                }
            )
        return pd.DataFrame()

    def close(self):
        self.closed = True


class FactorLabServiceTest(unittest.TestCase):
    def test_app_registers_factor_lab_routes(self):
        from api.main import app

        paths = {route.path for route in app.routes}

        self.assertIn("/api/factor-lab/node-types", paths)
        self.assertIn("/api/factor-lab/validate", paths)
        self.assertIn("/api/factor-lab/compile", paths)
        self.assertIn("/api/factor-lab/runs", paths)
        route_methods = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(("/api/factor-lab/experiments/{experiment_id}", "PUT"), route_methods)
        self.assertIn(("/api/factor-lab/experiments/{experiment_id}", "DELETE"), route_methods)
        self.assertIn(("/api/factor-lab/experiments/by-name", "GET"), route_methods)
        self.assertIn(("/api/factor-lab/experiments/by-name", "PUT"), route_methods)
        self.assertIn(("/api/factor-lab/experiments/by-name", "DELETE"), route_methods)
        self.assertIn(("/api/factor-lab/runs/{run_id}/backtest", "POST"), route_methods)

    def test_compile_graph_uses_factor_catalog_allowlist(self):
        client = FakeFactorLabClient()
        graph = FactorLabGraphDto(**service_graph())

        response = FactorLabService(client_factory=lambda: client).compile_graph(graph)

        self.assertIn("node_z_per AS", response.query)
        self.assertEqual(response.final_node_id, "z_per")
        self.assertEqual(response.parameters["node_factor_per_factor_id"], "per")
        self.assertTrue(client.closed)

    def test_run_graph_creates_tables_registers_factor_and_loads_quality(self):
        client = FakeFactorLabClient()
        graph = FactorLabGraphDto(**service_graph())

        response = FactorLabService(client_factory=lambda: client).run_graph(
            FactorLabRunRequestDto(graph=graph)
        )

        command_sql = "\n".join(query for query, _ in client.commands)
        factor_insert_query, factor_insert_params = next(
            (query, params)
            for query, params in client.commands
            if "INSERT INTO factor_lab_values" in query
        )
        self.assertEqual(response.status, "completed")
        self.assertTrue(response.factor_id.startswith("lab_"))
        self.assertEqual(response.quality.valid_rows, 8)
        self.assertEqual(response.quality.invalid_reason_counts["zscore_zero_std"], 2)
        quality_queries = [
            (query, params)
            for query, params in client.queries
            if "FROM factor_lab_values" in query
            and ("count() AS input_rows" in query or "GROUP BY invalid_reason" in query)
        ]
        self.assertEqual(len(quality_queries), 2)
        for query, params in quality_queries:
            self.assertEqual(params["factor_id"], response.factor_id)
            self.assertIn("AND factor_id = {factor_id:String}", query)
        self.assertEqual([row.security_id for row in response.rows], ["SEC_KR_A", "SEC_KR_B"])
        self.assertEqual(response.results, response.rows)
        self.assertEqual(response.rankings, response.rows)
        self.assertEqual(response.positions, response.rows)
        ranking_query, ranking_params = next(
            (query, params) for query, params in client.queries if "ranked_values AS" in query
        )
        self.assertEqual(ranking_params["effective_trade_date"], "2026-12-30")
        self.assertEqual(ranking_params["factor_id"], response.factor_id)
        self.assertIn("SELECT {effective_trade_date:Date} AS trade_date", ranking_query)
        self.assertIn("AND factor_id = {factor_id:String}", ranking_query)
        self.assertEqual(factor_insert_params["start_date"], "2026-12-30")
        self.assertEqual(factor_insert_params["end_date"], "2026-12-30")
        self.assertIn("FROM fact_daily_factor_snapshot AS f", factor_insert_query)
        self.assertIn("CREATE TABLE IF NOT EXISTS factor_lab_experiment", command_sql)
        self.assertIn("INSERT INTO factor_lab_values", command_sql)
        self.assertIn("INSERT INTO factor_catalog", command_sql)
        self.assertIn("completed", {params.get("status") for _, params in client.commands})

    def test_run_graph_falls_back_to_latest_common_raw_factor_date(self):
        client = FakeFactorLabClient()
        client.snapshot_available = False
        graph = FactorLabGraphDto(**service_graph())

        FactorLabService(client_factory=lambda: client).run_graph(
            FactorLabRunRequestDto(graph=graph)
        )

        factor_insert_query, factor_insert_params = next(
            (query, params)
            for query, params in client.commands
            if "INSERT INTO factor_lab_values" in query
        )
        self.assertEqual(factor_insert_params["start_date"], "2026-12-29")
        self.assertEqual(factor_insert_params["end_date"], "2026-12-29")
        self.assertIn("FROM fact_daily_factors AS f", factor_insert_query)

    def test_run_graph_history_mode_keeps_requested_date_range(self):
        client = FakeFactorLabClient()
        graph = FactorLabGraphDto(**service_graph())

        FactorLabService(client_factory=lambda: client).run_graph(
            FactorLabRunRequestDto(graph=graph, mode="history")
        )

        factor_insert_query, factor_insert_params = next(
            (query, params)
            for query, params in client.commands
            if "INSERT INTO factor_lab_values" in query
        )
        self.assertEqual(factor_insert_params["start_date"], "2026-01-01")
        self.assertEqual(factor_insert_params["end_date"], "2026-12-31")
        self.assertIn("FROM fact_daily_factors AS f", factor_insert_query)

    def test_run_graph_history_mode_limits_inputs_to_backtest_signal_dates(self):
        client = FakeFactorLabClient()
        graph = FactorLabGraphDto(**service_graph())

        FactorLabService(client_factory=lambda: client).run_graph(
            FactorLabRunRequestDto(
                graph=graph,
                mode="history",
                history_start_date=date(2026, 1, 2),
                history_end_date=date(2026, 12, 31),
                history_rebalance_frequency="quarterly",
            )
        )

        factor_insert_query, factor_insert_params = next(
            (query, params)
            for query, params in client.commands
            if "INSERT INTO factor_lab_values" in query
        )
        self.assertEqual(
            factor_insert_params["trade_dates"],
            ["2026-01-01", "2026-03-31", "2026-06-30", "2026-09-30"],
        )
        self.assertIn("f.trade_date IN {trade_dates:Array(Date)}", factor_insert_query)

    def test_run_graph_history_mode_can_require_point_in_time_snapshots(self):
        client = FakeFactorLabClient()
        graph_data = service_graph()
        graph_data["experiment"]["factor_data_mode"] = "point_in_time_snapshot"
        graph = FactorLabGraphDto(**graph_data)

        FactorLabService(client_factory=lambda: client).run_graph(
            FactorLabRunRequestDto(
                graph=graph,
                mode="history",
                history_start_date=date(2026, 1, 2),
                history_end_date=date(2026, 12, 31),
                history_rebalance_frequency="quarterly",
            )
        )

        factor_insert_query, _ = next(
            (query, params)
            for query, params in client.commands
            if "INSERT INTO factor_lab_values" in query
        )
        self.assertIn("FROM fact_daily_factor_snapshot AS f", factor_insert_query)

    def test_update_experiment_writes_new_version_with_same_id(self):
        client = FakeFactorLabClient()
        graph = FactorLabGraphDto(**service_graph())
        experiment_id = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))

        response = FactorLabService(client_factory=lambda: client).update_experiment(
            experiment_id,
            FactorLabExperimentSaveRequestDto(graph=graph),
        )

        self.assertEqual(experiment_id, response.experiment_id)
        experiment_inserts = [
            params
            for query, params in client.commands
            if "INSERT INTO factor_lab_experiment" in query
        ]
        self.assertEqual([experiment_id], [params["experiment_id"] for params in experiment_inserts])
        self.assertTrue(any("SELECT 1 AS found" in query for query, _ in client.queries))

    def test_get_experiment_by_name_returns_latest_matching_experiment(self):
        client = FakeFactorLabClient()

        response = FactorLabService(client_factory=lambda: client).get_experiment_by_name(
            " service_lab "
        )

        self.assertEqual(client.experiment_id, response.experiment_id)
        self.assertEqual("service_lab", response.graph.experiment.name)
        name_queries = [
            (query, params)
            for query, params in client.queries
            if "FROM factor_lab_experiment FINAL" in query and "WHERE name =" in query
        ]
        self.assertEqual("service_lab", name_queries[0][1]["name"])
        self.assertIn("ORDER BY updated_at DESC", name_queries[0][0])

    def test_save_experiment_by_name_updates_latest_matching_experiment(self):
        client = FakeFactorLabClient()
        graph_dict = service_graph()
        graph_dict["experiment"]["name"] = " service_lab "
        graph = FactorLabGraphDto(**graph_dict)

        response = FactorLabService(client_factory=lambda: client).save_experiment_by_name(
            FactorLabExperimentSaveRequestDto(graph=graph)
        )

        self.assertEqual(client.experiment_id, response.experiment_id)
        self.assertEqual("service_lab", response.graph.experiment.name)
        experiment_inserts = [
            params
            for query, params in client.commands
            if "INSERT INTO factor_lab_experiment" in query
        ]
        self.assertEqual([client.experiment_id], [params["experiment_id"] for params in experiment_inserts])
        self.assertEqual(["service_lab"], [params["name"] for params in experiment_inserts])

    def test_save_experiment_by_name_creates_id_when_name_is_new(self):
        client = FakeFactorLabClient()
        client.experiment_name_exists = False
        graph = FactorLabGraphDto(**service_graph())

        response = FactorLabService(client_factory=lambda: client).save_experiment_by_name(
            FactorLabExperimentSaveRequestDto(graph=graph)
        )

        self.assertNotEqual(client.experiment_id, response.experiment_id)
        self.assertEqual(response.experiment_id, str(uuid.UUID(response.experiment_id)))

    def test_delete_experiment_by_name_resolves_latest_matching_id(self):
        client = FakeFactorLabClient()

        response = FactorLabService(client_factory=lambda: client).delete_experiment_by_name(
            "service_lab"
        )

        self.assertTrue(response.deleted)
        experiment_deletes = [
            params
            for query, params in client.commands
            if "ALTER TABLE factor_lab_experiment" in query
        ]
        self.assertEqual([client.experiment_id], [params["experiment_id"] for params in experiment_deletes])

    def test_delete_experiment_removes_experiment_runs_values_and_lab_catalog(self):
        client = FakeFactorLabClient()
        experiment_id = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))

        response = FactorLabService(client_factory=lambda: client).delete_experiment(experiment_id)

        command_sql = "\n".join(query for query, _ in client.commands)
        delete_params = [params for query, params in client.commands if "ALTER TABLE factor_catalog" in query][0]
        self.assertTrue(response.deleted)
        self.assertIn("ALTER TABLE factor_lab_experiment", command_sql)
        self.assertIn("ALTER TABLE factor_lab_run", command_sql)
        self.assertIn("ALTER TABLE factor_lab_node_cache", command_sql)
        self.assertIn("ALTER TABLE factor_lab_values", command_sql)
        self.assertEqual(["lab_11111111111111111111111111111111"], delete_params["factor_ids"])

    def test_update_missing_experiment_raises_key_error(self):
        client = FakeFactorLabClient()
        client.experiment_exists = False
        graph = FactorLabGraphDto(**service_graph())

        with self.assertRaises(KeyError):
            FactorLabService(client_factory=lambda: client).update_experiment(
                str(uuid.UUID("22222222-2222-2222-2222-222222222222")),
                FactorLabExperimentSaveRequestDto(graph=graph),
            )

    def test_run_backtest_validates_run_and_builds_lab_factor_request(self):
        client = FakeFactorLabClient()
        run_id = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
        result = FactorBacktestResult(
            summary=BacktestSummary(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                rebalance_frequency="quarterly",
            ),
            equity_curve=[],
            rebalance_history=[],
            annual_returns=[],
        )

        with patch("api.service.factor_lab_service.BacktestService") as backtest_service:
            instance = backtest_service.return_value
            instance.run_factor_backtest.return_value = result

            response = FactorLabService(client_factory=lambda: client).run_backtest(
                run_id,
                FactorLabBacktestRequestDto(
                    top_percent=15,
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                    rebalance_frequency="quarterly",
                    market="KR",
                    max_positions=30,
                    transaction_cost_bps=5,
                ),
            )

        self.assertIs(response, result)
        request = instance.run_factor_backtest.call_args.args[0]
        self.assertEqual("factor_lab_values", request.factor_table)
        self.assertEqual("lab", request.financial_basis)
        self.assertEqual("lab_11111111111111111111111111111111", request.conditions[0].factor_id)
        self.assertEqual(15, request.conditions[0].top_percent)
        self.assertEqual("higher", request.conditions[0].rank_direction)
        self.assertEqual("KR", request.market)
        self.assertEqual(30, request.max_positions)
        self.assertEqual(5, request.transaction_cost_bps)

    def test_run_backtest_missing_run_raises_key_error(self):
        client = FakeFactorLabClient()
        client.run_exists = False

        with self.assertRaises(KeyError):
            FactorLabService(client_factory=lambda: client).run_backtest(
                str(uuid.UUID("11111111-1111-1111-1111-111111111111")),
                FactorLabBacktestRequestDto(
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                    rebalance_frequency="quarterly",
                ),
            )

    def test_run_backtest_without_values_raises_value_error(self):
        client = FakeFactorLabClient()
        client.run_value_count = 0

        with self.assertRaises(ValueError):
            FactorLabService(client_factory=lambda: client).run_backtest(
                str(uuid.UUID("11111111-1111-1111-1111-111111111111")),
                FactorLabBacktestRequestDto(
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                    rebalance_frequency="quarterly",
                ),
            )

    def test_validation_reports_unknown_factor_without_running(self):
        client = FakeFactorLabClient()
        graph_dict = service_graph()
        graph_dict["nodes"][0]["config"]["factor_id"] = "unknown_factor"
        graph = FactorLabGraphDto(**graph_dict)

        response = FactorLabService(client_factory=lambda: client).validate_graph(graph)

        self.assertFalse(response.valid)
        self.assertIn("unknown_factor_id", {error.code for error in response.errors})


if __name__ == "__main__":
    unittest.main()
