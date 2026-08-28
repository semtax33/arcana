from __future__ import annotations

from datetime import date

import pandas as pd

from api.service.factor_lab_service import _require_history_snapshot_coverage


class _CoverageClient:
    def __init__(self) -> None:
        self.query = ""
        self.parameters: dict[str, object] = {}

    def query_df(self, query: str, parameters=None):
        self.query = query
        self.parameters = dict(parameters or {})
        return pd.DataFrame({"trade_date": self.parameters["trade_dates"]})


def test_history_snapshot_coverage_is_scoped_to_graph_market() -> None:
    client = _CoverageClient()
    graph = {
        "experiment": {"market": "US"},
        "nodes": [
            {
                "type": "factor_input",
                "config": {"factor_id": "opm", "financial_basis": "annual"},
            }
        ],
    }

    table = _require_history_snapshot_coverage(
        client,
        graph_dict=graph,
        trade_dates=[date(2021, 6, 30), date(2021, 12, 31)],
    )

    assert table == "fact_daily_factor_snapshot"
    assert "startsWith(security_id, {market_security_prefix:String})" in client.query
    assert client.parameters["market_security_prefix"] == "SEC_US_"


def test_history_snapshot_coverage_can_allow_missing_factor_inputs() -> None:
    client = _CoverageClient()
    graph = {
        "experiment": {
            "market": "US",
            "snapshot_coverage_policy": "allow_missing_inputs",
        },
        "nodes": [
            {
                "type": "factor_input",
                "config": {"factor_id": "opm", "financial_basis": "annual"},
            },
            {
                "type": "factor_input",
                "config": {"factor_id": "tr_12_1", "financial_basis": "annual"},
            },
        ],
    }

    _require_history_snapshot_coverage(
        client,
        graph_dict=graph,
        trade_dates=[date(2016, 3, 30), date(2016, 6, 29)],
    )

    assert "HAVING count() > 0" in client.query
    assert client.parameters["factor_pair_count"] == 2


def test_history_snapshot_coverage_remains_strict_by_default() -> None:
    client = _CoverageClient()
    graph = {
        "experiment": {"market": "US"},
        "nodes": [
            {
                "type": "factor_input",
                "config": {"factor_id": "opm", "financial_basis": "annual"},
            }
        ],
    }

    _require_history_snapshot_coverage(
        client,
        graph_dict=graph,
        trade_dates=[date(2021, 6, 30)],
    )

    assert "HAVING uniqExact(tuple(factor_id, financial_basis))" in client.query
