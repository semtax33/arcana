"""Real HTTP-to-ClickHouse regressions using an isolated, explicitly opted-in DB."""
import math
import os
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import app


def resource_graph(branches=4):
    graph = {
        "version": 2,
        "experiment": {"name": "resource_regression", "market": "US",
                       "start_date": "2026-01-02", "end_date": "2026-01-02",
                       "universe": {"size_percentile": {"side": "top", "percent": 100}}},
        "nodes": [], "edges": [], "outputs": {"final_node_id": "final_score"},
    }
    weights = {}
    for i in range(branches):
        source, winsor, score = f"input_{i}", f"winsor_{i}", f"score_{i}"
        graph["nodes"].extend([
            {"id": source, "type": "factor_input", "config": {
                "factor_id": "roe", "missing_policy": "cross_sectional_median"}},
            {"id": winsor, "type": "winsorize", "config": {
                "lower_quantile": 0, "upper_quantile": 1}},
            {"id": score, "type": "shrunk_zscore", "config": {
                "min_market_count": 2, "min_group_count": 2}},
        ])
        graph["edges"].extend([
            {"source": source, "target": winsor, "target_handle": "input"},
            {"source": winsor, "target": score, "target_handle": "input"},
            {"source": score, "target": "final_score", "target_handle": f"s{i}"},
        ])
        weights[f"s{i}"] = 1 / branches
    graph["nodes"].append({"id": "final_score", "type": "weighted_score", "config": {
        "weights": weights, "missing_weight_renormalize": True}})
    return graph


@pytest.fixture
def resource_api(monkeypatch):
    if os.getenv("ARCANA_TEST_FACTOR_LAB_RESOURCES") != "1":
        pytest.skip("set ARCANA_TEST_FACTOR_LAB_RESOURCES=1 with an isolated ClickHouse server")
    import clickhouse_connect
    real_factory = clickhouse_connect.get_client
    connection = {"host": os.getenv("ARCANA_TEST_CH_HOST", "127.0.0.1"),
                  "port": int(os.getenv("ARCANA_TEST_CH_PORT", "18123"))}
    admin = real_factory(**connection)
    db = "test_lab_resources_" + uuid.uuid4().hex
    admin.command(f"CREATE DATABASE {db}")
    seed = real_factory(**connection, database=db)
    common = "updated_at DateTime DEFAULT now()"
    schemas = {
        "security_master": f"security_id String, issuer_id String, country String, exchange_code String, {common}",
        "issuers": f"issuer_id String, sector_code String, industry_group_code String, legal_name_ko String DEFAULT '', legal_name_en String DEFAULT '', {common}",
        "identifiers": "security_id String, id_value String, id_type String, is_primary Bool DEFAULT true",
        "price_daily": "security_id String, trade_date Date",
        "fact_daily_factors": f"security_id String, trade_date Date, factor_id String, financial_basis String, factor_value Nullable(Float64), currency String, {common}",
        "fact_daily_factor_snapshot": f"security_id String, trade_date Date, factor_id String, financial_basis String, factor_value Nullable(Float64), currency String, source_trade_date Date, financial_period Date, {common}",
        "factor_catalog": f"factor_id String, factor_name String DEFAULT '', factor_type String DEFAULT '', factor_group String DEFAULT '', unit String DEFAULT '', value_direction String DEFAULT '', description String DEFAULT '', is_active Bool DEFAULT true, created_at DateTime DEFAULT now(), {common}",
        "security_listing_episodes": "security_id String, issuer_id String, country String, exchange_code String, published_date Date, valid_from Date, valid_until Nullable(Date), status String, security_type String",
        "security_trading_halts": "security_id String, start_date Date, end_date Nullable(Date), status String",
    }
    try:
        for name, schema in schemas.items():
            seed.command(f"CREATE TABLE {name} ({schema}) ENGINE=MergeTree ORDER BY tuple()")
        day = date(2026, 1, 2)
        for letter, value in zip("ABC", [10, 20, 30]):
            sid = "SEC_US_" + letter
            seed.insert("security_master", [[sid, letter, "US", "NASDAQ"]],
                        column_names=["security_id", "issuer_id", "country", "exchange_code"])
            seed.insert("issuers", [[letter, "45", "4510"]],
                        column_names=["issuer_id", "sector_code", "industry_group_code"])
            seed.insert("price_daily", [[sid, day]])
            seed.insert("fact_daily_factors", [[sid, day, f, "annual", v, "USD"]
                        for f, v in [("roe", value), ("mcap_mil", value * 10)]],
                        column_names=["security_id", "trade_date", "factor_id", "financial_basis", "factor_value", "currency"])
        seed.insert("factor_catalog", [["roe"]], column_names=["factor_id"])

        def factory(**kwargs):
            kwargs.update(connection, database=db, username="default", password="",
                          connect_timeout=3, send_receive_timeout=40)
            kwargs["settings"] = {**kwargs.get("settings", {}), "max_threads": 2,
                                  "max_memory_usage": 64 * 1024 * 1024, "max_execution_time": 20}
            return real_factory(**kwargs)

        monkeypatch.setattr(clickhouse_connect, "get_client", factory)
        yield TestClient(app), seed
    finally:
        seed.close()
        assert db.startswith("test_lab_resources_")
        admin.command(f"DROP DATABASE {db} SYNC")
        admin.close()


