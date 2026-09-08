from copy import deepcopy
import pytest

from api.repository.factor_lab_query import compile_factor_lab_graph, validate_factor_lab_graph
from api.service.dto import FactorLabGraphDto


def graph_v2():
    return {
        "version": 2,
        "experiment": {"name": "outcome", "market": "US", "start_date": "2026-01-02", "end_date": "2026-01-09"},
        "nodes": [
            {"id": "score", "type": "factor_input", "version": 1, "config": {"factor_id": "roe"}},
            {"id": "future", "type": "forward_outcome", "version": 1, "config": {
                "target_factor_id": "roe", "financial_basis": "annual", "measure": "change",
                "horizons": [1, 5], "unit": "trading_day", "bucket_count": 2, "score_order": "higher",
            }},
        ],
        "edges": [{"source": "score", "target": "future", "target_handle": "score"}],
        "outputs": {"final_node_id": "score", "evaluation_node_ids": ["future"]},
    }


def test_saved_graph_keeps_terminal_evaluations_separate_from_score():
    graph = FactorLabGraphDto(**graph_v2()).model_dump(mode="json")
    assert graph["outputs"]["evaluation_node_ids"] == ["future"]
    result = validate_factor_lab_graph(graph, known_factor_ids={"roe"})
    assert result.valid, result.errors
    assert not any(w.node_id == "future" and w.code == "disconnected_node" for w in result.warnings)
    assert compile_factor_lab_graph(graph).final_node_id == "score"


@pytest.mark.parametrize("case", ["graph_version", "node_version", "legacy_new_node", "label_as_score", "label_edge", "unknown_evaluation", "duplicate_evaluation", "bad_unit", "bad_horizons", "bad_target", "bad_measure", "bad_basis"])
def test_invalid_evaluation_contracts_are_rejected(case):
    graph = graph_v2()
    config = graph["nodes"][1]["config"]
    if case == "graph_version": graph["version"] = 99
    elif case == "node_version": graph["nodes"][0]["version"] = 99
    elif case == "legacy_new_node": graph["version"] = 1
    elif case == "label_as_score": graph["outputs"]["final_node_id"] = "future"
    elif case == "label_edge": graph["edges"].append({"source": "future", "target": "score", "target_handle": "input"})
    elif case == "unknown_evaluation": graph["outputs"]["evaluation_node_ids"] = ["absent"]
    elif case == "duplicate_evaluation": graph["outputs"]["evaluation_node_ids"] = ["future", "future"]
    elif case == "bad_unit": config["unit"] = "fiscal_period"
    elif case == "bad_horizons": config["horizons"] = [True, -1]
    elif case == "bad_target": config["target_factor_id"] = "absent"
    elif case == "bad_measure": config["measure"] = "magic"
    elif case == "bad_basis": config["financial_basis"] = "lab"
    assert not validate_factor_lab_graph(graph, known_factor_ids={"roe"}).valid


def test_legacy_graph_dto_round_trip_keeps_same_score_query_and_hash():
    graph = graph_v2()
    graph["version"] = 1
    graph["nodes"] = [graph["nodes"][0]]
    graph["nodes"][0].pop("version")
    graph["edges"] = []
    graph["outputs"] = {"final_node_id": "score"}
    # DTO defaults are already part of saved API graphs; only new fields may differ.
    legacy = FactorLabGraphDto(**graph).model_dump(mode="json")
    legacy["nodes"][0].pop("version")
    legacy["outputs"].pop("evaluation_node_ids")
    reloaded = FactorLabGraphDto(**legacy).model_dump(mode="json")
    before, after = compile_factor_lab_graph(legacy), compile_factor_lab_graph(reloaded)
    assert (before.query, before.parameters, before.graph_hash) == (after.query, after.parameters, after.graph_hash)


@pytest.mark.parametrize("field", ["financial_basis", "measure", "unit", "score_order", "period_policy"])
def test_malformed_outcome_options_return_validation_errors(field):
    graph = graph_v2()
    graph["nodes"][1]["config"][field] = []
    assert not validate_factor_lab_graph(graph).valid


def test_v2_sql_preview_uses_the_requested_pit_score_source():
    from tests.test_factor_lab_service import FakeFactorLabClient, service_graph
    from api.service.factor_lab_service import FactorLabService
    graph = service_graph()
    graph["version"] = 2
    graph["experiment"]["factor_data_mode"] = "point_in_time_snapshot"
    compiled = FactorLabService(client_factory=FakeFactorLabClient).compile_graph(FactorLabGraphDto(**graph))
    assert "FROM fact_daily_factor_snapshot AS f" in compiled.query
