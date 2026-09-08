from __future__ import annotations

import math
from datetime import date

import pytest

from scripts.build_us_soros_price_target_reflexivity_cagr import (
    CANDIDATES,
    TARGET_WEIGHTS,
    ReflexivitySpec,
    build_reflexivity_graph,
    build_target_price_module_graph,
    _set_screen_as_of,
    selection_tuple,
)


def _node(graph, node_id):
    return next(node for node in graph.nodes if node.id == node_id)


def test_target_modules_are_pit_and_test_both_economic_directions() -> None:
    gap = build_target_price_module_graph("target_gap")
    lead = build_target_price_module_graph("price_lead")
    for graph in (gap, lead):
        assert graph.experiment.factor_data_mode == "point_in_time_snapshot"
        assert graph.experiment.rebalance.signal_lag_days == 1
        source = _node(graph, "price_to_target_input")
        assert source.config["factor_id"] == "us_price_to_target_price"
        assert source.config["financial_basis"] == "ttm"
        assert source.config["missing_policy"] == "drop"
    assert _node(gap, "price_to_target_score").config["direction"] == "lower_better"
    assert _node(lead, "price_to_target_score").config["direction"] == "higher_better"


def test_screen_as_of_date_can_extend_beyond_the_history_backtest_end() -> None:
    graph = build_target_price_module_graph("target_gap")
    assert str(graph.experiment.end_date) == "2026-09-04"
    refreshed = _set_screen_as_of(graph, date(2026, 9, 8))
    assert str(refreshed.experiment.end_date) == "2026-09-08"
    assert str(graph.experiment.end_date) == "2026-09-04"


def test_candidate_grid_is_bounded_and_contains_control_additive_and_interactions() -> None:
    assert len(CANDIDATES) == 1 + 2 * (len(TARGET_WEIGHTS) + 1)
    assert CANDIDATES["matched_control"].style == "control"
    for direction in ("target_gap", "price_lead"):
        assert CANDIDATES[f"{direction}__interaction"].style == "interaction"
        for weight in TARGET_WEIGHTS:
            key = f"{direction}__w{int(weight * 100):02d}"
            assert CANDIDATES[key].target_weight == weight


def test_matched_control_requires_target_coverage_even_at_zero_weight() -> None:
    run_ids = {"target_gap": "2" * 32, "price_lead": "3" * 32}
    graph = build_reflexivity_graph(
        "control",
        CANDIDATES["matched_control"],
        base_run_id="1" * 32,
        target_run_ids=run_ids,
    )
    score = _node(graph, "reflexivity_score")
    assert score.type == "weighted_score"
    assert score.config["weights"] == {"base": 1.0, "target": 0.0}
    assert score.config["missing_weight_renormalize"] is False
    assert len([node for node in graph.nodes if node.type == "factor_input"]) == 2


def test_additive_and_interaction_graphs_encode_distinct_reflexivity_hypotheses() -> None:
    run_ids = {"target_gap": "2" * 32, "price_lead": "3" * 32}
    additive = build_reflexivity_graph(
        "additive",
        ReflexivitySpec("additive", "price_lead", 0.20),
        base_run_id="1" * 32,
        target_run_ids=run_ids,
    )
    interaction = build_reflexivity_graph(
        "interaction",
        ReflexivitySpec("interaction", "target_gap"),
        base_run_id="1" * 32,
        target_run_ids=run_ids,
    )
    assert _node(additive, "reflexivity_score").config["weights"] == pytest.approx(
        {"base": 0.80, "target": 0.20}
    )
    assert _node(interaction, "reflexivity_score").type == "mul"


def test_selection_is_literal_cagr_first_with_guardrails() -> None:
    train = {"cagr": 0.12, "sharpe": 0.7, "max_drawdown": -0.30}
    validation = {"cagr": 0.10, "sharpe": 0.6, "max_drawdown": -0.35}
    pre_holdout = {"cagr": 0.11, "sharpe": 0.65, "max_drawdown": -0.35}
    ordering, feasible, failures = selection_tuple(
        train=train,
        validation=validation,
        pre_holdout=pre_holdout,
    )
    faster, _, _ = selection_tuple(
        train=train,
        validation=validation,
        pre_holdout=dict(pre_holdout, cagr=0.15),
    )
    assert feasible
    assert failures == []
    assert math.isclose(ordering[0], 0.11)
    assert faster > ordering

    _, feasible, failures = selection_tuple(
        train=train,
        validation=dict(validation, sharpe=0.10),
        pre_holdout=pre_holdout,
    )
    assert not feasible
    assert failures == ["validation Sharpe is below 0.25"]


def test_screen_stability_run_count_is_bounded(tmp_path) -> None:
    from scripts.build_us_soros_price_target_reflexivity_cagr import (
        refresh_as_of_screen,
    )

    with pytest.raises(ValueError, match="between 1 and 3"):
        refresh_as_of_screen(
            tmp_path / "missing.json",
            as_of_date=date(2026, 9, 8),
            stability_runs=0,
        )
