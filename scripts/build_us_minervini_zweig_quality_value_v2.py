from __future__ import annotations

"""Build and verify the feedback-driven US Minervini/Zweig FactorLab V2.

The script clones the stored V1 experiment, so the original strategy remains
unchanged.  It then separates innovation from valuation, removes duplicated
momentum and solvency signals, and adds earnings-confidence, quality, capital
discipline, valuation, and drawdown controls.

Run from the project root with::

    python -m scripts.build_us_minervini_zweig_quality_value_v2
"""

import argparse
from copy import deepcopy
from datetime import date
import json
from typing import Any

from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService


BASE_NAME = (
    "Ungdroo_US_Minervini_Zweig_Innovation_Robust_v1_20260825_FactorLab"
)
MODEL_NAME = (
    "Ungdroo_US_Minervini_Zweig_Innovation_QualityValue_Quarterly_"
    "Robust_v2_20260825_FactorLab"
)
START_DATE = date(2022, 6, 30)
END_DATE = date(2026, 8, 21)
BACKTEST_END_DATE = date(2026, 7, 31)
REBALANCE_FREQUENCY = "quarterly"
TOP_PERCENT = 17.0
MAX_POSITIONS = 50
TRANSACTION_COST_BPS = 20.0


TREND_WEIGHTS = {
    "tr12": 0.25,
    "tr6": 0.20,
    "tr3": 0.10,
    "high52": 0.15,
    "riskmom": 0.20,
    "mdd": 0.10,
}
EARNINGS_WEIGHTS = {
    "epsyoy": 0.18,
    "salesg": 0.13,
    "epssurp": 0.13,
    "epsrev": 0.18,
    "epsrevacc": 0.13,
    "epsrevbreadth": 0.15,
    "epsdispersion": 0.10,
}
INNOVATION_WEIGHTS = {
    "iroe": 0.25,
    "rdmargin": 0.20,
    "roicspread": 0.20,
    "deltaep": 0.20,
    "spreadgrowth": 0.15,
}
QUALITY_CORE_WEIGHTS = {
    "grossprofit": 0.25,
    "opm": 0.20,
    "totalaccruals": 0.20,
    "fcfinterest": 0.20,
    "cashtodebt": 0.15,
}
CAPITAL_DISCIPLINE_WEIGHTS = {
    "assetgrowth": 0.35,
    "externalfinancing": 0.30,
    "inventorygrowth": 0.20,
    "capexgrowth": 0.15,
}
QUALITY_WEIGHTS = {"quality_core": 0.70, "capital_discipline": 0.30}
VALUATION_WEIGHTS = {
    "fcftoev": 0.35,
    "economicprofityield": 0.30,
    "evtonopat": 0.20,
    "rim": 0.15,
}
COMPOSITE_WEIGHTS = {
    "trend": 0.30,
    "earnings": 0.25,
    "innovation": 0.20,
    "quality": 0.15,
    "valuation": 0.10,
}


def _node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for item in graph["nodes"]:
        if item["id"] == node_id:
            return item
    raise KeyError(node_id)


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _remove_nodes(graph: dict[str, Any], *node_ids: str) -> None:
    removed = set(node_ids)
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] not in removed]
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if edge["source"] not in removed and edge["target"] not in removed
    ]


def _disconnect_target(graph: dict[str, Any], target: str) -> None:
    graph["edges"] = [edge for edge in graph["edges"] if edge["target"] != target]


def _add_factor_pipeline(
    graph: dict[str, Any],
    *,
    stem: str,
    factor_id: str,
    direction: str,
    y: float,
    missing_policy: str = "cross_sectional_median",
    financial_basis: str = "annual",
) -> str:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    score_id = f"{stem}_score"
    graph["nodes"].extend(
        [
            {
                "id": input_id,
                "type": "factor_input",
                "position": {"x": 60.0, "y": y},
                "config": {
                    "factor_id": factor_id,
                    "financial_basis": financial_basis,
                    "missing_policy": missing_policy,
                },
            },
            {
                "id": winsor_id,
                "type": "winsorize",
                "position": {"x": 290.0, "y": y},
                "config": {
                    "group_by": ["trade_date"],
                    "lower_quantile": 0.01,
                    "upper_quantile": 0.99,
                },
            },
            {
                "id": score_id,
                "type": "shrunk_zscore",
                "position": {"x": 520.0, "y": y},
                "config": {
                    "group_key": "sector",
                    "min_market_count": 20,
                    "min_group_count": 20,
                    "shrinkage_strength": 20,
                    "direction": direction,
                    "clip": 3.0,
                },
            },
        ]
    )
    graph["edges"].extend(
        [
            _edge(input_id, winsor_id, "input"),
            _edge(winsor_id, score_id, "input"),
        ]
    )
    return score_id


