from __future__ import annotations

import inspect

from scripts.finalize_us_pvgo_photo_shadow_audit import (
    AUDIT_RUN_ID,
    FINAL_EXPERIMENT_ID,
    FINAL_RUN_ID,
    MODEL_NAME,
    build_shadow_strategy_graph,
    run,
)
from scripts.improve_us_pvgo_from_comprehensive_score import (
    SOURCE_EXPERIMENT_ID,
    SOURCE_RUN_ID,
    build_checklist_module_graph,
)


def _payload(graph):
    return graph.model_dump(mode="json")


def _nodes_by_id(graph):
    return {node["id"]: node for node in _payload(graph)["nodes"]}


def test_photo_audit_module_is_point_in_time_and_balanced() -> None:
    graph = build_checklist_module_graph("test_photo_audit")
    payload = _payload(graph)
    nodes = _nodes_by_id(graph)

    assert payload["experiment"]["factor_data_mode"] == "point_in_time_snapshot"
    assert payload["experiment"]["snapshot_coverage_policy"] == "allow_missing_inputs"
    assert payload["outputs"]["final_node_id"] == "module_rank_score"
    assert nodes["checklist_confirmation"]["config"]["weights"] == {
        "checklist": 0.5,
        "qualified_rnd": 0.5,
    }
    assert nodes["checklist_pass_floor"]["config"]["value"] == 2.5

    factor_ids = {
        node["config"]["factor_id"]
        for node in payload["nodes"]
        if node["type"] == "factor_input"
    }
    assert factor_ids == {
        "rnd_to_market_cap",
        "gross_profitability_pct",
        "sales_growth_3y",
        "fcf_to_ev_yield",
        "current_ratio",
        "tr_6_1",
    }


def test_final_strategy_keeps_photo_module_at_zero_live_weight() -> None:
    graph = build_shadow_strategy_graph(AUDIT_RUN_ID)
    payload = _payload(graph)
    nodes = _nodes_by_id(graph)

    assert payload["experiment"]["name"] == MODEL_NAME
    assert nodes["source_score"]["config"]["factor_id"] == (
        "lab_" + SOURCE_RUN_ID.replace("-", "")
    )
    assert nodes["photo_audit_score"]["config"]["factor_id"] == (
        "lab_" + AUDIT_RUN_ID.replace("-", "")
    )
    assert nodes["source_plus_shadow_audit"]["config"]["weights"] == {
        "source": 1.0,
        "shadow_audit": 0.0,
    }
    assert payload["outputs"]["final_node_id"] == "final_score"


def test_final_strategy_is_new_and_verifier_is_non_mutating() -> None:
    assert FINAL_EXPERIMENT_ID != SOURCE_EXPERIMENT_ID
    assert FINAL_RUN_ID != SOURCE_RUN_ID

    source = inspect.getsource(run)
    assert "save_experiment(" not in source
    assert "save_experiment_by_name" not in source
    assert "update_experiment" not in source

