from __future__ import annotations

import math

import pytest

from scripts.build_us_minervini_zweig_pvgo_cagr import (
    CANDIDATES,
    MINERVINI_WEIGHTS,
    PVGO_RELATIVE_WEIGHTS,
    ZWEIG_WEIGHTS,
    build_composite_graph,
    build_minervini_module_graph,
    build_pvgo_module_graph,
    build_zweig_module_graph,
    selection_score,
)


def _factor_ids(graph) -> set[str]:
    return {
        str(node.config.get("factor_id"))
        for node in graph.nodes
        if node.type == "factor_input"
    }


def test_module_weights_are_normalized_and_per_is_not_used() -> None:
    for weights in (MINERVINI_WEIGHTS, ZWEIG_WEIGHTS, PVGO_RELATIVE_WEIGHTS):
        assert math.isclose(sum(weights.values()), 1.0)
    graphs = (
        build_minervini_module_graph(),
        build_zweig_module_graph(),
        build_pvgo_module_graph(),
    )
    factor_ids = set().union(*(_factor_ids(graph) for graph in graphs))
    assert {"pvgo_gap_pct", "pvgo_compression_pct", "roiic_wacc_spread"} <= factor_ids
    assert "eps_yoy_pct" in factor_ids
    assert "tr_12_1" in factor_ids
    assert not any(
        factor_id in {"per", "pe", "pe_ratio", "earnings_yield"}
        for factor_id in factor_ids
    )


def test_modules_are_point_in_time_with_one_day_signal_lag() -> None:
    for graph in (
        build_minervini_module_graph(),
        build_zweig_module_graph(),
        build_pvgo_module_graph(),
    ):
        assert graph.experiment.market == "US"
        assert graph.experiment.factor_data_mode == "point_in_time_snapshot"
        assert graph.experiment.rebalance.frequency == "quarterly"
        assert graph.experiment.rebalance.signal_lag_days == 1


def test_minervini_module_contains_trend_template_and_zweig_has_revision_confirmation() -> None:
    minervini = build_minervini_module_graph()
    minervini_types = {node.id: node.type for node in minervini.nodes}
    assert minervini_types["ma50_above_ma150"] == "greater_than"
    assert minervini_types["ma150_above_ma200"] == "greater_than"
    assert minervini_types["within_25pct_of_high"] == "greater_than"
    assert minervini_types["eligible_minervini_score"] == "condition_score"

    zweig_ids = _factor_ids(build_zweig_module_graph())
    assert {
        "us_eps_surprise_pct",
        "us_eps_revision_30d_pct",
        "us_eps_revision_acceleration_30d_pct",
        "us_eps_dispersion_pct",
    } <= zweig_ids


def test_composite_uses_materialized_factorlab_modules_without_missing_renormalization() -> None:
    graph = build_composite_graph(
        "test",
        CANDIDATES["balanced"],
        module_run_ids={
            "pvgo": "00000000-0000-0000-0000-000000000001",
            "minervini": "00000000-0000-0000-0000-000000000002",
            "zweig": "00000000-0000-0000-0000-000000000003",
        },
    )
    assert graph.experiment.factor_data_mode == "raw"
    factor_ids = _factor_ids(graph)
    assert len(factor_ids) == 3
    assert all(factor_id.startswith("lab_") for factor_id in factor_ids)
    momentum = next(
        node for node in graph.nodes if node.id == "momentum_confirmation_score"
    )
    final = next(node for node in graph.nodes if node.id == "composite_score")
    assert momentum.config["weights"] == pytest.approx({
        "minervini": 4 / 7,
        "zweig": 3 / 7,
    })
    assert final.config["weights"] == pytest.approx({
        "pvgo": 0.30,
        "momentum_confirmation": 0.70,
    })
    assert momentum.config["missing_weight_renormalize"] is False
    assert final.config["missing_weight_renormalize"] is False


def test_selection_score_is_cagr_first_but_enforces_robustness_guardrails() -> None:
    train = {"cagr": 0.20, "sharpe": 1.0, "max_drawdown": -0.30}
    validation = {"cagr": 0.15, "sharpe": 0.8, "max_drawdown": -0.35}
    pre_holdout = {"cagr": 0.18, "sharpe": 0.9, "max_drawdown": -0.35}
    score, feasible, failures = selection_score(
        train=train,
        validation=validation,
        pre_holdout=pre_holdout,
    )
    faster = dict(pre_holdout, cagr=0.25)
    faster_score, _, _ = selection_score(
        train=train,
        validation=validation,
        pre_holdout=faster,
    )
    assert feasible
    assert failures == []
    assert faster_score > score

    weak_validation = dict(validation, sharpe=0.2)
    _, feasible, failures = selection_score(
        train=train,
        validation=weak_validation,
        pre_holdout=pre_holdout,
    )
    assert not feasible
    assert failures == ["validation Sharpe is below 0.40"]
