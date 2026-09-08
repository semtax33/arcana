from datetime import date
import json
import pytest

from api.service.factor_lab_outcomes import evaluate_forward_outcome


CONFIG = {"target_factor_id": "roe", "financial_basis": "annual", "measure": "change", "horizons": [1, 5], "unit": "trading_day", "bucket_count": 2, "score_order": "higher"}


def test_outcome_buckets_are_chosen_before_missing_labels_and_pending_is_separate():
    scores = [{"security_id": s, "trade_date": "2026-01-02", "value": v} for s, v in [("A", 1), ("B", 2), ("C", 3), ("D", 4)]]
    targets = [
        {"security_id": s, "trade_date": d, "value": v, "financial_period": "2025-12-31"}
        for s, d, v in [("A", "2026-01-02", 10), ("A", "2026-01-05", 12), ("B", "2026-01-02", 20),
                        ("C", "2026-01-02", 30), ("C", "2026-01-05", 27), ("D", "2026-01-02", 40), ("D", "2026-01-05", 44)]
    ]
    result = evaluate_forward_outcome(CONFIG, scores, targets, ["2026-01-02", "2026-01-05"], date(2026, 1, 5))
    first, pending = result["horizons"]
    assert first["baseline"]["valid_count"] == 3
    assert first["baseline"]["mean"] == 1
    assert first["buckets"][0]["mean"] == 2
    assert first["buckets"][0]["missing_count"] == 1
    assert first["buckets"][1]["mean"] == 0.5
    assert first["buckets"][1]["positive_rate"] == 0.5
    assert pending["baseline"]["pending_count"] == 4
    assert pending["baseline"]["missing_count"] == 0
    assert pending["baseline"]["mean"] is None
    assert len(result["input_hash"]) == 64


@pytest.mark.parametrize("measure,old,new,old_period,new_period,expected,reason", [
    ("change", 10, 12, "2025-12-31", "2025-12-31", 2, ""),
    ("pct_change", 10, 12, None, None, 20, ""),
    ("pct_change", -10, -5, None, None, None, "nonpositive_baseline"),
    ("direction", 10, 8, None, None, -1, ""),
    ("direction", 10, 10, None, None, 0, ""),
    ("change", 10, 12, "2025-12-31", "2026-03-31", None, "financial_period_changed"),
    ("level", None, 12, None, "2026-03-31", 12, ""),
    ("change", 10, float("nan"), None, None, None, "missing_target"),
])
def test_target_measures_keep_units_and_comparability(measure, old, new, old_period, new_period, expected, reason):
    result = evaluate_forward_outcome({**CONFIG, "measure": measure, "horizons": [1]},
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 1}],
        [{"security_id": "A", "trade_date": "2026-01-02", "value": old, "financial_period": old_period},
         {"security_id": "A", "trade_date": "2026-01-05", "value": new, "financial_period": new_period}],
        ["2026-01-02", "2026-01-05"], date(2026, 1, 5))
    assert result["observations"][0]["outcome"] == expected
    assert result["observations"][0]["reason"] == reason
    json.dumps(result, allow_nan=False)


def test_source_published_after_snapshot_date_is_not_usable():
    result = evaluate_forward_outcome({**CONFIG, "measure": "level", "horizons": [1]},
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 1}],
        [{"security_id": "A", "trade_date": "2026-01-05", "value": 10, "source_trade_date": "2026-01-06"}],
        ["2026-01-02", "2026-01-05"], date(2026, 1, 5))
    assert result["observations"][0]["reason"] == "source_after_snapshot"


def test_researcher_can_explicitly_compare_actual_metrics_across_reporting_periods():
    result = evaluate_forward_outcome({**CONFIG, "horizons": [1], "period_policy": "allow_change"},
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 1}],
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 10, "financial_period": "2025-09-30"},
         {"security_id": "A", "trade_date": "2026-01-05", "value": 12, "financial_period": "2025-12-31"}],
        ["2026-01-02", "2026-01-05"], date(2026, 1, 5))
    assert result["observations"][0]["outcome"] == 2


def test_calendar_day_outcome_does_not_fill_weekends_from_later_dates():
    result = evaluate_forward_outcome({**CONFIG, "measure": "level", "horizons": [1], "unit": "calendar_day"},
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 1}],
        [{"security_id": "A", "trade_date": "2026-01-05", "value": 12}],
        ["2026-01-02", "2026-01-05"], date(2026, 1, 5))
    assert result["observations"][0]["target_date"] == "2026-01-03"
    assert result["observations"][0]["reason"] == "missing_target"


def test_later_information_is_excluded_from_cutoff_and_input_digest():
    args = ({**CONFIG, "measure": "level", "horizons": [1]},
        [{"security_id": "A", "trade_date": "2026-01-02", "value": 1}])
    first = evaluate_forward_outcome(*args, [], ["2026-01-02"], date(2026, 1, 2))
    second = evaluate_forward_outcome(*args, [{"security_id": "A", "trade_date": "2026-01-05", "value": 12}], ["2026-01-02", "2026-01-05"], date(2026, 1, 2))
    assert first == second
