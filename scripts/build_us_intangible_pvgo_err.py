from __future__ import annotations

"""Build a new PVGO Expectation Revision & Rerating (ERR) strategy.

This module intentionally does not import, mutate, or save over the frozen
``Arcana_US_IntangibleAdjustedPVGO_LevelOnly_MatchedSample_Quarterly_20260829``
experiment.  The old level-only strategy is a benchmark.  ERR adds separate
upside, revision, and recognition stages and expresses reflexivity through the
revision-recognition interaction in a multiplicative score.

The PEAD input is explicitly labelled a proxy: positive point-in-time EPS
surprise multiplied by positive one-month sector-relative return.  FactorLab
does not currently expose an exact announcement-window return node.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from api.service.dto import FactorLabGraphDto


ORIGINAL_LEVEL_ONLY_MODEL_NAME = (
    "Arcana_US_IntangibleAdjustedPVGO_LevelOnly_MatchedSample_Quarterly_20260829"
)
MODEL_NAME = "Arcana_US_IntangibleAdjustedPVGO_ERR_Quarterly_20260902"
START_DATE = date(2017, 10, 2)
END_DATE = date(2026, 8, 27)
TOP_PERCENT = 20.0
MAX_POSITIONS = 50
TRANSACTION_COST_BPS = 20.0
MIN_MARKET_CAP_USD_MILLIONS = 1_000.0
OPERATING_COMPANY_GICS_SECTORS = [
    "10",
    "15",
    "20",
    "25",
    "30",
    "35",
    "45",
    "50",
    "55",
]

DEFAULT_FOUNDATION_WEIGHTS = {
    "expectation_gap": 0.40,
    "adjusted_quality": 0.20,
    "incremental_quality": 0.15,
    "installed_quality": 0.15,
    "cash_validation": 0.10,
}
VALUE_HEAVY_FOUNDATION_WEIGHTS = {
    "expectation_gap": 0.60,
    "adjusted_quality": 0.15,
    "incremental_quality": 0.10,
    "installed_quality": 0.10,
    "cash_validation": 0.05,
}
QUALITY_HEAVY_FOUNDATION_WEIGHTS = {
    "expectation_gap": 0.30,
    "adjusted_quality": 0.25,
    "incremental_quality": 0.20,
    "installed_quality": 0.15,
    "cash_validation": 0.10,
}
REVISION_WEIGHTS = {
    "revision_30d": 0.50,
    "revision_acceleration": 0.30,
    "earnings_surprise": 0.20,
}
RECOGNITION_WEIGHTS = {
    "residual_momentum_6m": 0.40,
    "residual_return_1m": 0.20,
    "pead_proxy": 0.20,
    "price_to_target": 0.20,
}
ADDITIVE_WEIGHTS = {
    "foundation": 0.55,
    "revision": 0.25,
    "recognition": 0.20,
}


@dataclass(frozen=True)
class ErrGraphSpec:
    style: Literal["level_baseline", "additive", "multiplicative"]
    foundation_weights: dict[str, float]
    revision_acceleration: float = 0.50
    recognition_acceleration: float = 0.50
    quality_gate: bool = False
    rebalance_frequency: Literal["monthly", "quarterly"] = "quarterly"


def _edge(source: str, target: str, target_handle: str) -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{target_handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": target_handle,
    }


def _factor_pipeline(
    *,
    stem: str,
    factor_id: str,
    direction: str,
    financial_basis: str = "ttm",
    sector_relative: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    input_id = f"{stem}_input"
    winsor_id = f"{stem}_winsor"
    nodes: list[dict[str, Any]] = [
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
    ]
    edges = [_edge(input_id, winsor_id, "input")]
    if sector_relative:
        neutral_id = f"{stem}_sector_neutral"
        zscore_id = f"{stem}_market_z"
        nodes.extend(
            [
                {
                    "id": neutral_id,
                    "type": "neutralize",
                    "config": {"group_key": "sector"},
                },
                {
                    "id": zscore_id,
                    "type": "zscore",
                    "config": {
                        "group_by": ["trade_date"],
                        "stddev_method": "population",
                        "min_count": 20,
                        "zero_std_policy": "invalid",
                        "direction": direction,
                        "clip": 3.0,
                    },
                },
            ]
        )
        edges.extend(
            [
                _edge(winsor_id, neutral_id, "input"),
                _edge(neutral_id, zscore_id, "input"),
            ]
        )
    else:
        zscore_id = f"{stem}_sector_z"
        nodes.append(
            {
                "id": zscore_id,
                "type": "shrunk_zscore",
                "config": {
                    "group_key": "sector",
                    "min_market_count": 20,
                    "min_group_count": 20,
                    "shrinkage_strength": 20,
                    "direction": direction,
                    "clip": 3.0,
                },
            }
        )
        edges.append(_edge(winsor_id, zscore_id, "input"))
    return nodes, edges, zscore_id


def _positive_part(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    stem: str,
    source: str,
) -> str:
    zero_id = f"{stem}_zero"
    condition_id = f"{stem}_positive_condition"
    output_id = f"{stem}_positive"
    nodes.extend(
        [
            {"id": zero_id, "type": "constant", "config": {"value": 0.0}},
            {"id": condition_id, "type": "greater_than", "config": {}},
            {"id": output_id, "type": "condition", "config": {}},
        ]
    )
    edges.extend(
        [
            _edge(source, condition_id, "left"),
            _edge(zero_id, condition_id, "right"),
            _edge(condition_id, output_id, "condition"),
            _edge(source, output_id, "if_true"),
            _edge(zero_id, output_id, "if_false"),
        ]
    )
    return output_id


def _one_plus_scaled(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    stem: str,
    source: str,
    scale: float,
) -> str:
    scale_id = f"{stem}_scale"
    scaled_id = f"{stem}_scaled"
    one_id = f"{stem}_one"
    output_id = f"{stem}_one_plus"
    nodes.extend(
        [
            {"id": scale_id, "type": "constant", "config": {"value": scale}},
            {"id": scaled_id, "type": "mul", "config": {}},
            {"id": one_id, "type": "constant", "config": {"value": 1.0}},
            {"id": output_id, "type": "add", "config": {}},
        ]
    )
    edges.extend(
        [
            _edge(source, scaled_id, "left"),
            _edge(scale_id, scaled_id, "right"),
            _edge(one_id, output_id, "left"),
            _edge(scaled_id, output_id, "right"),
        ]
    )
    return output_id


def _and(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    node_id: str,
    left: str,
    right: str,
    research_design: str = "",
) -> str:
    nodes.append(
        {
            "id": node_id,
            "type": "and",
            "config": {"research_design": research_design},
        }
    )
    edges.extend([_edge(left, node_id, "left"), _edge(right, node_id, "right")])
    return node_id


def build_graph(
    *,
    name: str = MODEL_NAME,
    spec: ErrGraphSpec | None = None,
) -> FactorLabGraphDto:
    if name == ORIGINAL_LEVEL_ONLY_MODEL_NAME:
        raise ValueError("ERR must not overwrite the frozen level-only benchmark")
    spec = spec or ErrGraphSpec(
        style="multiplicative",
        foundation_weights=dict(DEFAULT_FOUNDATION_WEIGHTS),
    )
    if abs(sum(spec.foundation_weights.values()) - 1.0) > 1e-9:
        raise ValueError("foundation weights must sum to one")
    if spec.revision_acceleration < 0 or spec.recognition_acceleration < 0:
        raise ValueError("accelerator scales must be non-negative")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    pipelines = {
        "expectation_gap": (
            "normalized_intangible_adjusted_pvgo_pct",
            "lower_better",
            False,
        ),
        "adjusted_quality": (
            "intangible_adjusted_roe_spread_pct",
            "higher_better",
            False,
        ),
        "incremental_quality": ("roiic_wacc_spread", "higher_better", False),
        "installed_quality": ("roic_wacc_spread", "higher_better", False),
        "cash_validation": ("fcf_yield", "higher_better", False),
        "revision_30d": ("us_eps_revision_30d_pct", "higher_better", False),
        "revision_acceleration": (
            "us_eps_revision_acceleration_30d_pct",
            "higher_better",
            False,
        ),
        "earnings_surprise": ("us_eps_surprise_pct", "higher_better", False),
        "residual_return_1m": ("ret_1m", "higher_better", True),
        "residual_momentum_6m": ("tr_6_1", "higher_better", True),
        # P/TP is lower-better.  It remains optional inside recognition so
        # sparse historical analyst coverage cannot define the universe.
        "price_to_target": ("us_price_to_target_price", "lower_better", False),
    }
    outputs: dict[str, str] = {}
    for stem, (factor_id, direction, sector_relative) in pipelines.items():
        pipeline_nodes, pipeline_edges, output = _factor_pipeline(
            stem=stem,
            factor_id=factor_id,
            direction=direction,
            sector_relative=sector_relative,
        )
        nodes.extend(pipeline_nodes)
        edges.extend(pipeline_edges)
        outputs[stem] = output

    surprise_positive = _positive_part(
        nodes,
        edges,
        stem="pead_surprise",
        source=outputs["earnings_surprise"],
    )
    return_positive = _positive_part(
        nodes,
        edges,
        stem="pead_return",
        source=outputs["residual_return_1m"],
    )
    nodes.extend(
        [
            {
                "id": "pead_positive_product",
                "type": "mul",
                "config": {
                    "research_design": (
                        "PEAD proxy = positive PIT EPS surprise x positive one-month "
                        "sector-relative return"
                    )
                },
            },
            {
                "id": "pead_product_winsor",
                "type": "winsorize",
                "config": {
                    "group_by": ["trade_date"],
                    "lower_quantile": 0.01,
                    "upper_quantile": 0.99,
                },
            },
            {
                "id": "pead_proxy_z",
                "type": "zscore",
                "config": {
                    "group_by": ["trade_date"],
                    "stddev_method": "population",
                    "min_count": 20,
                    "zero_std_policy": "invalid",
                    "direction": "higher_better",
                    "clip": 3.0,
                },
            },
            {
                "id": "foundation_score",
                "type": "weighted_score",
                "config": {
                    "weights": spec.foundation_weights,
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "upside foundation: low adjusted PVGO plus adjusted/economic "
                        "quality and FCF validation"
                    ),
                },
            },
            {
                "id": "revision_score",
                "type": "weighted_score",
                "config": {
                    "weights": REVISION_WEIGHTS,
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "PIT EPS revision magnitude, acceleration, and surprise"
                    ),
                },
            },
            {
                "id": "recognition_score",
                "type": "weighted_score",
                "config": {
                    "weights": RECOGNITION_WEIGHTS,
                    "missing_weight_renormalize": True,
                    "research_design": (
                        "sector-relative price recognition plus PEAD proxy and optional "
                        "low price/target-price confirmation"
                    ),
                },
            },
        ]
    )
    edges.extend(
        [
            _edge(surprise_positive, "pead_positive_product", "left"),
            _edge(return_positive, "pead_positive_product", "right"),
            _edge("pead_positive_product", "pead_product_winsor", "input"),
            _edge("pead_product_winsor", "pead_proxy_z", "input"),
            *[
                _edge(outputs[handle], "foundation_score", handle)
                for handle in spec.foundation_weights
            ],
            _edge(outputs["revision_30d"], "revision_score", "revision_30d"),
            _edge(
                outputs["revision_acceleration"],
                "revision_score",
                "revision_acceleration",
            ),
            _edge(
                outputs["earnings_surprise"],
                "revision_score",
                "earnings_surprise",
            ),
            _edge(
                outputs["residual_momentum_6m"],
                "recognition_score",
                "residual_momentum_6m",
            ),
            _edge(
                outputs["residual_return_1m"],
                "recognition_score",
                "residual_return_1m",
            ),
            _edge("pead_proxy_z", "recognition_score", "pead_proxy"),
            _edge(outputs["price_to_target"], "recognition_score", "price_to_target"),
        ]
    )

    if spec.style == "level_baseline":
        nodes.append(
            {
                "id": "strategy_score",
                "type": "weighted_score",
                "config": {
                    "weights": {
                        "expectation_gap": 1.0,
                        "foundation": 0.0,
                        "revision": 0.0,
                        "recognition": 0.0,
                    },
                    "missing_weight_renormalize": False,
                    "research_design": (
                        "new non-destructive level-only benchmark on the exact ERR "
                        "complete-case sample"
                    ),
                },
            }
        )
        edges.extend(
            [
                _edge(outputs["expectation_gap"], "strategy_score", "expectation_gap"),
                _edge("foundation_score", "strategy_score", "foundation"),
                _edge("revision_score", "strategy_score", "revision"),
                _edge("recognition_score", "strategy_score", "recognition"),
            ]
        )
    elif spec.style == "additive":
        nodes.append(
            {
                "id": "strategy_score",
                "type": "weighted_score",
                "config": {
                    "weights": ADDITIVE_WEIGHTS,
                    "missing_weight_renormalize": False,
                    "research_design": "55% foundation + 25% revision + 20% recognition",
                },
            }
        )
        edges.extend(
            [
                _edge("foundation_score", "strategy_score", "foundation"),
                _edge("revision_score", "strategy_score", "revision"),
                _edge("recognition_score", "strategy_score", "recognition"),
            ]
        )
    else:
        nodes.append(
            {
                "id": "foundation_percentile",
                "type": "dense_score",
                "config": {
                    "group_by": ["trade_date"],
                    "order": "desc",
                    "scale": "0_100",
                },
            }
        )
        edges.append(_edge("foundation_score", "foundation_percentile", "input"))
        revision_positive = _positive_part(
            nodes,
            edges,
            stem="revision",
            source="revision_score",
        )
        recognition_positive = _positive_part(
            nodes,
            edges,
            stem="recognition",
            source="recognition_score",
        )
        revision_boost = _one_plus_scaled(
            nodes,
            edges,
            stem="revision_boost",
            source=revision_positive,
            scale=spec.revision_acceleration,
        )
        recognition_boost = _one_plus_scaled(
            nodes,
            edges,
            stem="recognition_boost",
            source=recognition_positive,
            scale=spec.recognition_acceleration,
        )
        nodes.extend(
            [
                {
                    "id": "foundation_times_revision",
                    "type": "mul",
                    "config": {
                        "research_design": (
                            "foundation x (1 + positive revision accelerator)"
                        )
                    },
                },
                {
                    "id": "strategy_score",
                    "type": "mul",
                    "config": {
                        "research_design": (
                            "ERR = foundation percentile x revision boost x recognition "
                            "boost; cross-term is the Soros reflexivity interaction"
                        )
                    },
                },
            ]
        )
        edges.extend(
            [
                _edge("foundation_percentile", "foundation_times_revision", "left"),
                _edge(revision_boost, "foundation_times_revision", "right"),
                _edge("foundation_times_revision", "strategy_score", "left"),
                _edge(recognition_boost, "strategy_score", "right"),
            ]
        )

    nodes.extend(
        [
            {
                "id": "normalized_adjusted_earnings_input",
                "type": "factor_input",
                "config": {
                    "factor_id": "normalized_intangible_adjusted_earnings_5y",
                    "financial_basis": "ttm",
                    "missing_policy": "drop",
                },
            },
            {"id": "earnings_floor", "type": "constant", "config": {"value": 0.0}},
            {"id": "positive_adjusted_earnings", "type": "greater_than", "config": {}},
            {
                "id": "market_cap_input",
                "type": "factor_input",
                "config": {
                    "factor_id": "mcap_mil",
                    "financial_basis": "annual",
                    "missing_policy": "drop",
                },
            },
            {
                "id": "market_cap_floor",
                "type": "constant",
                "config": {"value": MIN_MARKET_CAP_USD_MILLIONS},
            },
            {"id": "market_cap_eligible", "type": "greater_than", "config": {}},
        ]
    )
    edges.extend(
        [
            _edge(
                "normalized_adjusted_earnings_input",
                "positive_adjusted_earnings",
                "left",
            ),
            _edge("earnings_floor", "positive_adjusted_earnings", "right"),
            _edge("market_cap_input", "market_cap_eligible", "left"),
            _edge("market_cap_floor", "market_cap_eligible", "right"),
        ]
    )
    eligibility = _and(
        nodes,
        edges,
        node_id="base_eligibility",
        left="positive_adjusted_earnings",
        right="market_cap_eligible",
        research_design="positive normalized adjusted earnings and USD 1bn market cap",
    )
    if spec.quality_gate:
        nodes.extend(
            [
                {"id": "quality_gate_zero", "type": "constant", "config": {"value": 0.0}},
                {"id": "adjusted_quality_positive", "type": "greater_than", "config": {}},
                {"id": "installed_quality_positive", "type": "greater_than", "config": {}},
            ]
        )
        edges.extend(
            [
                _edge("adjusted_quality_input", "adjusted_quality_positive", "left"),
                _edge("quality_gate_zero", "adjusted_quality_positive", "right"),
                _edge("installed_quality_input", "installed_quality_positive", "left"),
                _edge("quality_gate_zero", "installed_quality_positive", "right"),
            ]
        )
        quality_ok = _and(
            nodes,
            edges,
            node_id="quality_gate",
            left="adjusted_quality_positive",
            right="installed_quality_positive",
            research_design="adjusted ROE-Ke > 0 and ROIC-WACC > 0",
        )
        eligibility = _and(
            nodes,
            edges,
            node_id="gated_eligibility",
            left=eligibility,
            right=quality_ok,
            research_design="base eligibility plus positive economic-quality floor",
        )

    nodes.extend(
        [
            {
                "id": "eligible_strategy_score",
                "type": "condition_score",
                "config": {
                    "research_design": (
                        "eligibility is applied before the final cross-sectional rank"
                    )
                },
            },
            {
                "id": "final_rank_score",
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
            _edge(eligibility, "eligible_strategy_score", "condition"),
            _edge("strategy_score", "eligible_strategy_score", "score"),
            _edge("eligible_strategy_score", "final_rank_score", "input"),
        ]
    )

    return FactorLabGraphDto(
        version=1,
        experiment={
            "name": name,
            "market": "US",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "factor_data_mode": "point_in_time_snapshot",
            "snapshot_coverage_policy": "strict",
            "universe": {
                "type": "market",
                "sector_codes": OPERATING_COMPANY_GICS_SECTORS,
            },
            "rebalance": {
                "frequency": spec.rebalance_frequency,
                "signal_lag_days": 1,
                "transaction_cost_bps": TRANSACTION_COST_BPS,
            },
            "research_design": {
                "strategy_family": "Expectation Revision & Rerating",
                "style": spec.style,
                "foundation_weights": spec.foundation_weights,
                "revision_acceleration": spec.revision_acceleration,
                "recognition_acceleration": spec.recognition_acceleration,
                "quality_gate": spec.quality_gate,
                "price_target_policy": (
                    "optional confirmation; never defines eligibility; lower P/TP is better"
                ),
                "pead_policy": "PIT EPS surprise x sector-relative 1M return proxy",
                "reflexivity_policy": (
                    "revision and recognition interaction, not a standalone factor"
                ),
                "frozen_benchmark_preserved": ORIGINAL_LEVEL_ONLY_MODEL_NAME,
            },
        },
        nodes=nodes,
        edges=edges,
        outputs={"final_node_id": "final_rank_score"},
    )
