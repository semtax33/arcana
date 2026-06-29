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
        self.run_ids = [str(uuid.UUID("11111111-1111-1111-1111-111111111111"))]
        self.run_exists = True
        self.run_status = "completed"
        self.run_value_count = 12

    def command(self, query, parameters=None):
        self.commands.append((query, parameters or {}))

    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "FROM factor_catalog" in query:
            return pd.DataFrame({"factor_id": ["per"]})
        if "SELECT 1 AS found" in query and "FROM factor_lab_experiment" in query:
            return pd.DataFrame({"found": [1]}) if self.experiment_exists else pd.DataFrame()
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
                    "trade_date": [pd.Timestamp("2026-01-10").date(), pd.Timestamp("2026-01-10").date()],
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
                    "max_trade_date": [pd.Timestamp("2026-01-10").date()],
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
        self.assertEqual(response.status, "completed")
        self.assertTrue(response.factor_id.startswith("lab_"))
        self.assertEqual(response.quality.valid_rows, 8)
        self.assertEqual(response.quality.invalid_reason_counts["zscore_zero_std"], 2)
        self.assertEqual([row.security_id for row in response.rows], ["SEC_KR_A", "SEC_KR_B"])
        self.assertEqual(response.results, response.rows)
        self.assertEqual(response.rankings, response.rows)
        self.assertEqual(response.positions, response.rows)
        self.assertIn("CREATE TABLE IF NOT EXISTS factor_lab_experiment", command_sql)
        self.assertIn("INSERT INTO factor_lab_values", command_sql)
        self.assertIn("INSERT INTO factor_catalog", command_sql)
        self.assertIn("completed", {params.get("status") for _, params in client.commands})

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
