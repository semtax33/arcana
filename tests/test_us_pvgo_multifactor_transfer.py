from __future__ import annotations

import json
from datetime import date
from pathlib import Path


RESULT_PATH = Path(
    "deliverables/us_pvgo_multifactor_transfer_20260906.json"
)


def _factor_signature(graph) -> dict[str, tuple[str, str]]:
    return {
        node.id: (
            str(node.config.get("factor_id")),
            str(node.config.get("financial_basis")),
        )
        for node in graph.nodes
        if node.type == "factor_input"
    }


def test_us_graph_changes_market_but_not_the_frozen_strategy() -> None:
    from scripts.apply_us_pvgo_multifactor_strategy import build_us_graph
    from scripts.discover_kr_pvgo_multifactor_strategy import (
        FINAL_MODEL_NAME,
        _frozen_final_spec,
        build_graph,
    )

    period = (date(2017, 1, 3), date(2026, 9, 4))
    kr = build_graph(
        FINAL_MODEL_NAME,
        _frozen_final_spec(),
        start_date=period[0],
        end_date=period[1],
    )
    us = build_us_graph(start_date=period[0], end_date=period[1])
    kr_nodes = {node.id: node for node in kr.nodes}
    us_nodes = {node.id: node for node in us.nodes}

    assert kr.experiment.market == "KR"
    assert us.experiment.market == "US"
    assert kr.experiment.rebalance == us.experiment.rebalance
    assert _factor_signature(kr) == _factor_signature(us)
    assert (
        kr_nodes["multifactor_score"].config
        == us_nodes["multifactor_score"].config
    )
    assert kr.outputs == us.outputs


def test_saved_run_reproducibility_ignores_only_machine_precision_noise() -> None:
    from scripts.apply_us_pvgo_multifactor_strategy import _metrics_equivalent

    baseline = {"sharpe": 0.41185860527012347, "rebalance_count": 39}
    rounding_only = {"sharpe": 0.4118586052701238, "rebalance_count": 39}
    material_change = {"sharpe": 0.4120, "rebalance_count": 39}

    assert _metrics_equivalent(baseline, rounding_only)
    assert not _metrics_equivalent(baseline, material_change)


def test_us_transfer_artifact_reports_the_same_strategy() -> None:
    assert RESULT_PATH.exists(), "US transfer result is missing"
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    transfer = payload["us_transfer"]

    assert transfer["experiment_id"]
    assert transfer["market"] == "US"
    assert transfer["same_strategy_audit"]["passed"] is True
    assert transfer["portfolio"] == {
        "max_positions": 50,
        "top_percent": 30.0,
    }
    assert {
        "pvgo_gap_pct",
        "roiic_wacc_spread",
        "pvgo_compression_pct",
        "asset_yoy_pct",
        "vol_12_1_ann",
        "beta",
    } == set(transfer["factor_ids"])
    assert "full_period" in transfer["performance_50bps"]
