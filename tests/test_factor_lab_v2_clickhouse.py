"""Numerical public compiler checks in session-local tables; no production writes."""
import os
import uuid
from datetime import date, datetime

import pytest

from api.config.clickhouse import get_clickhouse_client
from api.repository.factor_lab_query import compile_factor_lab_graph
from api.service.dto import FactorLabExperimentSaveRequestDto, FactorLabGraphDto, FactorLabRunRequestDto
from api.service.factor_lab_service import FactorLabService
from api.service.factor_lab_evaluation_service import FactorLabEvaluationService


@pytest.fixture
def lab_database():
    if os.getenv("ARCANA_TEST_CLICKHOUSE") != "1":
        pytest.skip("set ARCANA_TEST_CLICKHOUSE=1 for session-local ClickHouse checks")
    client = get_clickhouse_client()
    prefix = "labtest_" + uuid.uuid4().hex
    names = {key: f"{prefix}_{key}" for key in ("factors", "prices", "securities", "issuers")}
    schemas = {
        "factors": "trade_date Date, security_id String, factor_id String, financial_basis String, factor_value Nullable(Float64), updated_at DateTime",
        "prices": "trade_date Date, security_id String",
        "securities": "security_id String, issuer_id String, country String, updated_at DateTime DEFAULT now()",
        "issuers": "issuer_id String, sector_code String, industry_group_code String, updated_at DateTime DEFAULT now()",
    }
    try:
        for key, schema in schemas.items():
            client.command(f"CREATE TEMPORARY TABLE {names[key]} ({schema}) ENGINE=Memory")
        client.insert(names["securities"], [["A", "A", "US"]], column_names=["security_id", "issuer_id", "country"])
        client.insert(names["issuers"], [["A", "tech", "software"]], column_names=["issuer_id", "sector_code", "industry_group_code"])
        client.insert(names["prices"], [[date(2026, 1, d), "A"] for d in [2, 5, 6, 7]])
        client.insert(names["factors"], [[date(2026, 1, d), "A", "roe", "annual", v, datetime(2026, 1, d)] for d, v in [(2, 10), (6, 30), (7, 40)]])
        yield client, names
    finally:
        client.close()


@pytest.mark.integration
def test_trading_day_lag_does_not_skip_missing_input_days(lab_database):
    client, tables = lab_database
    graph = {
        "version": 2,
        "experiment": {"market": "US", "start_date": "2026-01-06", "end_date": "2026-01-07"},
        "nodes": [
            {"id": "input", "type": "factor_input", "config": {"factor_id": "roe"}},
            {"id": "previous", "type": "lag", "version": 2, "config": {"period": 1, "unit": "trading_day"}},
        ],
        "edges": [{"source": "input", "target": "previous", "target_handle": "input"}],
        "outputs": {"final_node_id": "previous"},
    }
    def run():
        result = compile_factor_lab_graph(graph, factor_table=tables["factors"], price_table=tables["prices"], security_table=tables["securities"], issuer_table=tables["issuers"])
        return [(r["trade_date"], r["value"]) for r in client.query_df(result.query, parameters=result.parameters).to_dict("records")]
    assert [(str(d)[:10], v) for d, v in run()] == [("2026-01-07", 30)]
    graph["nodes"][1].update(version=1, config={"period": 1})
    assert [(str(d)[:10], v) for d, v in run()] == [("2026-01-06", 10), ("2026-01-07", 30)]


