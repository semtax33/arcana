from datetime import date, datetime
import pytest
from api.repository.factor_lab_query import compile_factor_lab_graph, validate_factor_lab_graph
from tests.test_factor_lab_v2_clickhouse import lab_database, isolated_lab


def unary_graph(kind, config):
    return {"version": 2, "experiment": {"market": "US", "start_date": "2026-01-06", "end_date": "2026-01-07"},
            "nodes": [{"id": "input", "type": "factor_input", "config": {"factor_id": "roe"}},
                      {"id": "result", "type": kind, "version": 1, "config": config}],
            "edges": [{"source": "input", "target": "result", "target_handle": "input"}],
            "outputs": {"final_node_id": "result"}}


def execute(graph, database):
    client, tables = database
    result = compile_factor_lab_graph(graph, factor_table=tables["factors"], price_table=tables["prices"], security_table=tables["securities"], issuer_table=tables["issuers"])
    return client.query_df(result.query, parameters=result.parameters).to_dict("records")


@pytest.mark.integration
@pytest.mark.parametrize("kind,expected", [("rolling_mean", [20, 35]), ("rolling_std", [10, 5])])
def test_trailing_row_statistics_use_only_current_and_past_values(lab_database, kind, expected):
    graph = unary_graph(kind, {"window": 2, "unit": "row", "min_count": 2, "ddof": 0})
    assert [r["value"] for r in execute(graph, lab_database)] == pytest.approx(expected)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["filter", "mask"])
def test_gate_excludes_failed_conditions_before_ranking(lab_database, kind):
    graph = unary_graph(kind, {})
    graph["nodes"] += [{"id": "threshold", "type": "constant", "config": {"value": 30}},
                       {"id": "gate", "type": "greater_than", "config": {}}]
    graph["edges"] += [{"source": "input", "target": "gate", "target_handle": "left"},
                       {"source": "threshold", "target": "gate", "target_handle": "right"},
                       {"source": "gate", "target": "result", "target_handle": "condition"}]
    assert [r["value"] for r in execute(graph, lab_database)] == [40]


@pytest.mark.integration
@pytest.mark.parametrize("method,expected", [("ols", [1, -1, -1, 1]), ("ridge", [-1.5, -0.5, -1.5, 3.5]), ("ols_collinear", []), ("ols_missing", [])])
def test_residualize_removes_multiple_exposures_with_intercept(lab_database, method, expected):
    client, tables = lab_database
    client.insert(tables["securities"], [[sid, "A", "US"] for sid in "BCD"], column_names=["security_id", "issuer_id", "country"])
    values = {"y": [1, 5, 3, 11], "x": [-1, -1, 1, 1], "z": [-1, 1, -1, 1]}
    if method == "ols_collinear": values["z"] = values["x"][:]
    if method == "ols_missing": values["z"][0] = None
    client.insert(tables["factors"], [[date(2026, 1, 6), sid, factor, "annual", v, datetime(2026, 1, 6)] for factor, vals in values.items() for sid, v in zip("ABCD", vals)])
    graph = unary_graph("residualize", {"exposures": ["size", "beta"], "method": method.split("_")[0], "min_count": 4, "alpha": 4})
    graph["experiment"]["end_date"] = "2026-01-06"
    graph["nodes"] = [{"id": key, "type": "factor_input", "config": {"factor_id": key}} for key in values] + [graph["nodes"][-1]]
    graph["edges"] = [{"source": key, "target": "result", "target_handle": handle} for key, handle in [("y", "target"), ("x", "size"), ("z", "beta")]]
    rows = sorted(execute(graph, lab_database), key=lambda r: r["security_id"])
    assert [r["value"] for r in rows] == pytest.approx(expected)


