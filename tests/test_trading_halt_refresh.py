"""Reviewed halt sources reach the public refresh command and actual backtest."""
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from api.service.backtest_service import BacktestService
from api.service.dto import FactorBacktestRequestDto, FactorConditionDto
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DataLakePaths

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def halt_refresh_environment(tmp_path):
    lake = DataLakePaths(tmp_path / "data-lake")
    database = "arcana_test_halt_refresh_" + uuid4().hex
    admin = get_clickhouse_client(connect_timeout=3, send_receive_timeout=30)
    production = admin.query("SELECT currentDatabase()").result_rows[0][0]
    assert production.replace("_", "").isalnum()
    admin.command(f"CREATE DATABASE {database}")
    client = None
    try:
        for table in ("price_daily", "fact_daily_factors", "fact_daily_factor_snapshot", "factor_catalog", "security_master",
                      "issuers", "identifiers", "benchmark_price_daily"):
            admin.command(f"CREATE TABLE {database}.{table} AS {production}.{table}")
        client = get_clickhouse_client(database=database, connect_timeout=3, send_receive_timeout=30)
        days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 30), date(2026, 2, 2),
                date(2026, 2, 3), date(2026, 2, 4), date(2026, 2, 5)]
        for symbol, prices in (("999990", [100, 100, 100, 500, 900, 110, 110]),
                               ("999980", [100, 100, 100, 100, 200, 300, 300])):
            sid = "SEC_KR_" + symbol
            client.insert("security_master", [(sid, symbol, "KR", True, "KOSPI")],
                          column_names=["security_id", "issuer_id", "country", "is_active", "exchange_code"])
            client.insert("issuers", [(symbol, "Synthetic halt fixture")], column_names=["issuer_id", "legal_name_en"])
            client.insert("price_daily", [(sid, day, *([Decimal(value)] * 5), 100, "KRW") for day, value in zip(days, prices)],
                          column_names=["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "currency"])
            client.insert("fact_daily_factors", [
                (sid, date(2026, 1, 2), "roe", 10. if symbol == "999990" else 1.),
                (sid, date(2026, 1, 30), "roe", 1. if symbol == "999990" else 10.),
            ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
        client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                      column_names=["factor_id", "factor_name", "value_direction"])

        def run(manifest, *, succeeds=True):
            program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE = paths.DataLakePaths(Path(sys.argv[1]))
from engine.workflows import refresh
args = refresh.build_arg_parser().parse_args(['--market','kr','--targets','survivorship',
    '--end-date','2026-02-05','--survivorship-manifest',sys.argv[2],
    '--survivorship-no-download','--no-resume'])
refresh.run_refresh(args)
"""
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root), str(manifest)],
                cwd=ROOT, env={**os.environ, "CLICKHOUSE_DATABASE": database}, capture_output=True,
                text=True, encoding="utf-8", timeout=60)
            assert (completed.returncode == 0) == succeeds, completed.stdout + completed.stderr
            return completed.stdout + completed.stderr

        yield lake, client, run
    finally:
        if client is not None:
            client.close()
        assert database.startswith("arcana_test_halt_refresh_")
        admin.command(f"DROP DATABASE {database}")
        admin.close()


def reviewed_halt(lake, *, end_date="2026-02-04"):
    source = lake.bronze("dart", "synthetic_halt.html")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Synthetic DART fixture: common shares listed in 2000; trading halted Feb 2, 2026. "
        + (f"Confirmed resumption: {end_date}." if end_date else "Resumption has not been confirmed."), "utf-8")
    sources = [dict(source_id="dart-halt", provider="DART", published_date="2026-01-30",
        path=source.relative_to(lake.root).as_posix(), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260130000001")]
    reviewed = dict(schema_version=1, market="kr", review_status="verified", source_root="../..", sources=sources,
        listing_episodes=[dict(episode_id="listing-"+symbol, security_id="SEC_KR_"+symbol, issuer_id=symbol,
            symbol=symbol, country="KR", exchange_code="KOSPI", security_type="common_stock", status="confirmed",
            valid_from="2000-01-01", valid_until=None, published_date="2026-01-30", source_ids=["dart-halt"])
            for symbol in ("999990", "999980")], events=[], entitlements=[], trading_halts=[dict(
                halt_id="confirmed-halt", security_id="SEC_KR_999990", start_date="2026-02-02", end_date=end_date,
                published_date="2026-01-30", status="confirmed", source_ids=["dart-halt"])])
    manifest = lake.silver("reviews", "halts.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(reviewed), "utf-8")
    return manifest, reviewed


def backtest(client):
    return BacktestService(client_factory=lambda: client).run_factor_backtest(FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 2, 5), rebalance_frequency="monthly", market="kr",
        max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors"))


def test_reviewed_halt_refresh_reaches_backtest_and_retains_separate_listing_lifetime(halt_refresh_environment):
    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    run(manifest)
    result = backtest(client)
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx([1, 1, 1, 1, 1.1, 1.1])
    gold = lake.gold("survivorship", "kr")
    halt, = json.loads((gold / "trading_halts.json").read_text("utf-8"))["rows"]
    assert halt["start_date"] == "2026-02-02" and halt["end_date"] == "2026-02-04"
    assert halt["source_sha256"] == reviewed["sources"][0]["source_sha256"]
    assert all(row["valid_until"] is None for row in json.loads((gold / "listing_episodes.json").read_text("utf-8"))["rows"])
    halted = result.raw["portfolio_history"][3]["positions"][0]
    assert halted["trading_status"] == "halted" and halted["mark_date"] == date(2026, 1, 30)
    assert halted["market_value"] == 1
    assert '"status": "unchanged"' in run(manifest)


def test_factorlab_uses_tradable_population_before_top70_ranking(halt_refresh_environment):
    from api.service.dto import FactorLabGraphDto, FactorScreenRequestDto
    from api.service.factor_lab_service import FactorLabService
    from api.service.factor_screen_service import FactorScreenService

    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    for symbol in ("999970", "999960", "999950"):
        sid = "SEC_KR_" + symbol
        client.insert("security_master", [(sid, symbol, "KR", True, "KOSPI")],
                      column_names=["security_id", "issuer_id", "country", "is_active", "exchange_code"])
        reviewed["listing_episodes"].append(dict(episode_id="listing-"+symbol, security_id=sid, issuer_id=symbol,
            symbol=symbol, country="KR", exchange_code="KOSPI", security_type="common_stock", status="confirmed",
            valid_from="2000-01-01", valid_until=None, published_date="2026-01-30", source_ids=["dart-halt"]))
    manifest.write_text(json.dumps(reviewed), "utf-8")
    run(manifest)
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    screen_service = FactorScreenService(client_factory=lambda: get_clickhouse_client(
        database=database, connect_timeout=3, send_receive_timeout=30))
    client.insert("factor_catalog", [("mcap_mil", "Market cap", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    for day in (date(2026, 2, 2), date(2026, 2, 4)):
        client.insert("fact_daily_factors", [("SEC_KR_"+symbol, day, "mcap_mil", float(cap), "KRW")
            for symbol, cap in (("999990", 500), ("999970", 400), ("999960", 300), ("999980", 200), ("999950", 100))],
            column_names=["security_id", "trade_date", "factor_id", "factor_value", "currency"])
    for day, expected in (("2026-02-02", {"SEC_KR_999970", "SEC_KR_999960", "SEC_KR_999980"}),
                          ("2026-02-04", {"SEC_KR_999990", "SEC_KR_999970", "SEC_KR_999960", "SEC_KR_999980"})):
        graph = FactorLabGraphDto.model_validate(dict(version=2,
            experiment=dict(name="halt-aware universe", market="KR", start_date=day, end_date=day,
                universe=dict(size_percentile=dict(side="top", percent=70))),
            nodes=[dict(id="input", type="factor_input", version=1,
                config=dict(factor_id="mcap_mil", financial_basis="annual", missing_policy="drop"))],
            edges=[], outputs=dict(final_node_id="input", evaluation_node_ids=[])))
        compiled = FactorLabService(client_factory=lambda: client).compile_graph(graph)
        rows = client.query_df(compiled.query, parameters=compiled.parameters)
        assert set(rows.security_id) == expected
        screened = screen_service.screen_stocks(FactorScreenRequestDto(
            conditions=[FactorConditionDto(factor_id="mcap_mil", mode="top_percent", top_percent=100)],
            universe=dict(size_percentile=dict(side="top", percent=70)),
            as_of_date=date.fromisoformat(day), market="KR"))
        assert {row.security_id for row in screened.rows} == expected
        assert screened.universe_summary["dates"][0]["after_count"] == len(expected)
        assert screened.universe_summary["dates"][0]["before_count"] == (4 if day == "2026-02-02" else 5)


def test_backtest_selects_available_stock_instead_of_halted_highest_factor(halt_refresh_environment):
    lake, client, run = halt_refresh_environment
    manifest, _ = reviewed_halt(lake)
    run(manifest)
    client.insert("fact_daily_factors", [
        ("SEC_KR_999990", date(2026, 2, 2), "roe", 50.),
        ("SEC_KR_999980", date(2026, 2, 2), "roe", 1.),
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    result = BacktestService(client_factory=lambda: client).run_factor_backtest(FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 2, 3), end_date=date(2026, 2, 5), rebalance_frequency="monthly", market="kr",
        max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors"))
    # The halt was already effective on the signal date. NEW is bought at 200
    # and rises to 300; the unavailable high-ROE old stock is not a cash slot.
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx([1, 1.5, 1.5])
    assert result.rebalance_history[0].positions[0].security_id == "SEC_KR_999980"


def test_factorlab_frozen_evaluation_uses_the_same_halt_filtered_scores_as_the_run(halt_refresh_environment):
    from api.service.dto import FactorLabExperimentSaveRequestDto, FactorLabGraphDto, FactorLabRunRequestDto
    from api.service.factor_lab_service import FactorLabService
    from api.service.factor_lab_evaluation_service import FactorLabEvaluationService

    lake, client, run = halt_refresh_environment
    manifest, _ = reviewed_halt(lake)
    run(manifest)
    for symbol, values in (("999990", [42., 242.]), ("999980", [2., 3.])):
        client.insert("fact_daily_factor_snapshot", [(date(2026, 2, day), "SEC_KR_"+symbol, "roe", "annual",
            value, date(2026, 2, day), date(2025, 12, 31)) for day, value in zip((2, 3), values)],
            column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value",
                          "source_trade_date", "financial_period"])
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    factory = lambda: get_clickhouse_client(database=database, connect_timeout=3, send_receive_timeout=30)
    graph = FactorLabGraphDto.model_validate(dict(version=2,
        experiment=dict(name="same frozen investable score", market="KR", start_date="2026-02-02",
            end_date="2026-02-02", factor_data_mode="point_in_time_snapshot"),
        nodes=[dict(id="score", type="factor_input", version=1, config=dict(factor_id="roe")),
               dict(id="future", type="forward_outcome", version=1, config=dict(target_factor_id="roe",
                    financial_basis="annual", measure="change", horizons=[1], unit="trading_day",
                    bucket_count=2, score_order="higher"))],
        edges=[dict(source="score", target="future", target_handle="score")],
        outputs=dict(final_node_id="score", evaluation_node_ids=["future"])))
    service = FactorLabService(client_factory=factory)
    saved = service.save_experiment(FactorLabExperimentSaveRequestDto(graph=graph))
    result = service.run_graph(FactorLabRunRequestDto(experiment_id=saved.experiment_id, mode="screen",
        evaluation_as_of=date(2026, 2, 3)))
    assert {row.security_id for row in result.rows} == {"SEC_KR_999980"}
    assert result.evaluation is not None, result.warnings
    baseline = result.evaluation["evaluations"][0]["horizons"][0]["baseline"]
    assert baseline["opportunity_count"] == baseline["valid_count"] == 1
    assert baseline["mean"] == 1
    assert FactorLabEvaluationService(client_factory=factory).list_evaluations(result.run_id)["evaluations"][0] == result.evaluation


@pytest.mark.parametrize("snapshot", [False, True], ids=["native", "snapshot"])
def test_factor_screen_ranks_only_available_stocks_after_reviewed_halt_refresh(halt_refresh_environment, snapshot):
    from api.service.dto import FactorScreenRequestDto
    from api.service.factor_screen_service import FactorScreenService

    lake, client, run = halt_refresh_environment
    manifest, _ = reviewed_halt(lake)
    run(manifest)
    client.insert("fact_daily_factors", [
        ("SEC_KR_999990", date(2026, 2, 2), "roe", 50.),
        ("SEC_KR_999980", date(2026, 2, 2), "roe", 1.),
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    if snapshot:
        client.insert("fact_daily_factor_snapshot", [(date(2026, 2, 2), "SEC_KR_" + symbol, "roe", "annual",
            value, date(2026, 2, 2)) for symbol, value in (("999990", 50.), ("999980", 1.))],
            column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date"])
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    service = FactorScreenService(client_factory=lambda: get_clickhouse_client(
        database=database, connect_timeout=3, send_receive_timeout=30))
    result = service.screen_stocks(FactorScreenRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=50)],
        as_of_date=date(2026, 2, 2), market="KR"))
    # OLD has the highest factor but cannot be bought on this closing session.
    # Ranking the single available stock must select NEW, not return no stocks.
    assert [row.security_id for row in result.rows] == ["SEC_KR_999980"]
    assert result.total_count == 1


@pytest.mark.parametrize("snapshot", [False, True], ids=["native", "snapshot"])
def test_factor_screen_restores_historical_listing_only_during_its_confirmed_lifetime(halt_refresh_environment, snapshot):
    from api.service.dto import FactorScreenRequestDto
    from api.service.factor_screen_service import FactorScreenService

    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    reviewed["listing_episodes"].append(dict(reviewed["listing_episodes"][0],
        episode_id="former-listing", security_id="SEC_KR_999970", issuer_id="999970", symbol="999970",
        valid_from="2026-02-02", valid_until="2026-02-04"))
    manifest.write_text(json.dumps(reviewed), "utf-8")
    run(manifest)
    client.insert("issuers", [("999970", "Synthetic former issuer")],
        column_names=["issuer_id", "legal_name_en"])
    # The former listing is deliberately absent from the current master. Its
    # vendor factor survives both before IPO and after the verified removal.
    client.insert("fact_daily_factors", [("SEC_KR_" + symbol, date(2026, 2, day), "roe", value)
        for day in (1, 2, 4) for symbol, value in (("999970", 100.), ("999990", 50.), ("999980", 1.))],
        column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    if snapshot:
        client.insert("fact_daily_factor_snapshot", [(date(2026, 2, day), "SEC_KR_" + symbol, "roe", "annual",
            value, date(2026, 2, day)) for day in (1, 2, 4)
            for symbol, value in (("999970", 100.), ("999990", 50.), ("999980", 1.))],
            column_names=["trade_date", "security_id", "factor_id", "financial_basis", "factor_value", "source_trade_date"])
    database = client.query("SELECT currentDatabase()").result_rows[0][0]
    service = FactorScreenService(client_factory=lambda: get_clickhouse_client(
        database=database, connect_timeout=3, send_receive_timeout=30))
    for day, expected in ((1, "999990"), (2, "999970"), (4, "999990")):
        result = service.screen_stocks(FactorScreenRequestDto(
            conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=50)],
            as_of_date=date(2026, 2, day), market="KR"))
        assert [row.security_id for row in result.rows] == ["SEC_KR_" + expected]
        assert result.rows[0].exchange_code == "KOSPI"


def test_closed_listing_without_terminal_rights_cannot_be_sold_at_vendor_price(halt_refresh_environment):
    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    reviewed["trading_halts"] = []
    reviewed["listing_episodes"][0]["valid_until"] = "2026-01-30"
    # The exchange removal is confirmed; private-share or cash recovery is not.
    # No settlement event has been approved, although vendor quotes continue.
    manifest.write_text(json.dumps(reviewed), "utf-8")
    run(manifest)
    exported = json.loads(lake.gold("survivorship", "kr", "listing_episodes.json").read_text("utf-8"))["rows"]
    assert next(row for row in exported if row["security_id"] == "SEC_KR_999990")["valid_until"] == "2026-01-30"
    with pytest.raises(ValueError, match="Unresolved terminal outcome for listing"):
        backtest(client)


@pytest.mark.parametrize(("outcome", "expected"), [
    ("sold_before_closure", [1, 1, 5, 10, 15, 15]),
    ("confirmed_cash", [1, 1.2, 1.2, 1.2, 1.2, 1.2]),
    ("continuous_listing", [1, 1, 5, 10, 15, 15]),
    ("closed_before_entry", [1, 1, 1, 2, 3, 3]),
])
def test_listing_closure_checks_only_remaining_unresolved_exposure(halt_refresh_environment, outcome, expected):
    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    reviewed["trading_halts"] = []
    old = reviewed["listing_episodes"][0]
    old["valid_until"] = "2026-01-30"
    if outcome == "sold_before_closure":
        old["valid_until"] = "2026-02-03"
    elif outcome == "closed_before_entry":
        old["valid_until"] = "2026-01-05"
    elif outcome == "continuous_listing":
        reviewed["listing_episodes"].append(dict(old, episode_id="next-exchange-listing",
            valid_from="2026-01-30", valid_until=None, exchange_code="KOSDAQ"))
    else:
        reviewed["events"] = [dict(event_id="confirmed-cash", security_id=old["security_id"],
            event_type="cash_merger", effective_date="2026-01-30", cash_per_share=120.,
            cash_payment_date="2026-02-04", currency="KRW", status="confirmed",
            published_date="2026-01-30", source_ids=["dart-halt"], entitlements_complete=True)]
    manifest.write_text(json.dumps(reviewed), "utf-8")
    run(manifest)
    result = backtest(client)
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx(expected)


def test_unknown_halt_end_is_not_inferred_from_a_later_vendor_quote(halt_refresh_environment):
    lake, client, run = halt_refresh_environment
    manifest, _ = reviewed_halt(lake, end_date=None)
    run(manifest)
    result = backtest(client)
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx([1, 1, 1, 1, 1, 1])
    last = result.raw["portfolio_history"][-1]["positions"][0]
    assert last["trading_status"] == "halted" and last["mark_date"] == date(2026, 1, 30)
    halt, = json.loads(lake.gold("survivorship", "kr", "trading_halts.json").read_text("utf-8"))["rows"]
    assert halt["end_date"] is None


def test_conflicting_halt_review_preserves_published_files_and_backtest(halt_refresh_environment):
    lake, client, run = halt_refresh_environment
    manifest, reviewed = reviewed_halt(lake)
    run(manifest)
    gold = lake.gold("survivorship", "kr")
    previous = {path.name: path.read_bytes() for path in gold.iterdir() if path.is_file()}
    reviewed["trading_halts"].append({**reviewed["trading_halts"][0], "halt_id": "conflicting-halt",
                                    "start_date": "2026-02-03", "end_date": "2026-02-05"})
    manifest.write_text(json.dumps(reviewed), "utf-8")
    assert "Overlapping trading halt" in run(manifest, succeeds=False)
    assert {path.name: path.read_bytes() for path in gold.iterdir() if path.is_file()} == previous
    assert backtest(client).equity_curve[-1].strategy_nav == pytest.approx(1.1)
