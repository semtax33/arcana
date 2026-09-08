"""Database boundary fixtures exercise public evaluation save/read behavior."""
import json
from datetime import date

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from api.service.factor_lab_evaluation_service import FactorLabEvaluationService


class EvaluationDatabase:
    def __init__(self):
        self.saved = []
        self.status = "completed"

    def close(self):
        pass

    def command(self, query, parameters=None):
        if "INSERT INTO factor_lab_evaluation" in query:
            self.saved.append(parameters)

    def query_df(self, query, parameters=None):
        if "FROM factor_lab_run FINAL" in query:
            return pd.DataFrame([{"status": self.status}])
        if "FROM factor_lab_run_definition" in query:
            return pd.DataFrame([{"graph_json": json.dumps({"version": 2, "experiment": {"market": "US"},
                "nodes": [{"id": "outcome", "type": "forward_outcome", "version": 1, "config": {
                    "target_factor_id": "roe", "financial_basis": "annual", "measure": "change", "horizons": [1], "unit": "trading_day", "bucket_count": 2, "score_order": "higher"}}],
                "edges": [{"source": "score", "target": "outcome", "target_handle": "score"}],
                "outputs": {"final_node_id": "score", "evaluation_node_ids": ["outcome"]}}), "graph_hash": "frozen_graph"}])
        if "FROM factor_lab_node_cache" in query:
            return pd.DataFrame([{"trade_date": date(2026, 1, 2), "security_id": "A", "value": 1}])
        if "FROM price_daily" in query:
            return pd.DataFrame([{"trade_date": date(2026, 1, 2)}, {"trade_date": date(2026, 1, 5)}])
        if "FROM fact_daily_factor_snapshot" in query:
            return pd.DataFrame([{"trade_date": date(2026, 1, d), "security_id": "A", "value": v, "financial_period": "2025-12-31", "source_trade_date": date(2026, 1, d)} for d, v in [(2, 10), (5, 12)]])
        if "FROM factor_lab_evaluation" in query:
            return pd.DataFrame(self.saved)
        raise AssertionError(query)


def test_evaluate_run_then_read_returns_the_same_frozen_results():
    database = EvaluationDatabase()
    service = FactorLabEvaluationService(client_factory=lambda: database)
    result = service.evaluate("11111111-1111-1111-1111-111111111111", as_of=date(2026, 1, 5))
    assert result["evaluations"][0]["horizons"][0]["baseline"]["mean"] == 2
    stored = service.list_evaluations(result["run_id"])
    assert stored["evaluations"][0]["evaluation_id"] == result["evaluation_id"]
    assert stored["evaluations"][0]["evaluations"] == result["evaluations"]


def test_http_evaluation_can_be_created_and_loaded_independently():
    from api.controller.factor_lab_controller import router
    app = FastAPI()
    app.include_router(router)
    database = EvaluationDatabase()
    with patch("api.controller.factor_lab_controller.get_clickhouse_client", return_value=database):
        client = TestClient(app)
        run_id = "11111111-1111-1111-1111-111111111111"
        created = client.post(f"/api/factor-lab/runs/{run_id}/evaluations", json={"as_of": "2026-01-05"})
        assert created.status_code == 200, created.text
        loaded = client.get(f"/api/factor-lab/runs/{run_id}/evaluations")
        assert loaded.status_code == 200
        assert loaded.json()["evaluations"][0] == created.json()


def test_partial_run_cannot_be_evaluated():
    database = EvaluationDatabase()
    database.status = "failed"
    with pytest.raises(ValueError, match="completed"):
        FactorLabEvaluationService(client_factory=lambda: database).evaluate(
            "11111111-1111-1111-1111-111111111111", as_of=date(2026, 1, 5))
    assert database.saved == []


def test_evaluation_cutoff_cannot_precede_frozen_signals():
    database = EvaluationDatabase()
    with pytest.raises(ValueError, match="signal"):
        FactorLabEvaluationService(client_factory=lambda: database).evaluate(
            "11111111-1111-1111-1111-111111111111", as_of=date(2026, 1, 1))
    assert database.saved == []
