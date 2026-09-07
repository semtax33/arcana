from __future__ import annotations

import pandas as pd
import pytest

from scripts import backfill_kr_all_factors_20260825_20260904 as job


def test_backfill_contract_has_exact_nine_korean_sessions():
    assert job.TARGET_DATES == (
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    )
    assert job.FINANCIAL_BASES == ("annual", "quarterly", "ttm")
    assert job.FDR_DATE == "2026-09-04"


def test_validate_target_frame_rejects_a_missing_session():
    rows = [
        {"security_id": "SEC_KR_005930", "trade_date": day}
        for day in job.TARGET_DATES[:-1]
        for _ in range(2700)
    ]
    frame = pd.DataFrame(rows)
    frame["security_id"] = [f"SEC_KR_{index % 2700:06d}" for index in range(len(frame))]
    with pytest.raises(RuntimeError, match="dates mismatch"):
        job._validate_target_frame(frame, name="test")


def test_validate_target_frame_rejects_duplicate_security_date():
    rows = [
        {"security_id": f"SEC_KR_{index:06d}", "trade_date": day}
        for day in job.TARGET_DATES
        for index in range(2700)
    ]
    frame = pd.DataFrame(rows)
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(RuntimeError, match="duplicate"):
        job._validate_target_frame(frame, name="test")


def test_sharding_is_stable_and_complete():
    security_ids = ["SEC_KR_005930", "SEC_KR_000660", "SEC_KR_035420"]
    first = job._shard_for_ids(security_ids)
    second = job._shard_for_ids(list(reversed(security_ids)))
    assert first == second
    assert set(first) == set(security_ids)
    assert all(0 <= shard < job.SHARD_COUNT for shard in first.values())


def test_resume_requires_every_basis_and_expected_security_date():
    expected = {"SEC_KR_A": 9, "SEC_KR_B": 8}
    observed = [
        ("SEC_KR_A", "annual", 9),
        ("SEC_KR_A", "quarterly", 9),
        ("SEC_KR_A", "ttm", 9),
        ("SEC_KR_B", "annual", 8),
        ("SEC_KR_B", "quarterly", 8),
        ("SEC_KR_B", "ttm", 7),
    ]
    assert job._fully_completed_security_ids(expected, observed) == {"SEC_KR_A"}
