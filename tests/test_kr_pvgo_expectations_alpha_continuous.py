from __future__ import annotations


def test_continuous_graph_uses_new_identity_and_exact_requested_weights() -> None:
    from scripts.build_kr_pvgo_expectations_alpha_continuous import (
        BRIDGE_MODEL_NAME,
        END_DATE,
        FINAL_WEIGHTS,
        MODEL_NAME,
        START_DATE,
        US_SOURCE_MODEL_NAME,
        build_graph,
    )

    graph = build_graph()

    assert MODEL_NAME not in {US_SOURCE_MODEL_NAME, BRIDGE_MODEL_NAME}
    assert graph.experiment.name == MODEL_NAME
    assert str(graph.experiment.start_date) == str(START_DATE)
    assert str(graph.experiment.end_date) == str(END_DATE)
    assert graph.experiment.market == "KR"
    assert graph.experiment.snapshot_coverage_policy == "allow_missing_inputs"
    assert FINAL_WEIGHTS == {
        "gap": 0.40,
        "quality": 0.25,
        "compression": 0.20,
        "raw": 0.15,
    }
    weighted = next(node for node in graph.nodes if node.id == "expectations_alpha")
    assert weighted.config["weights"] == FINAL_WEIGHTS
    assert weighted.config["missing_weight_renormalize"] is False
    zscores = [node for node in graph.nodes if node.type == "zscore"]
    assert zscores
    assert all(node.config["group_by"] == ["trade_date"] for node in zscores)
    assert all(node.config["min_count"] == 3 for node in zscores)


def test_continuous_runner_uses_both_requested_benchmarks() -> None:
    from scripts.run_kr_pvgo_expectations_alpha_continuous import BENCHMARKS

    assert BENCHMARKS == ("KOSPI200", "KOSDAQ")
