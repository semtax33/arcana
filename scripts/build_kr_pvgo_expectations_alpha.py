from __future__ import annotations

"""Build a non-destructive Korean port of the U.S. PVGO Expectations Alpha.

The score topology and weights are cloned from
``Arcana_US_PVGO_ExpectationsAlpha_Quarterly_20260829``.  Only the experiment
identity, market, dates, and market universe change.  No intangible-adjusted
factor is introduced.
"""

from copy import deepcopy
from datetime import date

from api.service.dto import FactorLabGraphDto
from scripts.build_us_pvgo_expectations_alpha import (
    FINAL_WEIGHTS,
    MAX_POSITIONS,
    MODEL_NAME as US_SOURCE_MODEL_NAME,
    NON_FINANCIAL_GICS_SECTORS,
    REBALANCE_FREQUENCY,
    TOP_PERCENT,
    TRANSACTION_COST_BPS,
    build_graph as build_us_graph,
)


MODEL_NAME = "Arcana_KR_PVGO_ExpectationsAlpha_Quarterly_2002_2026_20260906"
START_DATE = date(2002, 4, 1)
END_DATE = date(2026, 8, 24)
EARLY_PERIOD = (START_DATE, date(2012, 12, 31))
LATE_PERIOD = (date(2016, 4, 1), END_DATE)
UNAVAILABLE_PERIOD = (date(2013, 1, 1), date(2016, 3, 31))


def build_graph(name: str = MODEL_NAME) -> FactorLabGraphDto:
    if name == US_SOURCE_MODEL_NAME:
        raise ValueError("the Korean port must not overwrite the U.S. source model")

    payload = deepcopy(build_us_graph().model_dump(mode="json"))
    experiment = payload["experiment"]
    experiment.update(
        {
            "name": name,
            "market": "KR",
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "factor_data_mode": "point_in_time_snapshot",
            # The source model deliberately renormalizes across sleeves that
            # genuinely did not exist on an old signal date.  Korea needs the
            # same policy because justified-PVGO history matures much later.
            "snapshot_coverage_policy": "allow_missing_inputs",
            "universe": {
                "type": "market",
                "sector_codes": list(NON_FINANCIAL_GICS_SECTORS),
                "industry_group_codes": [],
            },
            "rebalance": {
                "frequency": REBALANCE_FREQUENCY,
                "signal_lag_days": 1,
                "transaction_cost_bps": TRANSACTION_COST_BPS,
            },
        }
    )
    return FactorLabGraphDto(**payload)


__all__ = [
    "EARLY_PERIOD",
    "END_DATE",
    "FINAL_WEIGHTS",
    "LATE_PERIOD",
    "MAX_POSITIONS",
    "MODEL_NAME",
    "NON_FINANCIAL_GICS_SECTORS",
    "REBALANCE_FREQUENCY",
    "START_DATE",
    "TOP_PERCENT",
    "TRANSACTION_COST_BPS",
    "UNAVAILABLE_PERIOD",
    "US_SOURCE_MODEL_NAME",
    "build_graph",
]