def _connect_weighted_score(
    graph: dict[str, Any],
    *,
    target: str,
    sources: dict[str, str],
    weights: dict[str, float],
    research_design: str,
    missing_weight_renormalize: bool = False,
) -> None:
    _disconnect_target(graph, target)
    target_node = _node(graph, target)
    target_node["type"] = "weighted_score"
    target_node["config"] = {
        "missing_weight_renormalize": missing_weight_renormalize,
        "research_design": research_design,
        "weights": weights,
    }
    graph["edges"].extend(
        [_edge(source, target, handle) for handle, source in sources.items()]
    )


def build_graph(base_graph: FactorLabGraphDto, name: str = MODEL_NAME) -> FactorLabGraphDto:
    graph = deepcopy(base_graph.model_dump(mode="json"))
    graph["experiment"].update(
        {
            "name": name,
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "factor_data_mode": "point_in_time_snapshot",
            "rebalance": {
                "frequency": REBALANCE_FREQUENCY,
                "signal_lag_days": 1,
                "transaction_cost_bps": TRANSACTION_COST_BPS,
            },
        }
    )

    # Remove duplicated momentum/solvency inputs and factors reassigned to new
    # economic roles.  RIM is retained and moved into the valuation block.
    _remove_nodes(
        graph,
        "kratio_input",
        "kratio_winsor",
        "kratio_score",
        "rpr_input",
        "rpr_winsor",
        "rpr_score",
        "accrual_input",
        "accrual_winsor",
        "accrual_score",
        "intcov_input",
        "intcov_winsor",
        "intcov_score",
        "icr_input",
        "icr_winsor",
        "icr_score",
        "intcov_floor",
        "intcov_ok",
        "icr_floor",
        "icr_ok",
        "solvency_ok",
    )

    tr3 = _add_factor_pipeline(
        graph,
        stem="tr3",
        factor_id="tr_3_1",
        direction="higher_better",
        y=2200.0,
        missing_policy="drop",
    )
    mdd = _add_factor_pipeline(
        graph,
        stem="mdd",
        factor_id="mdd1yr_12_1_pct",
        # The stored value is a negative drawdown (-100..0), so values closer
        # to zero are safer even though the catalog currently says LOWER_BETTER.
        direction="higher_better",
        y=2315.0,
        missing_policy="drop",
    )
    _connect_weighted_score(
        graph,
        target="trend_block",
        sources={
            "tr12": "tr12_score",
            "tr6": "tr6_score",
            "tr3": tr3,
            "high52": "high52_score",
            "riskmom": "riskmom_score",
            "mdd": mdd,
        },
        weights=TREND_WEIGHTS,
        research_design="trend_acceleration_with_drawdown_control",
    )

    breadth = _add_factor_pipeline(
        graph,
        stem="epsrevbreadth",
        factor_id="us_eps_revision_breadth_30d_pct",
        direction="higher_better",
        y=2430.0,
    )
    dispersion = _add_factor_pipeline(
        graph,
        stem="epsdispersion",
        factor_id="us_eps_dispersion_pct",
        direction="lower_better",
        y=2545.0,
    )
    _connect_weighted_score(
        graph,
        target="zweig_block",
        sources={
            "epsyoy": "epsyoy_score",
            "salesg": "salesg_score",
            "epssurp": "epssurp_score",
            "epsrev": "epsrev_score",
            "epsrevacc": "epsrevacc_score",
            "epsrevbreadth": breadth,
            "epsdispersion": dispersion,
        },
        weights=EARNINGS_WEIGHTS,
        research_design="revision_magnitude_breadth_and_dispersion_confidence",
        # Historical breadth can legitimately have a zero-variance cross
        # section even when revision magnitude, acceleration, dispersion, and
        # surprise are well populated.  Renormalize this block over the valid
        # precommitted inputs instead of invalidating the entire strategy.
        missing_weight_renormalize=True,
    )

    delta_ep = _add_factor_pipeline(
        graph,
        stem="deltaep",
        factor_id="delta_economic_profit",
        direction="higher_better",
        y=2660.0,
    )
    spread_growth = _add_factor_pipeline(
        graph,
        stem="spreadgrowth",
        factor_id="roic_wacc_spread_growth_1y",
        direction="higher_better",
        y=2775.0,
    )
    _connect_weighted_score(
        graph,
        target="innovation_block",
        sources={
            "iroe": "iroe_score",
            "rdmargin": "rdmargin_score",
            "roicspread": "roicspread_score",
            "deltaep": delta_ep,
            "spreadgrowth": spread_growth,
        },
        weights=INNOVATION_WEIGHTS,
        research_design="innovation_converted_to_economic_profit_growth",
    )

    gross_profit = _add_factor_pipeline(
        graph,
        stem="grossprofit",
        factor_id="gross_profitability_pct",
        direction="higher_better",
        y=2890.0,
    )
    total_accruals = _add_factor_pipeline(
        graph,
        stem="totalaccruals",
        factor_id="percent_total_accruals_pct",
        direction="lower_better",
        y=3005.0,
    )
    fcf_interest = _add_factor_pipeline(
        graph,
        stem="fcfinterest",
        factor_id="fcf_interest_coverage",
        direction="higher_better",
        y=3120.0,
        missing_policy="drop",
    )
    cash_to_debt = _add_factor_pipeline(
        graph,
        stem="cashtodebt",
        factor_id="cash_to_debt",
        direction="higher_better",
        y=3235.0,
    )
    asset_growth = _add_factor_pipeline(
        graph,
        stem="assetgrowth",
        factor_id="asset_yoy_pct",
        direction="lower_better",
        y=3350.0,
    )
    external_financing = _add_factor_pipeline(
        graph,
        stem="externalfinancing",
        factor_id="net_external_financing_pct",
        direction="lower_better",
        y=3465.0,
    )
    inventory_growth = _add_factor_pipeline(
        graph,
        stem="inventorygrowth",
        factor_id="inventory_growth_1y_pct",
        direction="lower_better",
        y=3580.0,
    )
    capex_growth = _add_factor_pipeline(
        graph,
        stem="capexgrowth",
        factor_id="capex_growth_2y_pct",
        direction="lower_better",
        y=3695.0,
    )
    graph["nodes"].extend(
        [
            {
                "id": "quality_core",
                "type": "weighted_score",
                "position": {"x": 780.0, "y": 3100.0},
                "config": {},
            },
            {
                "id": "capital_discipline",
                "type": "weighted_score",
                "position": {"x": 780.0, "y": 3520.0},
                "config": {},
            },
        ]
    )
    _connect_weighted_score(
        graph,
        target="quality_core",
        sources={
            "grossprofit": gross_profit,
            "opm": "opm_score",
            "totalaccruals": total_accruals,
            "fcfinterest": fcf_interest,
            "cashtodebt": cash_to_debt,
        },
        weights=QUALITY_CORE_WEIGHTS,
        research_design="cash_backed_multidimensional_quality",
    )
    _connect_weighted_score(
        graph,
        target="capital_discipline",
        sources={
            "assetgrowth": asset_growth,
            "externalfinancing": external_financing,
            "inventorygrowth": inventory_growth,
            "capexgrowth": capex_growth,
        },
        weights=CAPITAL_DISCIPLINE_WEIGHTS,
        research_design="capital_discipline_anti_overinvestment",
    )
    _connect_weighted_score(
        graph,
        target="quality_block",
        sources={
            "quality_core": "quality_core",
            "capital_discipline": "capital_discipline",
        },
        weights=QUALITY_WEIGHTS,
        research_design="quality_plus_capital_discipline",
    )

    fcf_to_ev = _add_factor_pipeline(
        graph,
        stem="fcftoev",
        factor_id="fcf_to_ev_yield",
        direction="higher_better",
        y=3810.0,
    )
    economic_profit_yield = _add_factor_pipeline(
        graph,
        stem="economicprofityield",
        factor_id="economic_profit_yield",
        direction="higher_better",
        y=3925.0,
    )
    ev_to_nopat = _add_factor_pipeline(
        graph,
        stem="evtonopat",
        factor_id="ev_to_nopat",
        direction="lower_better",
        y=4040.0,
    )
    graph["nodes"].append(
        {
            "id": "valuation_block",
            "type": "weighted_score",
            "position": {"x": 780.0, "y": 3930.0},
            "config": {},
        }
    )
    _connect_weighted_score(
        graph,
        target="valuation_block",
        sources={
            "fcftoev": fcf_to_ev,
            "economicprofityield": economic_profit_yield,
            "evtonopat": ev_to_nopat,
            "rim": "rim_score",
        },
        weights=VALUATION_WEIGHTS,
        research_design="innovation_compatible_valuation_sanity",
    )

    _connect_weighted_score(
        graph,
        target="composite_score",
        sources={
            "trend": "trend_block",
            "earnings": "zweig_block",
            "innovation": "innovation_block",
            "quality": "quality_block",
            "valuation": "valuation_block",
        },
        weights=COMPOSITE_WEIGHTS,
        research_design="five_block_feedback_v2_precommitted_weights",
    )

    # Replace two economically overlapping accounting-coverage gates with one
    # deliberately weak cash-based bankruptcy screen.
    graph["nodes"].extend(
        [
            {
                "id": "fcfinterest_floor",
                "type": "constant",
                "position": {"x": 1030.0, "y": 3810.0},
                "config": {"value": 0.0},
            },
            {
                "id": "fcfinterest_ok",
                "type": "greater_than",
                "position": {"x": 1250.0, "y": 3810.0},
                "config": {},
            },
        ]
    )
    _disconnect_target(graph, "risk_gate")
    graph["edges"].extend(
        [
            _edge("fcfinterest_input", "fcfinterest_ok", "left"),
            _edge("fcfinterest_floor", "fcfinterest_ok", "right"),
            _edge("fcfinterest_ok", "risk_gate", "left"),
            _edge("mcap_ok", "risk_gate", "right"),
        ]
    )

    final = _node(graph, "final_score")
    final["config"] = {
        "research_design": (
            "minervini_trend_gate_plus_revision_confidence_economic_profit_"
            "capital_discipline_and_valuation_sanity"
        ),
        "portfolio_hint": {
            "rebalance_frequency": REBALANCE_FREQUENCY,
            "top_percent": TOP_PERCENT,
            "max_positions": MAX_POSITIONS,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "microcap_percentile_floor": 20,
            "minimum_fcf_interest_coverage": 0,
        },
    }
    return FactorLabGraphDto(**graph)