@pytest.mark.integration
@pytest.mark.parametrize("period,expected", [(1, [10]), (2, [])])
def test_fiscal_lag_uses_reported_quarter_identity_not_days_or_future_revisions(isolated_lab, period, expected):
    client = isolated_lab()
    client.command("""CREATE TABLE dart_report_metadata (security_id String, fiscal_year Int32, fiscal_month UInt8,
        period_end_date Date, report_date Date) ENGINE=Memory""")
    client.insert("dart_report_metadata", [["SEC_US_A", 2025, 6, date(2025, 6, 28), date(2025, 8, 1)],
                                            ["SEC_US_A", 2025, 9, date(2025, 9, 27), date(2025, 11, 1)]])
    for trade, financial_period, value in [(date(2025, 8, 1), date(2025, 6, 28), 10),
                                 (date(2026, 1, 6), date(2025, 9, 27), 20),
                                 (date(2026, 1, 7), date(2025, 6, 28), 999)]:
        client.insert("fact_daily_factor_snapshot", [[trade, "SEC_US_A", "roe", "quarterly", value, trade, financial_period]],
                      column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date", "financial_period"])
    graph = unary_graph("fiscal_lag", {"period": period})
    graph["experiment"]["end_date"] = "2026-01-06"
    graph["nodes"][0]["config"]["financial_basis"] = "quarterly"
    compiled = compile_factor_lab_graph(graph)
    rows = client.query_df(compiled.query, parameters=compiled.parameters).to_dict("records")
    client.close()
    assert [r["value"] for r in rows] == expected
    if period == 1:
        from api.service.dto import FactorLabGraphDto, FactorLabRunRequestDto
        from api.service.factor_lab_service import FactorLabService
        graph["experiment"]["start_date"] = "2026-01-06"
        run = FactorLabService(client_factory=isolated_lab).run_graph(FactorLabRunRequestDto(graph=FactorLabGraphDto(**graph), mode="screen"))
        assert [r.value for r in run.rows] == [10]


@pytest.mark.integration
@pytest.mark.parametrize("kind,count,ddof,expected", [("rolling_mean", 2, 0, [35]), ("rolling_mean", 1, 0, [30, 35]), ("rolling_std", 1, 1, [7.0710678118654755])])
def test_trading_window_missing_days_and_sample_degrees_of_freedom(lab_database, kind, count, ddof, expected):
    graph = unary_graph(kind, {"window": 2, "unit": "trading_day", "min_count": count, "ddof": ddof})
    assert [r["value"] for r in execute(graph, lab_database)] == pytest.approx(expected)


@pytest.mark.integration
def test_residualize_supports_eight_independent_exposures(lab_database):
    client, tables = lab_database
    ids = [f"S{i:02}" for i in range(16)]
    client.insert(tables["securities"], [[sid, "A", "US"] for sid in ids], column_names=["security_id", "issuer_id", "country"])
    names = [f"x{i}" for i in range(1, 9)]
    data = []
    for i, sid in enumerate(ids):
        # Walsh columns are mutually orthogonal, including to the intercept.
        xs = [(-1) ** ((i & j).bit_count()) for j in range(1, 9)]
        y = 10 + sum((j + 1) * x for j, x in enumerate(xs)) + (-1) ** i.bit_count()
        data.extend([date(2026, 1, 6), sid, name, "annual", value, datetime(2026, 1, 6)] for name, value in zip(["y", *names], [y, *xs]))
    client.insert(tables["factors"], data)
    graph = unary_graph("residualize", {"exposures": names, "method": "ols", "min_count": 16})
    graph["experiment"]["end_date"] = "2026-01-06"
    graph["nodes"] = [{"id": key, "type": "factor_input", "config": {"factor_id": key}} for key in ["y", *names]] + [graph["nodes"][-1]]
    graph["edges"] = [{"source": key, "target": "result", "target_handle": "target" if key == "y" else key} for key in ["y", *names]]
    rows = sorted(execute(graph, lab_database), key=lambda r: r["security_id"])
    assert [r["value"] for r in rows] == pytest.approx([1, -1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1, -1, -1, 1])


@pytest.mark.parametrize("kind,config", [
    ("rolling_mean", {"window": True, "unit": "row"}),
    ("rolling_mean", {"window": 2, "unit": "fiscal_period"}),
    ("rolling_std", {"window": 2, "unit": "row", "ddof": 2}),
    ("rolling_std", {"window": 2, "unit": "row", "min_count": 3}),
    ("residualize", {"exposures": [[]], "method": "ols"}),
    ("residualize", {"exposures": ["target"], "method": "ridge", "alpha": -1}),
    ("fiscal_lag", {"period": 0}),
])
def test_invalid_research_contracts_are_rejected_without_execution(kind, config):
    assert not validate_factor_lab_graph(unary_graph(kind, config)).valid
