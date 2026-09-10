"""A reviewed correction must not silently overwrite unrelated native values."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/research"))
from publish_kr_survivorship_period_factors import plan_delta


def frame(value, factor="roe", symbol="008560", basis="annual"):
    return pd.DataFrame([dict(security_id=f"SEC_KR_{symbol}", trade_date="2019-04-02",
        financial_basis=basis, factor_id=factor, factor_value=value)])


def test_revision_requires_exact_reviewed_previous_value_and_scope():
    previous = frame(12.809845156773465)
    expected = frame(12.78635835398759)
    delta, corrections = plan_delta(previous, expected, previous)
    assert len(delta) == len(corrections) == 1
    assert delta.iloc[0].factor_value == 12.78635835398759
    with pytest.raises(AssertionError, match="previous"):
        plan_delta(frame(99), expected, previous)
    with pytest.raises(AssertionError, match="scope"):
        plan_delta(frame(10, factor="npm"), frame(11, factor="npm"), frame(10, factor="npm"))
    delta, corrections = plan_delta(expected, expected, previous)
    assert delta.empty and corrections.empty
