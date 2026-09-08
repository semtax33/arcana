from __future__ import annotations

from datetime import date

import pytest

from api.model.backtest import (
    BacktestEquityCurvePoint,
    BacktestSummary,
    FactorBacktestResult,
)
from api.repository.factor_lab_query import validate_factor_lab_graph
from scripts.build_us_expectation_gap_recovery_cagr import (
    MAX_HIGH_52W_GAP_PCT,
    MAX_PRICE_TO_TARGET,
    OPERATING_COMPANY_GICS_SECTORS,
    RECOVERY_MODEL_NAME,
    RECOVERY_WEIGHTS,
    RecoveryBlendSpec,
    _blend_backtest_results,
    _position_count,
    _set_as_of,
    build_recovery_module_graph,
    selection_tuple,
)
from scripts.optimize_kr_pvgo_expectations_alpha import PortfolioSpec


def _node(graph, node_id):
    return next(node for node in graph.nodes if node.id == node_id)


def test_recovery_graph_is_pit_lagged_and_excludes_financials() -> None:
    graph = build_recovery_module_graph()
    assert graph.experiment.name == RECOVERY_MODEL_NAME
    assert graph.experiment.factor_data_mode == "point_in_time_snapshot"
    assert graph.experiment.rebalance.signal_lag_days == 1
    assert "60" not in OPERATING_COMPANY_GICS_SECTORS
    assert "UNMAPPED" in OPERATING_COMPANY_GICS_SECTORS
    assert graph.experiment.universe.sector_codes == list(
        OPERATING_COMPANY_GICS_SECTORS
    )


def test_recovery_graph_encodes_reset_quality_and_revision_economics() -> None:
    graph = build_recovery_module_graph()
    expected_factors = {
        "us_price_to_target_price",
        "high52w_gap_pct",
        "normalized_intangible_adjusted_pvgo_pct",
        "pvgo_pct",
        "intangible_adjusted_roe_spread_pct",
        "roiic_wacc_spread",
        "fcf_to_ev_yield",
        "sales_growth_1y",
        "us_eps_dispersion_pct",
        "us_eps_revision_30d_pct",
        "us_eps_revision_acceleration_30d_pct",
        "us_eps_revision_breadth_30d_pct",
        "us_eps_surprise_pct",
        "mcap_mil",
        "normalized_intangible_adjusted_earnings_5y",
    }
    factor_ids = {
        node.config["factor_id"]
        for node in graph.nodes
        if node.type == "factor_input"
    }
    assert factor_ids == expected_factors
    assert _node(graph, "target_gap_score").config["direction"] == "lower_better"
    assert _node(graph, "drawdown_score").config["direction"] == "lower_better"
    assert (
        _node(graph, "adjusted_roe_spread_score").config["direction"]
        == "higher_better"
    )
    assert _node(graph, "target_ratio_ceiling").config["value"] == pytest.approx(
        MAX_PRICE_TO_TARGET
    )
    assert _node(graph, "drawdown_ceiling").config["value"] == pytest.approx(
        MAX_HIGH_52W_GAP_PCT
    )
    assert _node(graph, "target_gap_present").type == "less_than"
    assert _node(graph, "deep_drawdown_present").type == "less_than"
    assert _node(graph, "positive_adjusted_roe_spread").type == "greater_than"
    assert _node(graph, "positive_fcf_yield").type == "greater_than"
    assert _node(graph, "positive_eps_revision").type == "greater_than"


def test_recovery_graph_validates_against_declared_factor_catalog() -> None:
    graph = build_recovery_module_graph()
    known = {
        node.config["factor_id"]
        for node in graph.nodes
        if node.type == "factor_input"
    }
    result = validate_factor_lab_graph(
        graph.model_dump(mode="json"),
        known_factor_ids=known,
    )
    assert result.valid, result.errors
    assert result.final_node_id == "module_rank_score"


def test_sleeves_are_combined_by_actual_capital_weight() -> None:
    dates = [date(2026, 1, 2), date(2026, 1, 5)]

    def result(navs):
        return FactorBacktestResult(
            summary=BacktestSummary(
                start_date=dates[0],
                end_date=dates[-1],
                rebalance_frequency="quarterly",
            ),
            equity_curve=[
                BacktestEquityCurvePoint(trade_date=value, strategy_nav=nav)
                for value, nav in zip(dates, navs, strict=True)
            ],
            rebalance_history=[],
            annual_returns=[],
        )

    blended = _blend_backtest_results(
        result([1.0, 1.10]),
        result([1.0, 0.80]),
        spec=RecoveryBlendSpec(recovery_weight=0.25),
        period=(dates[0], dates[-1]),
        recovery_portfolio=PortfolioSpec(top_percent=5.0, max_positions=20),
        cost_bps=50.0,
    )
    # 75% * +10% plus 25% * -20% = +2.5%.
    assert blended["metrics"]["cumulative_return"] == pytest.approx(0.025)
    assert blended["capital_weights"] == pytest.approx(
        {"anchor_weight": 0.75, "recovery_weight": 0.25}
    )


def test_recovery_weight_grid_never_silently_selects_a_zero_weight_sleeve() -> None:
    assert RECOVERY_WEIGHTS
    assert min(RECOVERY_WEIGHTS) > 0
    assert max(RECOVERY_WEIGHTS) < 1


def test_position_count_keeps_an_empty_sleeve_in_cash() -> None:
    portfolio = PortfolioSpec(top_percent=3.0, max_positions=10)
    assert _position_count(0, portfolio) == 0
    assert _position_count(42, portfolio) == 2


def test_selection_is_cagr_first_but_rejects_unrobust_segments() -> None:
    train = {"cagr": 0.20, "sharpe": 0.8, "max_drawdown": -0.30}
    validation = {"cagr": 0.10, "sharpe": 0.5, "max_drawdown": -0.40}
    pre_holdout = {"cagr": 0.16, "sharpe": 0.7, "max_drawdown": -0.40}
    ordering, feasible, failures = selection_tuple(
        train=train,
        validation=validation,
        pre_holdout=pre_holdout,
    )
    assert feasible
    assert not failures
    assert ordering[0] == pytest.approx(0.16)

    bad_validation = {**validation, "cagr": -0.01}
    _ordering, feasible, failures = selection_tuple(
        train=train,
        validation=bad_validation,
        pre_holdout=pre_holdout,
    )
    assert not feasible
    assert "validation CAGR is not positive" in failures


def test_screen_as_of_can_reach_september_without_changing_the_source_graph() -> None:
    graph = build_recovery_module_graph()
    updated = _set_as_of(graph, date(2026, 9, 8))
    assert updated.experiment.end_date == date(2026, 9, 8)
    assert graph.experiment.end_date == date(2026, 9, 4)
