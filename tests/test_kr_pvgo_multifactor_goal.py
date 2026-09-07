from __future__ import annotations

import json
from datetime import date
from pathlib import Path


RESULT_PATH = Path(
    "deliverables/kr_pvgo_multifactor_strategy_20260906.json"
)


def _incoming(graph, target: str) -> dict[str, str]:
    return {
        edge.target_handle: edge.source
        for edge in graph.edges
        if edge.target == target
    }


def test_multifactor_graph_preserves_the_pvgo_core() -> None:
    from scripts.discover_kr_pvgo_multifactor_strategy import (
        CandidateSpec,
        SleeveSpec,
        build_graph,
    )

    graph = build_graph(
        "test_multifactor",
        CandidateSpec(
            core_weight=0.70,
            sleeves=(
                SleeveSpec(
                    key="trend_quality",
                    factor_id="k_ratio_3y",
                    weight=0.30,
                    direction="higher_better",
                    financial_basis="annual",
                ),
            ),
            beta_gate=None,
        ),
        start_date=date(2002, 4, 1),
        end_date=date(2026, 9, 4),
    )
    nodes = {node.id: node for node in graph.nodes}
    incoming = _incoming(graph, "multifactor_score")

    assert nodes["gap_input"].config["factor_id"] == "pvgo_gap_pct"
    assert nodes["roiic_input"].config["factor_id"] == "roiic_wacc_spread"
    assert (
        nodes["compression_input"].config["factor_id"]
        == "pvgo_compression_pct"
    )
    assert incoming["gap"] == "gap_sector_z"
    assert incoming["quality"] == "roiic_sector_z"
    assert incoming["compression"] == "compression_sector_z"
    assert incoming["trend_quality"] == "trend_quality_z"
    assert nodes["multifactor_score"].config["weights"] == {
        "gap": 0.315,
        "quality": 0.21,
        "compression": 0.175,
        "trend_quality": 0.30,
    }
    assert "beta_input" not in nodes


def test_multifactor_graph_can_apply_an_economic_size_floor() -> None:
    from scripts.discover_kr_pvgo_multifactor_strategy import (
        CandidateSpec,
        build_graph,
    )

    graph = build_graph(
        "test_size_floor",
        CandidateSpec(core_weight=1.0, min_market_cap_mil=1_000.0),
        start_date=date(2016, 1, 4),
        end_date=date(2026, 9, 4),
    )
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["market_cap_input"].config["factor_id"] == "mcap_mil"
    assert nodes["market_cap_floor"].config["value"] == 1_000.0
    assert nodes["market_cap_eligible"].type == "greater_than"


def test_multifactor_graph_can_require_every_declared_factor() -> None:
    from scripts.discover_kr_pvgo_multifactor_strategy import (
        CandidateSpec,
        build_graph,
    )

    graph = build_graph(
        "test_complete_case",
        CandidateSpec(core_weight=1.0, require_complete_factor_case=True),
        start_date=date(2016, 1, 4),
        end_date=date(2026, 9, 4),
    )
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["multifactor_score"].config["missing_weight_renormalize"] is False


def test_multifactor_graph_can_apply_a_positive_trend_gate() -> None:
    from scripts.discover_kr_pvgo_multifactor_strategy import (
        CandidateSpec,
        build_graph,
    )
    from scripts.optimize_kr_pvgo_expectations_alpha import GateSpec

    graph = build_graph(
        "test_trend_gate",
        CandidateSpec(
            core_weight=1.0,
            extra_gates=(GateSpec("tr_12_1", "greater_than", 0.0, "annual"),),
        ),
        start_date=date(2016, 1, 4),
        end_date=date(2026, 9, 4),
    )
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["hard_gate_1_input"].config["factor_id"] == "tr_12_1"
    assert nodes["hard_gate_1_threshold"].config["value"] == 0.0
    assert nodes["hard_gate_1_condition"].type == "greater_than"


def test_new_factor_lab_strategy_meets_the_requested_goal() -> None:
    assert RESULT_PATH.exists(), "new multifactor FactorLab result is missing"
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    final = payload["final"]

    assert final["experiment_id"]
    assert final["source_preservation"]["unchanged"] is True
    assert {
        "pvgo_gap_pct",
        "roiic_wacc_spread",
        "pvgo_compression_pct",
    }.issubset(set(final["factor_ids"]))

    evidence = final["goal_evidence_50bps"]
    for segment in ("train", "validation", "holdout"):
        assert evidence[segment]["metrics"]["sharpe"] > 1.0

    assert final["selected_on_pareto_front"] is True
    assert final["objective"] == (
        "maximize robust Sharpe, maximize CAGR, minimize absolute MDD"
    )