@pytest.mark.integration
@pytest.mark.parametrize("branches", [1, 4, 32])
@pytest.mark.parametrize("mode", ["screen", "history"])
def test_multifactor_screen_completes_within_memory_budget(resource_api, branches, mode):
    http, _ = resource_api
    response = http.post("/api/factor-lab/runs", json={"graph": resource_graph(branches), "mode": mode})
    assert response.status_code == 200, response.text[:2000]
    rows = response.json()["rows"]
    assert [r["security_id"] for r in rows] == ["SEC_US_C", "SEC_US_B", "SEC_US_A"]
    assert [r["value"] for r in rows] == pytest.approx([math.sqrt(1.5), 0, -math.sqrt(1.5)])


@pytest.mark.integration
def test_history_stages_preserve_warmup_and_missing_trading_day(resource_api):
    http, seed = resource_api
    for day, values in [(5, [30, None, 10]), (6, [100, 100, 100])]:
        for letter, value in zip("ABC", values):
            sid, when = "SEC_US_" + letter, date(2026, 1, day)
            seed.insert("price_daily", [[sid, when]])
            factors = [[sid, when, "mcap_mil", "annual", 100, "USD"]]
            if value is not None:
                factors.append([sid, when, "roe", "annual", value, "USD"])
            seed.insert("fact_daily_factors", factors,
                        column_names=["security_id", "trade_date", "factor_id", "financial_basis", "factor_value", "currency"])
    graph = resource_graph(1)
    graph["experiment"].update(start_date="2026-01-05", end_date="2026-01-06")
    graph["nodes"][0]["config"]["missing_policy"] = "drop"
    graph["nodes"].append({"id": "previous", "type": "lag", "version": 2,
                           "config": {"period": 1, "unit": "trading_day"}})
    graph["edges"][0]["source"] = "previous"
    graph["edges"].append({"source": "input_0", "target": "previous", "target_handle": "input"})
    from api.repository.factor_lab_query import compile_factor_lab_graph
    compiled = compile_factor_lab_graph(graph, listing_table="security_listing_episodes",
                                        trading_halt_table="security_trading_halts")
    reference = seed.query(f"SELECT trade_date, security_id, value FROM ({compiled.query}) "
                           "ORDER BY trade_date, security_id", parameters=compiled.parameters).result_rows
    response = http.post("/api/factor-lab/runs", json={"graph": graph, "mode": "history"})
    assert response.status_code == 200, response.text[:2000]
    rows = seed.query("""SELECT trade_date, security_id, factor_value FROM factor_lab_values
        WHERE run_id = {run:UUID} ORDER BY trade_date, security_id""",
        parameters={"run": response.json()["run_id"]}).result_rows
    assert [(str(d), sid) for d, sid, _ in rows] == [
        ("2026-01-05", "SEC_US_A"), ("2026-01-05", "SEC_US_C"),
        ("2026-01-06", "SEC_US_A"), ("2026-01-06", "SEC_US_C")]
    # Lag anchors on existing input rows: B has no current row on Jan 5,
    # and no preceding trading-day value on Jan 6. Neither is backfilled.
    assert [v for _, _, v in rows] == pytest.approx([-1, 1, 1, -1])
    assert [(d, sid) for d, sid, _ in rows] == [(d, sid) for d, sid, _ in reference]
    assert [v for _, _, v in rows] == pytest.approx([v for _, _, v in reference])
