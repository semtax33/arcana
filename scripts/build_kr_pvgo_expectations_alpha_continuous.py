from __future__ import annotations

"""Build the sparse-history robust 2002-2026 Korean PVGO graph.

This is intentionally a new model identity.  It keeps the requested economic
weights and the non-financial universe, but uses a market cross-section for
normalisation because the justified-PVGO gap is sparse in the restored early
history and cannot support five observations inside every sector.
"""

from copy import deepcopy
from datetime import date

from api.service.dto import FactorLabGraphDto
from scripts.build_kr_pvgo_expectations_alpha import (
    FINAL_WEIGHTS,
    MAX_POSITIONS,
    MODEL_NAME as BRIDGE_MODEL_NAME,
    NON_FINANCIAL_GICS_SECTORS,
    REBALANCE_FREQUENCY,
    TOP_PERCENT,
    TRANSACTION_COST_BPS,
    US_SOURCE_MODEL_NAME,
    build_graph as build_bridge_graph,
)


MODEL_NAME = (
    "Arcana_KR_PVGO_ExpectationsAlpha_Quarterly_"
    "2002_2026_ContinuousMarketZ_20260906"
)
START_DATE = date(2002, 1, 2)
END_DATE = date(2026, 9, 4)


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    if name in {US_SOURCE_MODEL_NAME, BRIDGE_MODEL_NAME}:
        raise ValueError("the continuous model must have a new experiment identity")
    payload = deepcopy(build_bridge_graph(name=name).model_dump(mode="json"))
    payload["experiment"].update(
        {
            "name": name,
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            # Some 2002-era dates predate the first justified-PVGO observation.
            # Permit the history engine to traverse those dates, but keep the
            # score itself complete-case by disabling weight renormalization.
            "snapshot_coverage_policy": "allow_missing_inputs",
        }
    )
    for node in payload["nodes"]:
        if node["type"] == "zscore":
            node["config"]["group_by"] = ["trade_date"]
            node["config"]["min_count"] = 3
            node["config"]["normalization_scope"] = (
                "KR non-financial market cross-section; sparse-history robust"
            )
        if node["id"] == "expectations_alpha":
            node["config"]["missing_weight_renormalize"] = False
            node["config"]["research_design"] = (
                "40% gap + 25% quality + 20% compression + 15% raw PVGO; "
                "all four sleeves required"
            )
    return FactorLabGraphDto(**payload)


__all__ = [
    "BRIDGE_MODEL_NAME",
    "END_DATE",
    "FINAL_WEIGHTS",
    "MAX_POSITIONS",
    "MODEL_NAME",
    "NON_FINANCIAL_GICS_SECTORS",
    "REBALANCE_FREQUENCY",
    "START_DATE",
    "TOP_PERCENT",
    "TRANSACTION_COST_BPS",
    "US_SOURCE_MODEL_NAME",
    "build_graph",
]