@pytest.fixture
def isolated_lab():
    if os.getenv("ARCANA_TEST_CLICKHOUSE") != "1":
        pytest.skip("set ARCANA_TEST_CLICKHOUSE=1 for isolated database checks")
    admin = get_clickhouse_client()
    source_db = admin.query("SELECT currentDatabase()").first_row[0]
    db = "test_factor_lab_" + uuid.uuid4().hex
    admin.command(f"CREATE DATABASE {db}")
    client = get_clickhouse_client(database=db)
    try:
        for table in ["fact_daily_factors", "fact_daily_factor_snapshot", "factor_catalog", "security_master", "issuers", "price_daily", "identifiers"]:
            admin.command(f"CREATE TABLE {db}.{table} AS {source_db}.{table}")
        client.insert("security_master", [["SEC_US_A", "A", "US"]], column_names=["security_id", "issuer_id", "country"])
        client.insert("issuers", [["A", "tech", "software"]], column_names=["issuer_id", "sector_code", "industry_group_code"])
        client.insert("factor_catalog", [["roe", "ROE"]], column_names=["factor_id", "factor_name"])
        for day, value in [(2, 10), (5, 12), (6, 15), (7, 20)]:
            d = date(2026, 1, day)
            client.insert("price_daily", [[d, "SEC_US_A"]], column_names=["trade_date", "security_id"])
            client.insert("fact_daily_factors", [[d, "SEC_US_A", "roe", "annual", 999]], column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value"])
            client.insert("fact_daily_factor_snapshot", [[d, "SEC_US_A", "roe", "annual", value, d, date(2025, 12, 31)]], column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date", "financial_period"])
        yield lambda: get_clickhouse_client(database=db)
    finally:
        client.close()
        # Exactly the isolated database created by this fixture; never the source database.
        assert db.startswith("test_factor_lab_") and db != source_db
        admin.command(f"DROP DATABASE {db} SYNC")
        admin.close()


@pytest.mark.integration
def test_trading_calendar_uses_market_days_outside_selected_sector(lab_database):
    client, tables = lab_database
    client.insert(tables["securities"], [["B", "B", "US"]], column_names=["security_id", "issuer_id", "country"])
    client.insert(tables["issuers"], [["B", "energy", "oil"]], column_names=["issuer_id", "sector_code", "industry_group_code"])
    client.insert(tables["prices"], [[date(2026, 1, 8), "B"], [date(2026, 1, 9), "A"]])
    client.insert(tables["factors"], [[date(2026, 1, 9), "A", "roe", "annual", 50, datetime(2026, 1, 9)]])
    graph = {"version": 2, "experiment": {"market": "US", "start_date": "2026-01-09", "end_date": "2026-01-09", "universe": {"sector_codes": ["tech"]}},
             "nodes": [{"id": "input", "type": "factor_input", "config": {"factor_id": "roe"}}, {"id": "lag", "type": "lag", "version": 2, "config": {"period": 1, "unit": "trading_day"}}],
             "edges": [{"source": "input", "target": "lag", "target_handle": "input"}], "outputs": {"final_node_id": "lag"}}
    compiled = compile_factor_lab_graph(graph, factor_table=tables["factors"], price_table=tables["prices"], security_table=tables["securities"], issuer_table=tables["issuers"])
    assert client.query_df(compiled.query, parameters=compiled.parameters).empty


@pytest.mark.integration
def test_run_materializes_scores_and_multiple_evaluations_from_pit_snapshots(isolated_lab):
    from tests.test_factor_lab_v2 import graph_v2
    graph = graph_v2()
    graph["experiment"].update(end_date="2026-01-06", factor_data_mode="point_in_time_snapshot")
    graph["nodes"].append({"id": "level", "type": "forward_outcome", "version": 1,
                          "config": {**graph["nodes"][1]["config"], "measure": "level", "horizons": [1]}})
    graph["edges"].append({"source": "score", "target": "level", "target_handle": "score"})
    graph["outputs"]["evaluation_node_ids"].append("level")
    service = FactorLabService(client_factory=isolated_lab)
    saved = service.save_experiment(FactorLabExperimentSaveRequestDto(graph=FactorLabGraphDto(**graph)))
    run = service.run_graph(FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="history", evaluation_as_of=date(2026, 1, 7)))
    assert run.evaluation is not None, run.warnings
    assert run.evaluation["evaluations"][0]["horizons"][0]["baseline"]["mean"] == pytest.approx(10 / 3)
    assert run.evaluation["evaluations"][1]["horizons"][0]["baseline"]["mean"] == pytest.approx(47 / 3)
    assert run.rows[0].value != 999
    retrieved = FactorLabEvaluationService(client_factory=isolated_lab).list_evaluations(run.run_id)
    assert retrieved["evaluations"][0] == run.evaluation
    # Changing source data must not rewrite an existing evaluation or its scores.
    client = isolated_lab()
    client.insert("fact_daily_factor_snapshot", [[date(2026, 1, 7), "SEC_US_A", "roe", "annual", 25, date(2026, 1, 7), date(2025, 12, 31), datetime(2030, 1, 1)]],
                  column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date", "financial_period", "updated_at"])
    client.close()
    newer = FactorLabEvaluationService(client_factory=isolated_lab).evaluate(run.run_id, as_of=date(2026, 1, 7))
    assert newer["evaluation_id"] != run.evaluation["evaluation_id"]
    assert newer["evaluations"][0]["horizons"][0]["baseline"]["mean"] == 5
    history = FactorLabEvaluationService(client_factory=isolated_lab).list_evaluations(run.run_id)["evaluations"]
    assert next(r for r in history if r["evaluation_id"] == run.evaluation["evaluation_id"]) == run.evaluation
    service.delete_experiment(saved.experiment_id)
    assert FactorLabEvaluationService(client_factory=isolated_lab).list_evaluations(run.run_id)["evaluations"] == []


@pytest.mark.integration
def test_pit_score_does_not_use_a_cell_with_source_after_signal_date(isolated_lab):
    from tests.test_factor_lab_v2 import graph_v2
    client = isolated_lab()
    client.insert("fact_daily_factor_snapshot", [[date(2026, 1, 6), "SEC_US_A", "roe", "annual", 100, date(2026, 1, 7), datetime(2030, 1, 1)]],
                  column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date", "updated_at"])
    graph = graph_v2()
    graph["outputs"]["evaluation_node_ids"] = []
    graph["experiment"]["end_date"] = "2026-01-06"
    compiled = compile_factor_lab_graph(graph, factor_table="fact_daily_factor_snapshot")
    rows = client.query_df(compiled.query, parameters=compiled.parameters).to_dict("records")
    client.close()
    assert not any(str(r["trade_date"])[:10] == "2026-01-06" for r in rows)
