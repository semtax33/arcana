from __future__ import annotations

from datetime import date


def _incoming(graph, target: str) -> dict[str, str]:
    return {
        edge.target_handle: edge.source
        for edge in graph.edges
        if edge.target == target
    }


def test_candidate_removes_raw_pvgo_double_count_and_adds_confirmation_sleeves() -> None:
    from scripts.optimize_kr_pvgo_expectations_alpha import (
        CandidateSpec,
        GateSpec,
        build_candidate_graph,
    )

    spec = CandidateSpec(
        weights={
            "gap": 0.30,
            "quality": 0.20,
            "compression": 0.15,
            "momentum": 0.20,
            "low_volatility": 0.15,
        },
        deduplicate_quality=True,
        include_momentum=True,
        include_low_volatility=True,
    )
    graph = build_candidate_graph(
        "test_candidate",
        spec,
        start_date=date(2016, 4, 1),
        end_date=date(2026, 8, 24),
    )
    nodes = {node.id: node for node in graph.nodes}
    incoming = _incoming(graph, "expectations_alpha")

    assert nodes["momentum_input"].config["factor_id"] == "risk_adj_mom"
    assert nodes["low_volatility_input"].config["factor_id"] == "vol_12_1_ann"
    assert incoming["quality"] == "roiic_sector_z"
    assert "raw" not in incoming
    assert "quality_sum" not in nodes
    assert abs(sum(nodes["expectations_alpha"].config["weights"].values()) - 1.0) < 1e-12


def test_candidate_places_economic_eligibility_before_final_rank() -> None:
    from scripts.optimize_kr_pvgo_expectations_alpha import (
        CandidateSpec,
        GateSpec,
        build_candidate_graph,
    )

    graph = build_candidate_graph(
        "test_gate",
        CandidateSpec(
            weights={"gap": 0.5, "quality": 0.3, "compression": 0.2},
            deduplicate_quality=True,
            min_market_cap_mil=100_000.0,
            require_positive_normalized_nopat=True,
            hard_gates=(
                GateSpec("tr_12_1", "greater_than", 0.0),
            ),
        ),
        start_date=date(2016, 4, 1),
        end_date=date(2026, 8, 24),
    )
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["market_cap_floor"].config["value"] == 100_000.0
    assert nodes["normalized_nopat_input"].config["factor_id"] == "normalized_nopat_5y"
    assert nodes["hard_gate_1_input"].config["factor_id"] == "tr_12_1"
    assert nodes["hard_gate_1_condition"].type == "greater_than"
    assert nodes["eligible_strategy_score"].type == "condition_score"
    assert _incoming(graph, "eligible_strategy_score") == {
        "condition": "eligibility_gate",
        "score": "expectations_alpha",
    }
    assert _incoming(graph, "final_rank_score")["input"] == "eligible_strategy_score"


def test_selection_score_rewards_all_three_requested_directions() -> None:
    from scripts.optimize_kr_pvgo_expectations_alpha import selection_score

    baseline = {
        "sharpe": 0.9,
        "cagr": 0.15,
        "max_drawdown": -0.40,
    }
    higher_sharpe = {**baseline, "sharpe": 1.1}
    higher_cagr = {**baseline, "cagr": 0.20}
    lower_drawdown = {**baseline, "max_drawdown": -0.25}

    assert selection_score(higher_sharpe, higher_sharpe) > selection_score(
        baseline, baseline
    )
    assert selection_score(higher_cagr, higher_cagr) > selection_score(
        baseline, baseline
    )
    assert selection_score(lower_drawdown, lower_drawdown) > selection_score(
        baseline, baseline
    )
