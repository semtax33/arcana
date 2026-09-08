from __future__ import annotations

"""Build and evaluate a U.S. expectation-gap recovery strategy.

The strategy keeps the already-frozen, CAGR-oriented Minervini/Zweig/PVGO
target-gap model as an anchor and adds a separate recovery sleeve.  The sleeve
is intentionally allowed to own high-quality companies whose prices are far
below their 52-week highs, so the trend template in the anchor cannot exclude
the very expectation resets that this experiment is meant to study.

Candidate recovery weights and portfolio concentration are selected only from
the train and validation windows.  The 2024-2026 holdout is opened once after
selection.  All raw recovery inputs use point-in-time factor snapshots and a
one-trading-day signal lag.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any

from api.model.backtest import BacktestEquityCurvePoint, FactorBacktestResult
from api.service.backtest_service import _annual_returns, _summary
from api.service.dto import (
    FactorLabBacktestRequestDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabRunRequestDto,
)
from api.service.factor_lab_service import FactorLabService, _nav_returns_by_date
from scripts.build_us_soros_price_target_reflexivity_cagr import (
    FINAL_MODEL_NAME as ANCHOR_MODEL_NAME,
    OUTPUT as ANCHOR_OUTPUT,
    ReflexivitySpec,
    _run_live_screen as _run_anchor_live_screen,
)
from scripts.optimize_kr_pvgo_expectations_alpha import (
    PortfolioSpec,
    _jsonable,
    _write_checkpoint,
)


MODEL_STEM = (
    "Arcana_US_ExpectationGap_DeepResetRecovery_CAGR_"
    "Quarterly_2017_2026_20260908"
)
RECOVERY_MODEL_NAME = MODEL_STEM + "__RecoveryModule"
FINAL_MODEL_NAME = MODEL_STEM + "__Final"
OUTPUT = Path("deliverables/us_expectation_gap_recovery_cagr_20260908.json")
STRATEGY_VERSION = 3

FULL = (date(2017, 10, 2), date(2026, 9, 4))
TRAIN = (date(2017, 10, 2), date(2021, 12, 31))
VALIDATION = (date(2022, 1, 3), date(2023, 12, 29))
PRE_HOLDOUT = (TRAIN[0], VALIDATION[1])
HOLDOUT = (date(2024, 1, 2), FULL[1])

SELECTION_COST_BPS = 50.0
COST_SENSITIVITY_BPS = (20.0, 50.0, 100.0)
MIN_MARKET_CAP_USD_MILLIONS = 1_000.0
MAX_PRICE_TO_TARGET = 1.0
MAX_HIGH_52W_GAP_PCT = -15.0
RECOVERY_WEIGHTS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
PORTFOLIOS = (
    PortfolioSpec(top_percent=3.0, max_positions=10),
    PortfolioSpec(top_percent=5.0, max_positions=20),
    PortfolioSpec(top_percent=10.0, max_positions=30),
)

# Financial companies need equity-PVGO and ROE-cost-of-equity economics, so
# they are deliberately outside this operating-company recovery graph.
OPERATING_COMPANY_GICS_SECTORS = (
    "10",
    "15",
    "20",
    "25",
    "30",
    "35",
    "45",
    "50",
    "55",
    # Keep unmapped U.S. issuers eligible for economic gates.  Autodesk is
    # currently UNMAPPED in issuer metadata despite being an operating company.
    "UNMAPPED",
)

ANCHOR_SPEC = ReflexivitySpec(
    style="additive",
    direction="target_gap",
    target_weight=0.25,
)
ANCHOR_PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)

RESET_WEIGHTS = {
    "target_gap": 0.40,
    "drawdown": 0.25,
    "adjusted_pvgo": 0.20,
    "raw_pvgo": 0.15,
}
QUALITY_WEIGHTS = {
    "adjusted_roe_spread": 0.30,
    "roiic_spread": 0.20,
    "fcf_yield": 0.25,
    "sales_growth": 0.15,
    "low_eps_dispersion": 0.10,
}
INFLECTION_WEIGHTS = {
    "revision_30d": 0.35,
    "revision_acceleration": 0.25,
    "revision_breadth": 0.20,
    "eps_surprise": 0.20,
}
RECOVERY_SCORE_WEIGHTS = {
    "reset": 0.45,
    "quality": 0.35,
    "inflection": 0.20,
}


@dataclass(frozen=True)
class RecoveryBlendSpec:
    recovery_weight: float

    @property
    def anchor_weight(self) -> float:
        return 1.0 - self.recovery_weight


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _experiment(name: str, *, factor_data_mode: str) -> dict[str, Any]:
    return {
        "name": name,
        "market": "US",
        "start_date": FULL[0].isoformat(),
        "end_date": FULL[1].isoformat(),
        "factor_data_mode": factor_data_mode,
        "snapshot_coverage_policy": "allow_missing_inputs",
        "universe": {
            "type": "market",
            "sector_codes": list(OPERATING_COMPANY_GICS_SECTORS),
            "industry_group_codes": [],
        },
        "rebalance": {
            "frequency": "quarterly",
            "signal_lag_days": 1,
            "transaction_cost_bps": SELECTION_COST_BPS,
        },
    }


def _factor_pipeline(
    *,
    stem: str,
    factor_id: str,
    direction: str,
    financial_basis: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    score_id = f"{stem}_score"
    return (
        [
            {
                "id": input_id,
                "type": "factor_input",
                "config": {
                    "factor_id": factor_id,
                    "financial_basis": financial_basis,
                    "missing_policy": "drop",
                },
            },
            {
                "id": winsor_id,
                "type": "winsorize",
                "config": {
                    "group_by": ["trade_date"],
                    "lower_quantile": 0.01,
                    "upper_quantile": 0.99,
                },
            },
            {
                "id": score_id,
                "type": "shrunk_zscore",
                "config": {
                    "group_key": "industry_group",
                    "min_market_count": 20,
                    "min_group_count": 20,
                    "shrinkage_strength": 20,
                    "direction": direction,
                    "clip": 3.0,
                },
            },
        ],
        [
            _edge(input_id, winsor_id, "input"),
            _edge(winsor_id, score_id, "input"),
        ],
        score_id,
    )


def _binary_condition(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    node_id: str,
    left: str,
    right: str,
    operator: str,
    research_design: str,
) -> str:
    nodes.append(
        {
            "id": node_id,
            "type": operator,
            "config": {"research_design": research_design},
        }
    )
    edges.extend(
        [
            _edge(left, node_id, "left"),
            _edge(right, node_id, "right"),
        ]
    )
    return node_id


def _and_chain(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    conditions: list[str],
) -> str:
    if len(conditions) < 2:
        raise ValueError("an AND chain requires at least two conditions")
    result = conditions[0]
    for index, condition in enumerate(conditions[1:], start=1):
        node_id = f"eligibility_and_{index}"
        nodes.append(
            {
                "id": node_id,
                "type": "and",
                "config": {
                    "research_design": "all deep-reset economic gates must pass"
                },
            }
        )
        edges.extend(
            [
                _edge(result, node_id, "left"),
                _edge(condition, node_id, "right"),
            ]
        )
        result = node_id
    return result


def build_recovery_module_graph(
    name: str = RECOVERY_MODEL_NAME,
) -> FactorLabGraphDto:
    """Build the PIT deep-reset recovery sleeve.

    The hard gates encode the investment thesis directly: price and target
    expectations have reset, cash economics and intangible-adjusted earning
    power remain positive, and EPS estimates have stopped deteriorating.
    """

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    pipelines = {
        "target_gap": (
            "us_price_to_target_price",
            "lower_better",
            "annual",
        ),
        "drawdown": ("high52w_gap_pct", "lower_better", "annual"),
        "adjusted_pvgo": (
            "normalized_intangible_adjusted_pvgo_pct",
            "lower_better",
            "ttm",
        ),
        "raw_pvgo": ("pvgo_pct", "lower_better", "ttm"),
        "adjusted_roe_spread": (
            "intangible_adjusted_roe_spread_pct",
            "higher_better",
            "ttm",
        ),
        "roiic_spread": ("roiic_wacc_spread", "higher_better", "ttm"),
        "fcf_yield": ("fcf_to_ev_yield", "higher_better", "ttm"),
        "sales_growth": ("sales_growth_1y", "higher_better", "ttm"),
        "low_eps_dispersion": (
            "us_eps_dispersion_pct",
            "lower_better",
            "annual",
        ),
        "revision_30d": (
            "us_eps_revision_30d_pct",
            "higher_better",
            "annual",
        ),
        "revision_acceleration": (
            "us_eps_revision_acceleration_30d_pct",
            "higher_better",
            "annual",
        ),
        "revision_breadth": (
            "us_eps_revision_breadth_30d_pct",
            "higher_better",
            "annual",
        ),
        "eps_surprise": ("us_eps_surprise_pct", "higher_better", "annual"),
    }
    outputs: dict[str, str] = {}
    for stem, (factor_id, direction, basis) in pipelines.items():
        extra_nodes, extra_edges, output = _factor_pipeline(
            stem=stem,
            factor_id=factor_id,
            direction=direction,
            financial_basis=basis,
        )
        nodes.extend(extra_nodes)
        edges.extend(extra_edges)
        outputs[stem] = output

    nodes.extend(
        [
            {
                "id": "reset_score",
                "type": "weighted_score",
                "config": {
                    "weights": dict(RESET_WEIGHTS),
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "analyst target gap, price capitulation, and low market-implied "
                        "PVGO measure the expectations reset"
                    ),
                },
            },
            {
                "id": "quality_score",
                "type": "weighted_score",
                "config": {
                    "weights": dict(QUALITY_WEIGHTS),
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "intangible-adjusted returns, incremental returns, cash yield, "
                        "and sales growth reject low-quality value traps"
                    ),
                },
            },
            {
                "id": "inflection_score",
                "type": "weighted_score",
                "config": {
                    "weights": dict(INFLECTION_WEIGHTS),
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "PIT EPS revisions, acceleration, breadth, and surprise measure "
                        "bad-but-less-bad fundamental change"
                    ),
                },
            },
            {
                "id": "recovery_score",
                "type": "weighted_score",
                "config": {
                    "weights": dict(RECOVERY_SCORE_WEIGHTS),
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "deep expectations reset plus durable economics and an earnings "
                        "inflection; this is not a standalone drawdown score"
                    ),
                },
            },
        ]
    )
    edges.extend(
        [
            *[
                _edge(outputs[handle], "reset_score", handle)
                for handle in RESET_WEIGHTS
            ],
            *[
                _edge(outputs[handle], "quality_score", handle)
                for handle in QUALITY_WEIGHTS
            ],
            *[
                _edge(outputs[handle], "inflection_score", handle)
                for handle in INFLECTION_WEIGHTS
            ],
            _edge("reset_score", "recovery_score", "reset"),
            _edge("quality_score", "recovery_score", "quality"),
            _edge("inflection_score", "recovery_score", "inflection"),
        ]
    )

    # Gate inputs are deliberately separate from their standardized scores so
    # the economic requirements remain interpretable in native units.
    gate_inputs = {
        "market_cap": ("mcap_mil", "annual"),
        "normalized_adjusted_earnings": (
            "normalized_intangible_adjusted_earnings_5y",
            "ttm",
        ),
    }
    for stem, (factor_id, basis) in gate_inputs.items():
        nodes.append(
            {
                "id": f"{stem}_input",
                "type": "factor_input",
                "config": {
                    "factor_id": factor_id,
                    "financial_basis": basis,
                    "missing_policy": "drop",
                },
            }
        )

    constants = {
        "market_cap_floor": MIN_MARKET_CAP_USD_MILLIONS,
        "target_ratio_ceiling": MAX_PRICE_TO_TARGET,
        "drawdown_ceiling": MAX_HIGH_52W_GAP_PCT,
        "zero_floor": 0.0,
    }
    nodes.extend(
        {
            "id": node_id,
            "type": "constant",
            "config": {"value": value},
        }
        for node_id, value in constants.items()
    )

    conditions = [
        _binary_condition(
            nodes,
            edges,
            node_id="market_cap_eligible",
            left="market_cap_input",
            right="market_cap_floor",
            operator="greater_than",
            research_design="exclude microcaps below USD 1 billion",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="target_gap_present",
            left="target_gap_input",
            right="target_ratio_ceiling",
            operator="less_than",
            research_design="price remains below the fresh point-in-time target price",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="deep_drawdown_present",
            left="drawdown_input",
            right="drawdown_ceiling",
            operator="less_than",
            research_design="price is at least 15 percent below its 52-week high",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="positive_normalized_adjusted_earnings",
            left="normalized_adjusted_earnings_input",
            right="zero_floor",
            operator="greater_than",
            research_design="normalized intangible-adjusted earning power is positive",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="positive_adjusted_roe_spread",
            left="adjusted_roe_spread_input",
            right="zero_floor",
            operator="greater_than",
            research_design="intangible-adjusted ROE exceeds the cost of equity",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="positive_fcf_yield",
            left="fcf_yield_input",
            right="zero_floor",
            operator="greater_than",
            research_design="enterprise value is supported by positive free cash flow",
        ),
        _binary_condition(
            nodes,
            edges,
            node_id="positive_eps_revision",
            left="revision_30d_input",
            right="zero_floor",
            operator="greater_than",
            research_design="the latest 30-day EPS revision is positive",
        ),
    ]
    eligibility = _and_chain(nodes, edges, conditions)
    nodes.extend(
        [
            {
                "id": "eligible_recovery_score",
                "type": "condition_score",
                "config": {
                    "research_design": (
                        "deep-reset economic gates are applied before cross-sectional ranking"
                    )
                },
            },
            {
                "id": "module_rank_score",
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                    "semantic_label": "cross_sectional_rank_not_probability",
                },
            },
        ]
    )
    edges.extend(
        [
            _edge(eligibility, "eligible_recovery_score", "condition"),
            _edge("recovery_score", "eligible_recovery_score", "score"),
            _edge("eligible_recovery_score", "module_rank_score", "input"),
        ]
    )
    return FactorLabGraphDto(
        version=1,
        experiment=_experiment(name, factor_data_mode="point_in_time_snapshot"),
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "module_rank_score"},
    )


def selection_tuple(
    *,
    train: dict[str, Any],
    validation: dict[str, Any],
    pre_holdout: dict[str, Any],
) -> tuple[tuple[float, float, float, float], bool, list[str]]:
    """CAGR-first ordering with non-negotiable economic risk guardrails."""

    segments = {"train": train, "validation": validation}
    failures: list[str] = []
    for label, metrics in segments.items():
        cagr = metrics.get("cagr")
        sharpe = metrics.get("sharpe")
        max_drawdown = metrics.get("max_drawdown")
        if cagr is None or float(cagr) <= 0.0:
            failures.append(f"{label} CAGR is not positive")
        if sharpe is None or float(sharpe) < 0.25:
            failures.append(f"{label} Sharpe is below 0.25")
        if max_drawdown is None or abs(float(max_drawdown)) > 0.55:
            failures.append(f"{label} absolute MDD exceeds 55%")
    worst_cagr = min(float(value.get("cagr") or -1.0) for value in segments.values())
    worst_sharpe = min(
        float(value.get("sharpe") or -1.0) for value in segments.values()
    )
    worst_mdd = max(
        abs(float(value.get("max_drawdown") or -1.0))
        for value in segments.values()
    )
    ordering = (
        float(pre_holdout.get("cagr") or -1.0),
        worst_cagr,
        worst_sharpe,
        -worst_mdd,
    )
    return ordering, not failures, failures


def _set_as_of(graph: FactorLabGraphDto, as_of_date: date) -> FactorLabGraphDto:
    payload = graph.model_dump(mode="json")
    payload["experiment"]["end_date"] = as_of_date.isoformat()
    return FactorLabGraphDto(**payload)


def _run_history(
    service: FactorLabService,
    graph: FactorLabGraphDto,
) -> tuple[str, Any]:
    validation = service.validate_graph(graph)
    if not validation.valid:
        raise RuntimeError([_jsonable(value) for value in validation.errors])
    saved = service.save_experiment_by_name(
        FactorLabExperimentSaveRequestDto(graph=graph)
    )
    run = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=saved.experiment_id,
            mode="history",
            history_start_date=FULL[0],
            history_end_date=FULL[1],
            history_rebalance_frequency="quarterly",
        )
    )
    if run.status != "completed":
        raise RuntimeError(f"FactorLab history run failed: {run.status}")
    return saved.experiment_id, run


def _backtest_result(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    cost_bps: float,
) -> FactorBacktestResult:
    return service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=portfolio.top_percent,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
            market="US",
            benchmarks=["US_NASDAQ", "US_SP500"],
            max_positions=portfolio.max_positions,
            transaction_cost_bps=cost_bps,
        ),
    )


def _serialize_backtest(
    result: FactorBacktestResult,
    *,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    cost_bps: float,
) -> dict[str, Any]:
    return {
        "period": [str(value) for value in period],
        "portfolio": asdict(portfolio),
        "transaction_cost_bps": cost_bps,
        "metrics": _jsonable(result.summary),
        "annual_returns": [_jsonable(value) for value in result.annual_returns],
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    cost_bps: float,
) -> dict[str, Any]:
    return _serialize_backtest(
        _backtest_result(
            service,
            run_id,
            period=period,
            portfolio=portfolio,
            cost_bps=cost_bps,
        ),
        period=period,
        portfolio=portfolio,
        cost_bps=cost_bps,
    )


def _blend_backtest_results(
    anchor: FactorBacktestResult,
    recovery: FactorBacktestResult,
    *,
    spec: RecoveryBlendSpec,
    period: tuple[date, date],
    recovery_portfolio: PortfolioSpec,
    cost_bps: float,
) -> dict[str, Any]:
    """Blend independently rebalanced sleeves by daily capital-weighted returns.

    Keeping the sleeves separate is necessary because the eligible security sets
    are nearly disjoint.  Renormalizing a single cross-sectional score would erase
    the requested capital weight whenever one module is missing for a security.
    """

    anchor_dates = [point.trade_date for point in anchor.equity_curve]
    recovery_dates = [point.trade_date for point in recovery.equity_curve]
    if anchor_dates != recovery_dates:
        raise ValueError("anchor and recovery backtests must share a trading calendar")
    if not anchor_dates:
        raise ValueError("sleeve backtests produced no equity curve")

    anchor_returns = _nav_returns_by_date(anchor.equity_curve)
    recovery_returns = _nav_returns_by_date(recovery.equity_curve)
    anchor_points = {point.trade_date: point for point in anchor.equity_curve}
    nav = 1.0
    points: list[BacktestEquityCurvePoint] = []
    for trade_date in anchor_dates:
        daily_return = (
            spec.anchor_weight * anchor_returns.get(trade_date, 0.0)
            + spec.recovery_weight * recovery_returns.get(trade_date, 0.0)
        )
        nav *= 1.0 + daily_return
        points.append(
            BacktestEquityCurvePoint(
                trade_date=trade_date,
                strategy_nav=nav,
                benchmark_navs=dict(anchor_points[trade_date].benchmark_navs),
            )
        )

    rebalance_dates = {
        value.rebalance_date
        for value in [*anchor.rebalance_history, *recovery.rebalance_history]
    }
    summary = _summary(
        points,
        start_date=period[0],
        end_date=period[1],
        rebalance_frequency="quarterly",
        rebalance_count=len(rebalance_dates),
    )
    benchmark_ids = sorted(
        {
            benchmark_id
            for point in points
            for benchmark_id in point.benchmark_navs
        }
    )
    warnings = list(dict.fromkeys([*anchor.warnings, *recovery.warnings]))
    warnings.append(
        "Combined independently rebalanced Factor Lab sleeves using fixed daily "
        f"capital weights: anchor={spec.anchor_weight:.2%}, "
        f"recovery={spec.recovery_weight:.2%}."
    )
    return {
        "period": [str(value) for value in period],
        "capital_weights": asdict(spec) | {"anchor_weight": spec.anchor_weight},
        "anchor_portfolio": asdict(ANCHOR_PORTFOLIO),
        "recovery_portfolio": asdict(recovery_portfolio),
        "transaction_cost_bps_per_sleeve": cost_bps,
        "metrics": _jsonable(summary),
        "annual_returns": [
            _jsonable(value) for value in _annual_returns(points, benchmark_ids)
        ],
        "rebalance_count": len(rebalance_dates),
        "module_metrics": {
            "anchor": _jsonable(anchor.summary),
            "recovery": _jsonable(recovery.summary),
        },
        "warnings": warnings,
    }


def _completed_run(payload: dict[str, Any], key: str) -> str | None:
    value = (payload.get("modules") or {}).get(key) or {}
    if value.get("status") != "completed":
        return None
    run_id = str(value.get("run_id") or "")
    return run_id or None


def _anchor_history_run(service: FactorLabService) -> tuple[str, Any]:
    if ANCHOR_OUTPUT.exists():
        payload = json.loads(ANCHOR_OUTPUT.read_text(encoding="utf-8"))
        final = payload.get("final") or {}
        run_id = str(final.get("history_run_id") or "")
        experiment_id = str(final.get("experiment_id") or "")
        if run_id and experiment_id:
            try:
                run = service.get_run(run_id)
                if run.status == "completed":
                    return experiment_id, run
            except Exception:
                pass
    experiment = service.get_experiment_by_name(ANCHOR_MODEL_NAME)
    run = service.run_graph(
        FactorLabRunRequestDto(
            experiment_id=experiment.experiment_id,
            mode="history",
            history_start_date=FULL[0],
            history_end_date=FULL[1],
            history_rebalance_frequency="quarterly",
        )
    )
    if run.status != "completed":
        raise RuntimeError(f"anchor history run failed: {run.status}")
    return experiment.experiment_id, run


def _screen_graph(
    service: FactorLabService,
    graph: FactorLabGraphDto,
    *,
    as_of_date: date,
) -> Any:
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=_set_as_of(graph, as_of_date),
            mode="screen",
        )
    )


def _position_count(eligible_count: int, portfolio: PortfolioSpec) -> int:
    if eligible_count <= 0:
        return 0
    return min(
        portfolio.max_positions,
        max(1, math.ceil(eligible_count * portfolio.top_percent / 100.0)),
    )


def run_live_screen(
    service: FactorLabService,
    *,
    spec: RecoveryBlendSpec,
    portfolio: PortfolioSpec,
    as_of_date: date,
) -> dict[str, Any]:
    anchor = _run_anchor_live_screen(
        service,
        selected_spec=ANCHOR_SPEC,
        selected_portfolio=ANCHOR_PORTFOLIO,
        as_of_date=as_of_date,
    )
    recovery = _screen_graph(
        service,
        build_recovery_module_graph(MODEL_STEM + "__LiveRecoveryModule"),
        as_of_date=as_of_date,
    )
    recovery_effective_date = recovery.quality.date_coverage.get("max")
    recovery_eligible_count = int(recovery.quality.security_coverage)
    recovery_count = _position_count(recovery_eligible_count, portfolio)
    recovery_positions = [_jsonable(row) for row in recovery.rows[:recovery_count]]
    anchor_positions = list(anchor.get("selected_positions") or [])

    allocations: dict[str, dict[str, Any]] = {}
    sleeves = (
        ("anchor", spec.anchor_weight, anchor_positions),
        ("recovery", spec.recovery_weight, recovery_positions),
    )
    for sleeve, capital_weight, rows in sleeves:
        if not rows:
            continue
        position_weight = capital_weight / len(rows)
        for row in rows:
            security_id = str(row["security_id"])
            current = allocations.setdefault(
                security_id,
                {
                    "security_id": security_id,
                    "ticker": row.get("ticker"),
                    "stock_name": row.get("stock_name"),
                    "capital_weight": 0.0,
                    "sleeves": [],
                    "sleeve_scores": {},
                },
            )
            current["capital_weight"] += position_weight
            current["sleeves"].append(sleeve)
            current["sleeve_scores"][sleeve] = row.get("score")
    selected_positions = sorted(
        allocations.values(),
        key=lambda row: (-float(row["capital_weight"]), str(row.get("ticker") or "")),
    )
    allocated_weight = sum(float(row["capital_weight"]) for row in selected_positions)
    return {
        "classification": "on_demand_two_sleeve_expectation_gap_screen",
        "requested_as_of_date": str(as_of_date),
        "effective_trade_date": (
            str(recovery_effective_date) if recovery_effective_date else None
        ),
        "status": (
            "completed"
            if anchor.get("status") == "completed" and recovery.status == "completed"
            else "failed"
        ),
        "capital_allocation": {
            "anchor_weight": spec.anchor_weight,
            "recovery_weight": spec.recovery_weight,
            "allocated_weight": allocated_weight,
            "cash_weight": max(0.0, 1.0 - allocated_weight),
        },
        "recovery_portfolio": asdict(portfolio),
        "selected_position_count": len(selected_positions),
        "anchor": anchor,
        "recovery_module": {
            "run_id": recovery.run_id,
            "factor_id": recovery.factor_id,
            "status": recovery.status,
            "quality": _jsonable(recovery.quality),
            "eligible_row_count": recovery_eligible_count,
            "selected_position_count": recovery_count,
            "top20": [_jsonable(row) for row in recovery.rows[:20]],
            "selected_positions": recovery_positions,
        },
        "selected_positions": selected_positions,
        "note": (
            "The two independently ranked Factor Lab sleeves are combined by "
            "capital weight; missing cross-sectional scores are never renormalized."
        ),
    }


def run(
    output_path: Path = OUTPUT,
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    service = FactorLabService()
    payload: dict[str, Any] = {
        "status": "researching",
        "design": {
            "strategy_version": STRATEGY_VERSION,
            "anchor_model": ANCHOR_MODEL_NAME,
            "thesis": (
                "trend-confirmed expectation gap plus a deep-reset recovery sleeve "
                "requiring positive intangible-adjusted economics, FCF, and EPS revision"
            ),
            "full": [str(value) for value in FULL],
            "train": [str(value) for value in TRAIN],
            "validation": [str(value) for value in VALIDATION],
            "pre_holdout": [str(value) for value in PRE_HOLDOUT],
            "holdout": [str(value) for value in HOLDOUT],
            "holdout_policy": "excluded from recovery weight and portfolio selection",
            "selection_rule": (
                "maximize pre-holdout CAGR after positive-CAGR, Sharpe, and MDD "
                "guardrails; tie-break by worst segment CAGR, Sharpe, and MDD"
            ),
            "selection_cost_bps": SELECTION_COST_BPS,
            "recovery_weight_grid": list(RECOVERY_WEIGHTS),
            "portfolios": [asdict(value) for value in PORTFOLIOS],
            "signal_lag_days": 1,
            "factor_data_mode": "point_in_time_snapshot for every recovery input",
            "implementation": (
                "independently ranked Factor Lab sleeves combined by fixed capital "
                "weights at the daily-return level"
            ),
        },
        "modules": {},
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        previous_version = (previous.get("design") or {}).get("strategy_version")
        if previous_version == STRATEGY_VERSION:
            payload["modules"].update(previous.get("modules") or {})
            payload["candidates"].update(previous.get("candidates") or {})
        else:
            anchor_module = (previous.get("modules") or {}).get("anchor")
            if anchor_module:
                payload["modules"]["anchor"] = anchor_module

    anchor_run_id = _completed_run(payload, "anchor")
    if anchor_run_id:
        try:
            anchor_run = service.get_run(anchor_run_id)
            if anchor_run.status != "completed":
                anchor_run_id = None
        except Exception:
            anchor_run_id = None
    if not anchor_run_id:
        anchor_experiment_id, anchor_run = _anchor_history_run(service)
        anchor_run_id = anchor_run.run_id
        payload["modules"]["anchor"] = {
            "status": "completed",
            "experiment_id": anchor_experiment_id,
            "run_id": anchor_run_id,
            "graph_hash": anchor_run.graph_hash,
            "quality": _jsonable(anchor_run.quality),
        }
        _write_checkpoint(output_path, payload)

    recovery_run_id = _completed_run(payload, "recovery")
    if recovery_run_id:
        try:
            recovery_run = service.get_run(recovery_run_id)
            if recovery_run.status != "completed":
                recovery_run_id = None
        except Exception:
            recovery_run_id = None
    if not recovery_run_id:
        recovery_experiment_id, recovery_run = _run_history(
            service,
            build_recovery_module_graph(),
        )
        recovery_run_id = recovery_run.run_id
        payload["modules"]["recovery"] = {
            "status": "completed",
            "experiment_id": recovery_experiment_id,
            "run_id": recovery_run_id,
            "graph_hash": recovery_run.graph_hash,
            "quality": _jsonable(recovery_run.quality),
        }
        _write_checkpoint(output_path, payload)

    periods = {
        "train": TRAIN,
        "validation": VALIDATION,
        "pre_holdout": PRE_HOLDOUT,
    }
    anchor_backtests = {
        label: _backtest_result(
            service,
            anchor_run_id,
            period=period,
            portfolio=ANCHOR_PORTFOLIO,
            cost_bps=SELECTION_COST_BPS,
        )
        for label, period in periods.items()
    }
    ranking: list[dict[str, Any]] = []
    for portfolio in PORTFOLIOS:
        recovery_backtests = {
            label: _backtest_result(
                service,
                recovery_run_id,
                period=period,
                portfolio=portfolio,
                cost_bps=SELECTION_COST_BPS,
            )
            for label, period in periods.items()
        }
        for recovery_weight in RECOVERY_WEIGHTS:
            key = (
                f"recovery_w{int(recovery_weight * 100):02d}"
                f"__top{portfolio.top_percent:g}__max{portfolio.max_positions}"
            )
            cached = payload["candidates"].get(key) or {}
            if cached.get("status") == "completed":
                ranking.append(cached["selection_row"])
                continue
            spec = RecoveryBlendSpec(recovery_weight=recovery_weight)
            blended = {
                label: _blend_backtest_results(
                    anchor_backtests[label],
                    recovery_backtests[label],
                    spec=spec,
                    period=period,
                    recovery_portfolio=portfolio,
                    cost_bps=SELECTION_COST_BPS,
                )
                for label, period in periods.items()
            }
            ordering, feasible, failures = selection_tuple(
                train=blended["train"]["metrics"],
                validation=blended["validation"]["metrics"],
                pre_holdout=blended["pre_holdout"]["metrics"],
            )
            row = {
                "key": key,
                "spec": asdict(spec),
                "portfolio": asdict(portfolio),
                "selection_ordering": list(ordering),
                "feasible": feasible,
                "guardrail_failures": failures,
                **blended,
            }
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "selection_row": row,
            }
        _write_checkpoint(output_path, payload)

    feasible = [row for row in ranking if row["feasible"]]
    selection_pool = feasible or ranking
    if not selection_pool:
        raise RuntimeError("no recovery candidate/portfolio combinations completed")
    selected = max(
        selection_pool,
        key=lambda row: tuple(row["selection_ordering"]),
    )
    selected_spec = RecoveryBlendSpec(**selected["spec"])
    selected_portfolio = PortfolioSpec(**selected["portfolio"])
    def blended_for(period: tuple[date, date], cost_bps: float) -> dict[str, Any]:
        anchor_result = _backtest_result(
            service,
            anchor_run_id,
            period=period,
            portfolio=ANCHOR_PORTFOLIO,
            cost_bps=cost_bps,
        )
        recovery_result = _backtest_result(
            service,
            recovery_run_id,
            period=period,
            portfolio=selected_portfolio,
            cost_bps=cost_bps,
        )
        return _blend_backtest_results(
            anchor_result,
            recovery_result,
            spec=selected_spec,
            period=period,
            recovery_portfolio=selected_portfolio,
            cost_bps=cost_bps,
        )

    full = blended_for(FULL, SELECTION_COST_BPS)
    holdout = blended_for(HOLDOUT, SELECTION_COST_BPS)
    costs = {
        f"{cost:g}": blended_for(FULL, cost)
        for cost in COST_SENSITIVITY_BPS
    }
    anchor_control = {
        "full_50bps": _backtest(
            service,
            anchor_run_id,
            period=FULL,
            portfolio=ANCHOR_PORTFOLIO,
            cost_bps=SELECTION_COST_BPS,
        ),
        "holdout_50bps": _backtest(
            service,
            anchor_run_id,
            period=HOLDOUT,
            portfolio=ANCHOR_PORTFOLIO,
            cost_bps=SELECTION_COST_BPS,
        ),
    }
    live = run_live_screen(
        service,
        spec=selected_spec,
        portfolio=selected_portfolio,
        as_of_date=as_of_date or FULL[1],
    )
    payload.update(
        {
            "status": "completed",
            "selection": selected,
            "final": {
                "model_name": FINAL_MODEL_NAME,
                "implementation": "two_factor_lab_sleeves_fixed_capital_weight",
                "module_experiment_ids": {
                    "anchor": payload["modules"]["anchor"]["experiment_id"],
                    "recovery": payload["modules"]["recovery"]["experiment_id"],
                },
                "module_history_run_ids": {
                    "anchor": anchor_run_id,
                    "recovery": recovery_run_id,
                },
                "spec": asdict(selected_spec),
                "anchor_portfolio": asdict(ANCHOR_PORTFOLIO),
                "recovery_portfolio": asdict(selected_portfolio),
                "full_50bps": full,
                "holdout_50bps": holdout,
                "cost_sensitivity": costs,
                "anchor_only_control": anchor_control,
                "live_screen": live,
            },
            "limitations": [
                "Capital weights are held fixed between quarterly sleeve rebalances; overlap adds the two position weights.",
                "Target prices are analyst opinions and can lag market prices.",
                "The finite grid reduces but does not eliminate strategy-selection bias.",
                "Delisted-security history is incomplete, so survivorship bias remains.",
                "The 2024-2026 holdout is evaluated once and is not recycled into selection.",
            ],
        }
    )
    _write_checkpoint(output_path, payload)
    return payload


def screen_only(
    output_path: Path = OUTPUT,
    *,
    as_of_date: date,
    stability_runs: int = 1,
) -> dict[str, Any]:
    if stability_runs < 1:
        raise ValueError("stability_runs must be at least one")
    if not output_path.exists():
        raise FileNotFoundError(
            f"run the research strategy before --screen-only: {output_path}"
        )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    selected = payload.get("selection") or {}
    spec = RecoveryBlendSpec(**selected["spec"])
    portfolio = PortfolioSpec(**selected["portfolio"])
    service = FactorLabService()
    screens = [
        run_live_screen(
            service,
            spec=spec,
            portfolio=portfolio,
            as_of_date=as_of_date,
        )
        for _ in range(stability_runs)
    ]
    observations = [
        {
            "run_ids": {
                "anchor": value["anchor"]["run_id"],
                "recovery": value["recovery_module"]["run_id"],
            },
            "effective_trade_date": value["effective_trade_date"],
            "eligible_row_counts": {
                "anchor": value["anchor"]["eligible_row_count"],
                "recovery": value["recovery_module"]["eligible_row_count"],
            },
            "selected_tickers": [
                row.get("ticker") for row in value["selected_positions"]
            ],
        }
        for value in screens
    ]
    stable = len(
        {
            (
                value["effective_trade_date"],
                value["anchor"]["eligible_row_count"],
                value["recovery_module"]["eligible_row_count"],
                tuple(row.get("ticker") for row in value["selected_positions"]),
            )
            for value in screens
        }
    ) == 1
    latest = screens[-1]
    latest["source_stability"] = {
        "stable": stable,
        "run_count": stability_runs,
        "observations": observations,
        "action": (
            "screen may be consumed as a repeatable diagnostic"
            if stable
            else "wait for source ingestion to finish and rerun --screen-only"
        ),
    }
    payload.setdefault("final", {})["live_screen"] = latest
    _write_checkpoint(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=FULL[1])
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--stability-runs", type=int, default=1)
    args = parser.parse_args()
    if args.screen_only:
        result = screen_only(
            args.output,
            as_of_date=args.as_of_date,
            stability_runs=args.stability_runs,
        )
    else:
        result = run(args.output, as_of_date=args.as_of_date)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
