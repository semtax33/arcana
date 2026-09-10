"""A new receipt history invalidates completed factor refresh checkpoints."""
import argparse
from datetime import date
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from engine.core.paths import DataLakePaths
from engine.workflows._internal import refresh_workflow as workflow


def setup_refresh(tmp_path, monkeypatch):
    lake = DataLakePaths.from_project_root(tmp_path)
    monkeypatch.setattr(workflow, "DATA_LAKE", lake)
    manifest = lake.silver("dart", "normalized", "history", "035480", "manifest.json")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema_version": 1, "market": "kr", "symbol": "035480",
        "receipts": [{"report_date": "2020-03-31"}]}), encoding="utf-8")
    args = argparse.Namespace(market="kr", dry_run=False, skip_clickhouse=False, financial_basis="annual",
        symbols="035480", workers=1, force_full=False, complete_universe_ratio=.99)
    state = workflow.RefreshState.open(tmp_path / "state.json", signature={"market": "kr"}, resume=False, enabled=True)
    window = workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))
    state.complete_step("factors-insert", window)
    monkeypatch.setattr(workflow, "ensure_krx_silver_market_data_current", lambda: None)
    monkeypatch.setattr(workflow, "load_factor_catalog", lambda *args: None)
    monkeypatch.setattr(workflow, "resolve_latest_complete_trade_date", lambda *args, **kwargs: date(2026, 6, 22))
    monkeypatch.setattr(workflow, "latest_market_table_date", lambda *args, **kwargs: date(2026, 6, 10))
    delete = Mock()
    monkeypatch.setattr(workflow, "market_scoped_delete", delete)
    result = pd.DataFrame()
    result.attrs["inserted_rows"] = 10
    insert = Mock(return_value=result)
    monkeypatch.setattr(workflow.factor_loader, "insert_daily_factors", insert)
    monkeypatch.setattr(workflow.factor_loader, "_insert_daily_factor_rows_by_partition",
                        lambda client, frame, **kwargs: len(frame))
    return args, state, window, manifest, insert, delete


def test_new_history_rebuilds_before_reusing_completed_factor_step(tmp_path, monkeypatch):
    args, state, window, manifest, insert, _ = setup_refresh(tmp_path, monkeypatch)
    workflow.run_factor_refresh(args, window, object(), state)
    assert insert.call_count == 1
    assert insert.call_args.kwargs["start_date"] == "2020-04-01"
    workflow.run_factor_refresh(args, window, object(), state)
    assert insert.call_count == 1, "Unchanged histories may resume"
    value = json.loads(manifest.read_text("utf-8"))
    value["receipts"].append({"report_date": "2021-03-31"})
    manifest.write_text(json.dumps(value), encoding="utf-8")
    workflow.run_factor_refresh(args, window, object(), state)
    assert insert.call_count == 2, "A receipt amendment invalidates the previous completion"


def test_history_snapshot_waits_for_matching_factor_rebuild(tmp_path, monkeypatch):
    args, _, _, _, _, delete = setup_refresh(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="financial history"):
        workflow.run_factor_snapshot_refresh(args, object())
    delete.assert_not_called()


def test_history_preparation_failure_preserves_native_rows(tmp_path, monkeypatch):
    args, state, window, _, insert, delete = setup_refresh(tmp_path, monkeypatch)
    insert.side_effect = ValueError("receipt evidence changed")
    with pytest.raises(ValueError, match="receipt evidence"):
        workflow.run_factor_refresh(args, window, object(), state)
    delete.assert_not_called()


def test_market_refresh_only_rebuilds_the_changed_issuers_history(tmp_path, monkeypatch):
    args, state, window, _, insert, delete = setup_refresh(tmp_path, monkeypatch)
    args.symbols = None
    workflow.run_factor_refresh(args, window, object(), state)
    assert insert.call_args.kwargs["stock_codes"] == ["035480"]
    assert insert.call_args.kwargs["dry_run"] is True
    assert delete.call_args.kwargs["symbols"] == ["035480"]


def test_changed_evidence_during_preparation_prevents_replacement(tmp_path, monkeypatch):
    args, state, window, manifest, insert, delete = setup_refresh(tmp_path, monkeypatch)
    def prepare(**kwargs):
        value = json.loads(manifest.read_text("utf-8"))
        value["receipts"].append({"report_date": "2021-03-31"})
        manifest.write_text(json.dumps(value), encoding="utf-8")
        return pd.DataFrame()
    insert.side_effect = prepare
    with pytest.raises(ValueError, match="changed before"):
        workflow.run_factor_refresh(args, window, object(), state)
    delete.assert_not_called()
