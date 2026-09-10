"""Native publication must retain audited scope and reject unsupported removal."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/research"))
from publish_kr_survivorship_market_factors import plan_delta, verify_correction_versions, next_revision_time


def frame(values):
    return pd.DataFrame([dict(security_id="SEC_KR_003410", trade_date=pd.Timestamp(day),
        financial_basis="annual", factor_id="na_20", factor_value=value,
        fiscal_year=2011, financial_period=pd.Timestamp("2011-12-31"), currency="KRW",
        updated_at=pd.Timestamp("2026-09-10")) for day, value in values])


def test_publication_inserts_missing_and_corrected_values_only():
    old = frame([("2012-01-03", 5000.), ("2012-01-04", 1000.)])
    new = frame([("2012-01-03", 1000.), ("2012-01-04", 1000.), ("2012-01-05", 1100.)])
    delta, corrections = plan_delta(old, new)
    assert delta.trade_date.dt.strftime("%Y-%m-%d").tolist() == ["2012-01-03", "2012-01-05"]
    assert delta.factor_value.tolist() == [1000., 1100.]
    assert corrections.previous_value.tolist() == [5000.]
    assert corrections.corrected_value.tolist() == [1000.]
    assert plan_delta(new, new)[0].empty


def test_publication_restores_financial_metadata_even_when_value_is_unchanged():
    old = frame([("2012-01-03", 1000.)])
    old["fiscal_year"] = pd.array([None], dtype="Int64")
    old["financial_period"] = pd.NaT
    new = frame([("2012-01-03", 1000.)])
    delta, corrections = plan_delta(old, new)
    assert len(delta) == 1
    assert delta.fiscal_year.tolist() == [2011]
    assert delta.financial_period.tolist() == [pd.Timestamp("2011-12-31")]
    assert corrections.previous_value.tolist() == corrections.corrected_value.tolist() == [1000.]
    # A later reviewed source can also withdraw an unsupported period.
    withdrawn, _ = plan_delta(new, old)
    assert len(withdrawn) == 1 and withdrawn.financial_period.isna().all()


def test_publication_does_not_silently_remove_unproduced_native_factors():
    with pytest.raises(AssertionError, match="missing-value revision"):
        plan_delta(frame([("2012-01-03", 5000.), ("2012-01-04", 1000.)]), frame([("2012-01-03", 1000.)]))


def test_publication_rejects_duplicate_candidate_identity():
    with pytest.raises(AssertionError, match="Duplicate factor identity"):
        plan_delta(frame([("2012-01-03", 5000.)]), frame([("2012-01-03", 1000.), ("2012-01-03", 2000.)]))


def test_future_version_on_unchanged_row_does_not_block_audited_corrections():
    old = frame([("2012-01-03", 5000.), ("2012-01-04", 1000.)])
    old["updated_at"] = pd.to_datetime(["2026-09-06", "2026-09-11"]).tz_localize("Asia/Seoul")
    new = frame([("2012-01-03", 1000.), ("2012-01-04", 1000.)])
    _, corrections = plan_delta(old, new)
    stamp = pd.Timestamp("2026-09-10 22:00", tz="Asia/Seoul")
    verify_correction_versions(old, corrections, stamp)
    old.loc[0, "updated_at"] = pd.Timestamp("2026-09-11", tz="Asia/Seoul")
    with pytest.raises(AssertionError, match="Correction version"):
        verify_correction_versions(old, corrections, stamp)
    with pytest.raises(AssertionError, match="timezone"):
        verify_correction_versions(old, corrections, stamp.tz_localize(None))


def test_revision_ordering_preserves_a_future_legacy_version_and_real_wall_clock():
    old = frame([("2012-01-03", 5000.)])
    old["updated_at"] = pd.to_datetime(["2026-09-11 00:23:18.946"]).tz_localize("Asia/Seoul")
    new = frame([("2012-01-03", 1000.)])
    _, revisions = plan_delta(old, new)
    wall_clock = pd.Timestamp("2026-09-10 22:00:00", tz="Asia/Seoul")
    version = next_revision_time(old, revisions, wall_clock)
    assert version == pd.Timestamp("2026-09-11 00:23:18.947", tz="Asia/Seoul")
    assert wall_clock == pd.Timestamp("2026-09-10 22:00:00", tz="Asia/Seoul")
    verify_correction_versions(old, revisions, version)
