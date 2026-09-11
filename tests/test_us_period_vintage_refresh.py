"""Ordinary dated SEC files retain the newest period through actual reloads."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from api.repository.factor_lab_query import compile_factor_lab_graph
from test_share_input_refresh import share_refresh_environment
from test_historical_refresh_resume import us_refresh_environment

pytestmark = pytest.mark.integration


@pytest.fixture
def ordinary_us_refresh(us_refresh_environment, share_refresh_environment):
    lake, days, _, _ = us_refresh_environment
    _, client, _, _ = share_refresh_environment
    # The fixture owns this empty history. This case deliberately exercises the
    # ordinary SEC files used by the restored issuers, with actual publication dates.
    lake.silver("sec", "normalized", "history", "999990", "manifest.json").unlink()
    listings = lake.silver("survivorship", "us", "listing_episodes.json")
    listings.parent.mkdir(parents=True, exist_ok=True)
    listings.write_text(json.dumps(dict(schema_version=1, market="us", rows=[dict(
        symbol="999990", security_id="SEC_US_999990", issuer_id="synthetic-us-issuer",
        status="confirmed", security_type="common_stock")])), "utf-8")
    database = client.query("SELECT currentDatabase()").result_rows[0][0]

    def publish(records):
        facts, metadata = [], []
        for year, month, received, accounts in records:
            facts.extend(dict(canonical_account_id=name, statement_type="BS" if name != "REVENUE" else "IS",
                original_account_name=name, normalized_amount=value, fiscal_year=year, fiscal_month=month)
                for name, value in accounts.items())
            metadata.append(dict(stock_code="999990", fiscal_year=year, fiscal_month=month,
                report_date=received, period_end_date=str((pd.Timestamp(year, month, 1)+pd.offsets.MonthEnd()).date()),
                rcept_no=f"synthetic-{year}-{month}", source_type="statement"))
        pd.DataFrame(facts).to_csv(lake.silver("sec", "normalized", "us_normalized_999990.csv"), index=False)
        pd.DataFrame(metadata).to_csv(lake.silver("sec", "us_report_metadata.csv"), index=False)

    def run(mode, basis="quarterly", *, succeeds=True):
        program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))
from engine.workflows import refresh
args=refresh.build_arg_parser().parse_args(['--market','us','--targets',sys.argv[2],
    '--symbols','999980,999990','--end-date',sys.argv[3],'--financial-basis',sys.argv[4],
    '--workers','1','--resume','--resume-state-path',str(paths.DATA_LAKE.silver('refresh_state',sys.argv[2]+'_'+sys.argv[4]+'.json'))])
refresh.run_refresh(args)
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root), mode,
            days[-1].strftime("%Y%m%d"), basis], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "CLICKHOUSE_DATABASE": database}, text=True, encoding="utf-8", capture_output=True, timeout=120)
        assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
        return result.stdout + result.stderr

    def value(day, factor, basis="quarterly", snapshot=False):
        day = day.strftime("%Y-%m-%d")
        graph = {"version": 2, "experiment": {"name": "dated_sec_periods", "market": "US", "start_date": day, "end_date": day},
            "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
            "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
        compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=[day],
            factor_table="fact_daily_factor_snapshot" if snapshot else "fact_daily_factors", listing_table="security_listing_episodes")
        rows = client.query_df(compiled.query, parameters=compiled.parameters)
        return dict(zip(rows.security_id, rows.value)).get("SEC_US_999990") if len(rows) else None

    return lake, days, publish, run, value


@pytest.mark.parametrize("basis,month", [("annual", 12), ("quarterly", 9), ("ttm", 9)])
def test_late_old_sec_period_does_not_replace_the_newest_known_balance(ordinary_us_refresh, basis, month):
    _, days, publish, run, value = ordinary_us_refresh
    publish([(2018, month, days[-2].strftime("%Y-%m-%d"),
              {"TOTAL_ASSETS": 200, "TOTAL_EQUITY": 100, "LONG_TERM_DEBT": 10, "SHORT_TERM_DEBT": 0}),
             (2019, month, "2020-04-01",
              {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200, "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0})])
    run("factors", basis)
    assert value(days[-2], "debt_to_equity", basis) == .5
    assert value(days[-1], "debt_to_equity", basis) == .5
    run("snapshots", basis)
    assert value(days[-1], "debt_to_equity", basis, snapshot=True) == .5


@pytest.mark.parametrize("basis,expected", [("quarterly", .1), ("ttm", .35)])
def test_old_balance_enters_the_current_average_only_after_its_publication(ordinary_us_refresh, basis, expected):
    _, days, publish, run, value = ordinary_us_refresh
    publish([(2018, 9, days[-2].strftime("%Y-%m-%d"), {"TOTAL_ASSETS": 200, "REVENUE": 45}),
             (2018, 12, "2019-03-01", {"TOTAL_ASSETS": 250, "REVENUE": 60}),
             (2019, 3, "2019-05-01", {"TOTAL_ASSETS": 300, "REVENUE": 30}),
             (2019, 6, "2020-03-01", {"TOTAL_ASSETS": 350, "REVENUE": 60}),
             (2019, 9, "2020-04-01", {"TOTAL_ASSETS": 400, "REVENUE": 90})])
    run("factors", basis)
    # The prior-year balance is still unavailable on its publication day.
    assert value(days[-2], "asset_turnover", basis) is None
    # Q3 revenue 90 - Q2 YTD 60 = 30; average assets (400 + 200) / 2 = 300.
    # TTM revenue is Q4 15 + Q1 30 + Q2 30 + Q3 30 = 105; 105 / 300 = .35.
    assert value(days[-1], "asset_turnover", basis) == pytest.approx(expected)
    run("snapshots", basis)
    assert value(days[-2], "asset_turnover", basis, snapshot=True) is None
    assert value(days[-1], "asset_turnover", basis, snapshot=True) == pytest.approx(expected)
