from datetime import date
import pytest
from tests.test_factor_lab_v2_clickhouse import isolated_lab


def test_next_earnings_uses_release_identity_and_availability_not_daily_surprise():
    from api.service.factor_lab_earnings import evaluate_earnings_outcome
    config = {"provider": "ALPHA_VANTAGE", "target_field": "surprise_pct", "horizons": [1, 2], "max_wait_days": 180, "bucket_count": 2, "score_order": "higher"}
    scores = [{"trade_date": date(2026, 1, 2), "security_id": "A", "value": 10}]
    events = [dict(security_id="A", event_date=date(2026, 1, 2), availability_date=date(2026, 1, 5), surprise_pct=999, fiscal_period_end="2025-09-30"),
              dict(security_id="A", event_date=date(2026, 1, 6), availability_date=date(2026, 1, 7), surprise_pct=5, fiscal_period_end="2025-12-31"),
              dict(security_id="A", event_date=date(2026, 4, 6), availability_date=date(2026, 4, 7), surprise_pct=-2, fiscal_period_end="2026-03-31")]
    pending = evaluate_earnings_outcome(config, scores, events, date(2026, 1, 6))
    assert pending["horizons"][0]["baseline"]["pending_count"] == 1
    result = evaluate_earnings_outcome(config, scores, events, date(2026, 1, 7))
    assert result["horizons"][0]["baseline"]["mean"] == 5
    assert result["horizons"][1]["baseline"]["pending_count"] == 1
    assert result["observations"][0]["event_date"] == "2026-01-06"


def test_unavailable_first_release_is_not_skipped_for_a_later_release():
    from api.service.factor_lab_earnings import evaluate_earnings_outcome
    config = {"provider": "ALPHA_VANTAGE", "target_field": "surprise_pct", "horizons": [1], "max_wait_days": 180, "bucket_count": 2, "score_order": "higher"}
    events = [{"security_id": "A", "event_date": "2026-01-06", "availability_date": "2026-01-09", "surprise_pct": 999},
              {"security_id": "A", "event_date": "2026-01-07", "availability_date": "2026-01-08", "surprise_pct": 5}]
    result = evaluate_earnings_outcome(config, [{"trade_date": "2026-01-02", "security_id": "A", "value": 10}], events, date(2026, 1, 8))
    stats = result["horizons"][0]["baseline"]
    assert (stats["mean"], stats["pending_count"]) == (None, 1)


def test_ambiguous_yahoo_history_dates_cannot_be_selected_as_actual_releases():
    from tests.test_factor_lab_v2 import graph_v2
    from api.repository.factor_lab_query import validate_factor_lab_graph
    graph = graph_v2()
    graph["nodes"][1].update(type="earnings_outcome", config={"provider": "YAHOO_FINANCE", "target_field": "surprise_pct", "horizons": [1], "score_order": "higher"})
    assert not validate_factor_lab_graph(graph).valid


@pytest.mark.integration
def test_earnings_evaluation_is_executed_and_persisted_with_score_run(isolated_lab):
    from tests.test_factor_lab_v2 import graph_v2
    from api.service.dto import FactorLabGraphDto, FactorLabRunRequestDto
    from api.service.factor_lab_service import FactorLabService
    from api.service.factor_lab_evaluation_service import FactorLabEvaluationService
    client = isolated_lab()
    client.command("""CREATE TABLE us_consensus_events (security_id String, event_type String,
        provider String, event_date Date, availability_date Date, snapshot_date Date,
        fiscal_period_end Nullable(Date), surprise_pct Nullable(Float64), reported_eps Nullable(Float64),
        estimated_eps Nullable(Float64), raw_path String) ENGINE=Memory""")
    client.insert("us_consensus_events", [["SEC_US_A", "EARNINGS_RELEASE", "ALPHA_VANTAGE", date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 7), date(2025, 12, 31), 5, 1.05, 1, "fixture"]])
    client.close()
    graph = graph_v2()
    graph["experiment"].update(end_date="2026-01-06", factor_data_mode="point_in_time_snapshot")
    graph["nodes"][1].update(type="earnings_outcome", config={"provider": "ALPHA_VANTAGE", "target_field": "surprise_pct", "horizons": [1], "max_wait_days": 180, "bucket_count": 2, "score_order": "higher"})
    run = FactorLabService(client_factory=isolated_lab).run_graph(FactorLabRunRequestDto(graph=FactorLabGraphDto(**graph), mode="history", evaluation_as_of=date(2026, 1, 7)))
    assert run.evaluation is not None, run.warnings
    stats = run.evaluation["evaluations"][0]["horizons"][0]["baseline"]
    assert (stats["mean"], stats["valid_count"], stats["pending_count"]) == (5, 2, 1)
    assert FactorLabEvaluationService(client_factory=isolated_lab).list_evaluations(run.run_id)["evaluations"][0] == run.evaluation
