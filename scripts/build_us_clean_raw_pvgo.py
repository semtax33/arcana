from __future__ import annotations

"""Build the accounting-unadjusted control for the clean PVGO experiment.

The graph is cloned from the clean intangible-adjusted strategy on purpose.
Only the three economic measurements and the positive-earnings gate change;
universe, dates, PIT policy, winsorization, sector shrinkage, weights, and
portfolio hints remain identical.  This makes C-B an accounting-treatment
contrast instead of a comparison between unrelated strategy designs.
"""

from copy import deepcopy

from api.service.dto import FactorLabGraphDto
from scripts.build_us_intangible_adjusted_pvgo import (
    END_DATE,
    FACTOR_NODE_IDS,
    FINAL_WEIGHTS,
    MAX_POSITIONS,
    MIN_MARKET_CAP_USD_MILLIONS,
    OPERATING_COMPANY_GICS_SECTORS,
    REBALANCE_FREQUENCY,
    START_DATE,
    TOP_PERCENT,
    TRANSACTION_COST_BPS,
    build_graph as build_adjusted_graph,
)


MODEL_NAME = "Arcana_US_CleanRawPVGO_ExpectationsAlpha_Quarterly_20260829"
RAW_FACTOR_IDS = {
    "expectation_level_input": "equity_pvgo_pct",
    "quality_input": "roe_cost_of_equity_spread_pct",
    "expectation_change_input": "equity_pvgo_compression_pct",
    "normalized_adjusted_eps_input": "normalized_earnings_5y",
}


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    payload = deepcopy(build_adjusted_graph().model_dump(mode="json"))
    payload["experiment"]["name"] = name
    nodes = {node["id"]: node for node in payload["nodes"]}
    for node_id, factor_id in RAW_FACTOR_IDS.items():
        nodes[node_id]["config"]["factor_id"] = factor_id

    nodes["positive_normalized_adjusted_eps"]["config"]["research_design"] = (
        "exclude negative normalized unadjusted equity earnings"
    )
    nodes["eligibility_gate"]["config"]["research_design"] = (
        "positive normalized unadjusted earnings and minimum USD 1bn market cap"
    )
    nodes["intangible_expectations_alpha"]["config"]["research_design"] = (
        "clean unadjusted control: equal-weight expectation level, unit-consistent "
        "ROE-cost-of-equity quality, and equity-PVGO compression; topology and "
        "parameters frozen to the adjusted graph"
    )
    nodes["eligible_expectations_alpha"]["config"]["research_design"] = (
        "unadjusted positive-earnings and microcap guard before ranking"
    )
    return FactorLabGraphDto(**payload)


__all__ = [
    "END_DATE",
    "FACTOR_NODE_IDS",
    "FINAL_WEIGHTS",
    "MAX_POSITIONS",
    "MIN_MARKET_CAP_USD_MILLIONS",
    "MODEL_NAME",
    "OPERATING_COMPANY_GICS_SECTORS",
    "RAW_FACTOR_IDS",
    "REBALANCE_FREQUENCY",
    "START_DATE",
    "TOP_PERCENT",
    "TRANSACTION_COST_BPS",
    "build_graph",
]
