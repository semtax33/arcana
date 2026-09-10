"""Native reconciliation handles a provider's columnless empty DataFrame."""
from pathlib import Path

import pandas as pd


def test_empty_native_snapshot_keeps_its_factor_identity_schema(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/research"))
    from publish_kr_survivorship_financial_factors import snapshot

    class EmptyNativeClient:
        def query_df(self, query, *, parameters):
            assert parameters["securities"] == ["SEC_KR_035480"]
            return pd.DataFrame()

    result = snapshot(EmptyNativeClient(), ["2017-01"], ["035480"], tmp_path)
    assert list(result.columns) == ["security_id", "trade_date", "financial_basis", "factor_id", "factor_value"]
    assert result.empty
    assert pd.read_parquet(tmp_path / "2017-01.parquet").columns.equals(result.columns)