def run(*, save_only: bool = False) -> dict[str, Any]:
    service = FactorLabService()
    base = service.get_experiment_by_name(BASE_NAME)
    graph = build_graph(base.graph)
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([issue.model_dump(mode="json") for issue in validation.errors])

    experiment = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    result: dict[str, Any] = {
        "base_name": BASE_NAME,
        "model_name": MODEL_NAME,
        "experiment_id": experiment.experiment_id,
        "graph_hash": validation.graph_hash,
        "validation_warnings": [
            issue.model_dump(mode="json") for issue in validation.warnings
        ],
        "weights": {
            "trend": TREND_WEIGHTS,
            "earnings": EARNINGS_WEIGHTS,
            "innovation": INNOVATION_WEIGHTS,
            "quality_core": QUALITY_CORE_WEIGHTS,
            "capital_discipline": CAPITAL_DISCIPLINE_WEIGHTS,
            "quality": QUALITY_WEIGHTS,
            "valuation": VALUATION_WEIGHTS,
            "composite": COMPOSITE_WEIGHTS,
        },
    }
    if save_only:
        return result

    history = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="history",
            history_start_date=START_DATE,
            history_end_date=BACKTEST_END_DATE,
            history_rebalance_frequency=REBALANCE_FREQUENCY,
        )
    )
    backtest = service.run_backtest(
        history.run_id,
        FactorLabBacktestRequestDto(
            top_percent=TOP_PERCENT,
            start_date=START_DATE,
            end_date=BACKTEST_END_DATE,
            rebalance_frequency=REBALANCE_FREQUENCY,
            market="US",
            benchmarks=[],
            max_positions=MAX_POSITIONS,
            transaction_cost_bps=TRANSACTION_COST_BPS,
        ),
    )
    screen = service.run_graph(
        FactorLabRunRequestDto(experiment_id=experiment.experiment_id, mode="screen")
    )
    result.update(
        {
            "history_run_id": history.run_id,
            "screen_run_id": screen.run_id,
            "history_quality": history.quality.model_dump(mode="json"),
            "metrics": vars(backtest.summary),
            "annual_returns": [vars(item) for item in backtest.annual_returns],
            "backtest_warnings": backtest.warnings,
            "screen_quality": screen.quality.model_dump(mode="json"),
            "screen_top25": [
                item.model_dump(mode="json") for item in screen.rows[:25]
            ],
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save-only",
        action="store_true",
        help="validate and save the cloned V2 graph without running history/backtest",
    )
    args = parser.parse_args()
    print(json.dumps(run(save_only=args.save_only), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
