"""Audited market observations reach FactorLab without crossing later events."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from test_share_input_refresh import share_refresh_environment, factorlab_value

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def test_public_snapshot_replay_handles_year_gaps_and_a_partial_final_month(share_refresh_environment):
    lake, client, _, _ = share_refresh_environment
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    days = pd.to_datetime(["2024-01-02", "2024-01-03", "2025-01-02", "2025-01-03",
        "2026-09-01", "2026-09-04", "2026-09-07", "2026-09-08"])
    columns = ["security_id", "trade_date", "financial_basis", "factor_id", "factor_value",
        "fiscal_year", "financial_period", "currency", "updated_at"]
    version = pd.Timestamp("2026-09-10T12:00:00+09:00").to_pydatetime()
    rows = []
    for symbol, events in [
        ("999990", [("2024-01-02", 100.), ("2025-01-02", 200.), ("2026-09-01", 300.), ("2026-09-07", None)]),
        ("999980", [("2024-01-02", 10.), ("2026-09-01", 30.)]),
    ]:
        sid = "SEC_KR_" + symbol
        for day, value in events:
            rows.append([sid, pd.Timestamp(day).date(), "annual", "mcap_mil", value, None, None, "KRW", version])
        client.insert("price_daily", [(sid, day.date(), *([Decimal("10")] * 5), 50, "KRW") for day in days],
            column_names=["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "currency"])
    client.insert("fact_daily_factors", rows, column_names=columns)
    # Snapshots belonging to an intervening year or a later NULL observation
    # already exist. The selected restoration must preserve both boundaries.
    snapshots = []
    for day, value, source in [("2025-01-02", 200., "2025-01-02"), ("2025-01-03", 200., "2025-01-02"),
                               ("2026-09-07", None, "2026-09-07"), ("2026-09-08", None, "2026-09-07")]:
        snapshots.append(["SEC_KR_999990", pd.Timestamp(day).date(), "annual", "mcap_mil", value,
            None, None, "KRW", version, pd.Timestamp(source).date()])
    client.insert("fact_daily_factor_snapshot", snapshots, column_names=columns + ["source_trade_date"])
    assert factorlab_value(client, pd.Timestamp("2024-01-03"), "mcap_mil", table="fact_daily_factor_snapshot") == {}

    preparation = lake.silver("synthetic_native_publication")
    preparation.mkdir(parents=True)
    current = client.query_df("SELECT " + ",".join(columns) + " FROM fact_daily_factors")
    current.trade_date = pd.to_datetime(current.trade_date)
    records = []
    for year in (2024, 2026):
        for month in pd.period_range(f"{year}-01", f"{year}-12", freq="M"):
            if month.start_time > pd.Timestamp("2026-09-04"):
                continue
            folder = preparation / str(month)
            folder.mkdir()
            observed = current.loc[current.trade_date.between(month.start_time, min(month.end_time, pd.Timestamp("2026-09-04")))]
            for name, frame in [("expected.parquet", observed), ("native_before.parquet", observed),
                                ("insert_delta.parquet", observed.iloc[:0])]:
                frame.to_parquet(folder / name, index=False)
            records.append(dict(month=str(month), hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in folder.glob("*.parquet")}))
    manifest = preparation / "publication.json"
    manifest.write_text(json.dumps(dict(native_published=True, factor_ids=["mcap_mil", "csho"],
        source_years=[2024, 2026], end_date="2026-09-04", months=records, dependencies={})), "utf-8")
    output = lake.silver("snapshot_publication")
    program = """
import runpy, sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE = paths.DataLakePaths(Path(sys.argv[1]))
script = Path('scripts/research/publish_kr_historical_capitalization_snapshots.py').resolve()
sys.path.insert(0, str(script.parent))
sys.argv = [str(script), '--native-publication', sys.argv[2], '--output', sys.argv[3],
    '--publish-snapshots', '--source-scope', 'all']
runpy.run_path(str(script), run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root), str(manifest), str(output)],
        cwd=ROOT, env={**os.environ, "CLICKHOUSE_DATABASE": database}, capture_output=True,
        text=True, encoding="utf-8", timeout=120)
    logs = lake.silver("test_results", "snapshot_publication.log")
    logs.parent.mkdir(parents=True, exist_ok=True)
    logs.write_text(result.stdout + result.stderr, "utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    for day, expected in [
        ("2024-01-03", {"SEC_KR_999990": 100., "SEC_KR_999980": 10.}),
        ("2025-01-03", {"SEC_KR_999990": 200., "SEC_KR_999980": 10.}),
        ("2026-09-04", {"SEC_KR_999990": 300., "SEC_KR_999980": 30.}),
        ("2026-09-08", {"SEC_KR_999980": 30.}),
    ]:
        assert factorlab_value(client, pd.Timestamp(day), "mcap_mil", table="fact_daily_factor_snapshot") == expected
    report = json.loads((output / "publication.json").read_text("utf-8"))
    assert report["snapshots_published"] is True
    assert report["coverage_complete"] is False
