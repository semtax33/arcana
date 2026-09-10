"""Execute the public snapshot SQL against isolated ClickHouse tables."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from engine.loaders.factors import prepare_daily_factor_rows
from engine.loaders.factor_snapshots import build_factor_snapshot_insert_query, build_incremental_factor_snapshot_insert_query, ensure_factor_snapshot_table

pytestmark = pytest.mark.skipif(os.environ.get("ARCANA_TEST_CLICKHOUSE") != "1", reason="Explicit local ClickHouse integration run")


@contextmanager
def snapshot_tables():
    database = "arcana_test_snapshot_" + uuid4().hex
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
    client.command(f"CREATE DATABASE {database}")
    try:
        client.command(f"""CREATE TABLE {database}.source (
            security_id String, trade_date Date, financial_basis String, factor_id String,
            factor_value Nullable(Float64), fiscal_year Nullable(UInt16), financial_period Nullable(Date),
            currency String, updated_at DateTime64(3, 'Asia/Seoul')) ENGINE=Memory""")
        ensure_factor_snapshot_table(client, snapshot_table=f"{database}.snapshots")
        yield client, database
    finally:
        client.command(f"DROP DATABASE {database}")
        client.close()


def test_snapshot_regeneration_keeps_metadata_from_the_selected_source_row():
    with snapshot_tables() as (client, database):
        stamp = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        columns = ["security_id", "trade_date", "financial_basis", "factor_id", "factor_value",
            "fiscal_year", "financial_period", "currency", "updated_at"]
        client.insert(f"{database}.source", [
            ["SEC_KR_003410", date(2020, 1, 2), "annual", "na_20", 100., 2019, date(2019, 12, 31), "KRW", stamp],
            ["SEC_KR_003410", date(2020, 1, 3), "annual", "na_20", 200., None, None, "KRW", stamp],
            ["SEC_KR_003410", date(2020, 1, 6), "annual", "na_20", 400., 2020, date(2020, 3, 31), "KRW", stamp],
            ["SEC_KR_005930", date(2020, 1, 2), "annual", "na_20", 999., 2019, date(2019, 12, 31), "KRW", stamp],
        ], column_names=columns)
        query, parameters = build_factor_snapshot_insert_query(market="kr", financial_basis="annual",
            factor_ids=["na_20"], security_ids=["SEC_KR_003410"], snapshot_dates=["2020-01-02", "2020-01-03"],
            source_table=f"{database}.source", snapshot_table=f"{database}.snapshots")
        client.command(query, parameters=parameters)
        actual = client.query(f"""SELECT trade_date, security_id, factor_value, source_trade_date,
            fiscal_year, financial_period FROM {database}.snapshots ORDER BY trade_date""").result_rows
        assert actual == [
            (date(2020, 1, 2), "SEC_KR_003410", 100., date(2020, 1, 2), 2019, date(2019, 12, 31)),
            (date(2020, 1, 3), "SEC_KR_003410", 200., date(2020, 1, 3), None, None),
        ]


def test_incremental_snapshots_keep_revised_null_metadata_for_new_and_carried_values():
    with snapshot_tables() as (client, database):
        # Retain both versions deterministically rather than depending on the
        # background merge timing of ReplacingMergeTree in this fixture.
        client.command(f"CREATE TABLE {database}.snapshot_versions AS {database}.snapshots ENGINE=Memory")
        stamp = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        source_columns = ["security_id", "trade_date", "financial_basis", "factor_id", "factor_value",
            "fiscal_year", "financial_period", "currency", "updated_at"]
        client.insert(f"{database}.source", [
            ["SEC_KR_003410", date(2020, 1, 6), "annual", "na_20", 300., 2019, date(2019, 12, 31), "KRW", stamp],
            ["SEC_KR_003410", date(2020, 1, 6), "annual", "na_20", 400., None, None, "KRW", stamp + timedelta(seconds=1)],
        ], column_names=source_columns)
        snapshot_columns = source_columns + ["source_trade_date"]
        client.insert(f"{database}.snapshot_versions", [
            ["SEC_KR_035480", date(2020, 1, 3), "annual", "na_20", 100., 2019, date(2019, 12, 31), "KRW", stamp, date(2020, 1, 2)],
            ["SEC_KR_035480", date(2020, 1, 3), "annual", "na_20", 200., None, None, "KRW", stamp + timedelta(seconds=1), date(2020, 1, 3)],
        ], column_names=snapshot_columns)
        query, parameters = build_incremental_factor_snapshot_insert_query(market="kr", financial_basis="annual",
            factor_ids=["na_20"], security_ids=["SEC_KR_003410", "SEC_KR_035480"],
            snapshot_date="2020-01-06", previous_snapshot_date="2020-01-03",
            source_table=f"{database}.source", snapshot_table=f"{database}.snapshot_versions")
        client.command(query, parameters=parameters)
        actual = client.query(f"""SELECT security_id, factor_value, source_trade_date, fiscal_year, financial_period
            FROM {database}.snapshot_versions WHERE trade_date='2020-01-06' ORDER BY security_id""").result_rows
        assert actual == [
            ("SEC_KR_003410", 400., date(2020, 1, 6), None, None),
            ("SEC_KR_035480", 200., date(2020, 1, 3), None, None),
        ]


@pytest.mark.parametrize("explicit_timezone", [False, True])
def test_factor_preparation_preserves_local_revision_time_through_snapshot_publication(explicit_timezone):
    with snapshot_tables() as (client, database):
        # Financial calculation output historically represents Seoul wall time
        # without tzinfo. Publishing it must preserve the intended instant.
        stamp = datetime(2026, 9, 10, 15, 23, 18)
        if explicit_timezone:
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        prepared = prepare_daily_factor_rows(pd.DataFrame([dict(
            security_id="SEC_KR_003410", trade_date=date(2020, 1, 2),
            na_20=100., updated_at=stamp)]), factor_ids=["na_20"])
        client.insert_df(f"{database}.source", prepared)
        native = client.query(f"SELECT updated_at FROM {database}.source").result_rows
        assert native == [(stamp.replace(tzinfo=ZoneInfo("Asia/Seoul")),)]
        query, parameters = build_factor_snapshot_insert_query(market="kr", financial_basis="annual",
            factor_ids=["na_20"], security_ids=["SEC_KR_003410"], snapshot_dates=["2020-01-02"],
            source_table=f"{database}.source", snapshot_table=f"{database}.snapshots")
        client.command(query, parameters=parameters)
        actual = client.query(f"SELECT updated_at FROM {database}.snapshots").result_rows
        assert actual == [(stamp.replace(tzinfo=ZoneInfo("Asia/Seoul")),)]


@pytest.mark.parametrize("missing_value", [None, float("nan")], ids=["null", "nan"])
@pytest.mark.parametrize("mode", ["carry", "incremental", "raw"])
def test_snapshot_abstention_replaces_prior_value_until_evidenced_recovery(mode, missing_value):
    with snapshot_tables() as (client, database):
        stamp = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        columns = ["security_id", "trade_date", "financial_basis", "factor_id", "factor_value",
            "fiscal_year", "financial_period", "currency", "updated_at"]
        # The same-date old version must not survive the newer abstention.
        client.insert(f"{database}.source", [
            ["SEC_KR_003410", date(2020, 1, 2), "quarterly", "ar_days", 25., 2019, date(2019, 9, 30), "KRW", stamp],
            ["SEC_KR_003410", date(2020, 1, 3), "quarterly", "ar_days", 50., 2019, date(2019, 9, 30), "KRW", stamp],
            ["SEC_KR_003410", date(2020, 1, 3), "quarterly", "ar_days", missing_value, 2019, date(2019, 12, 31), "KRW", stamp + timedelta(seconds=1)],
            ["SEC_KR_003410", date(2020, 1, 7), "quarterly", "ar_days", 30., 2020, date(2020, 3, 31), "KRW", stamp + timedelta(seconds=2)],
        ], column_names=columns)
        scope = dict(market="kr", financial_basis="quarterly", factor_ids=["ar_days"],
            security_ids=["SEC_KR_003410"], source_table=f"{database}.source",
            snapshot_table=f"{database}.snapshots")
        days = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
        if mode == "incremental":
            query, parameters = build_factor_snapshot_insert_query(**scope, snapshot_dates=days[:1])
            client.command(query, parameters=parameters)
            for previous, current in zip(days, days[1:]):
                query, parameters = build_incremental_factor_snapshot_insert_query(**scope,
                    snapshot_date=current, previous_snapshot_date=previous)
                client.command(query, parameters=parameters)
        else:
            query, parameters = build_factor_snapshot_insert_query(**scope,
                start_date=days[0], end_date=days[-1], snapshot_dates=days,
                carry_forward=mode == "carry")
            client.command(query, parameters=parameters)
        actual = client.query(f"""SELECT trade_date,
                if(isFinite(tupleElement(selected, 1)), tupleElement(selected, 1), NULL),
                tupleElement(selected, 2), tupleElement(selected, 3)
            FROM (SELECT trade_date, argMax(tuple(factor_value, source_trade_date, financial_period), updated_at) AS selected
                FROM {database}.snapshots GROUP BY trade_date) ORDER BY trade_date""").result_rows
        expected = [
            (date(2020, 1, 2), 25., date(2020, 1, 2), date(2019, 9, 30)),
            (date(2020, 1, 3), None, date(2020, 1, 3), date(2019, 12, 31)),
            (date(2020, 1, 6), None, date(2020, 1, 3), date(2019, 12, 31)),
            (date(2020, 1, 7), 30., date(2020, 1, 7), date(2020, 3, 31)),
        ]
        if mode == "raw":
            expected = [row for row in expected if row[0] != date(2020, 1, 6)]
        assert actual == expected

        client.command(f"CREATE TABLE {database}.prices (security_id String, trade_date Date) ENGINE=Memory")
        client.command(f"CREATE TABLE {database}.securities (security_id String, issuer_id String, country String, updated_at DateTime) ENGINE=Memory")
        client.command(f"CREATE TABLE {database}.issuers (issuer_id String, sector_code String, industry_group_code String, updated_at DateTime) ENGINE=Memory")
        client.insert(f"{database}.prices", [["SEC_KR_003410", date.fromisoformat(day)] for day in days])
        client.insert(f"{database}.securities", [["SEC_KR_003410", "issuer", "KR", stamp]])
        client.insert(f"{database}.issuers", [["issuer", "industrials", "industrial", stamp]])
        graph = {"version": 2, "experiment": {"name": "abstention", "market": "KR",
            "start_date": days[0], "end_date": days[-1], "factor_data_mode": "point_in_time_snapshot"},
            "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                "config": {"factor_id": "ar_days", "financial_basis": "quarterly", "missing_policy": "drop"}}],
            "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
        compiled = compile_factor_lab_graph(graph, known_factor_ids={"ar_days"}, trade_dates=days,
            factor_table=f"{database}.snapshots", price_table=f"{database}.prices",
            security_table=f"{database}.securities", issuer_table=f"{database}.issuers")
        observed = client.query_df(compiled.query, parameters=compiled.parameters).sort_values("trade_date")
        assert pd.to_datetime(observed.trade_date).dt.date.tolist() == [date(2020, 1, 2), date(2020, 1, 7)]
        assert observed.is_valid.all()
        assert observed.value.tolist() == [25., 30.]


def test_daily_preparation_emits_abstention_boundaries_without_filling_missing_days():
    with snapshot_tables() as (client, database):
        days = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"]
        rows = [dict(security_id=sid, trade_date=day, ar_days=value)
            for sid, values in [("SEC_KR_003410", [25., None, None, 30., None]),
                                ("SEC_KR_035480", [None, None, 10., float("inf"), None])]
            for day, value in zip(days, values)]
        wide = pd.DataFrame(rows[::-1])  # Input order and issuer boundaries cannot affect state.
        prepared = prepare_daily_factor_rows(wide, factor_ids=["ar_days"], include_abstentions=True)
        client.insert_df(f"{database}.source", prepared)
        # Initial absence and the first day of each finite-to-missing transition
        # are retained; repeated missing days require no additional source rows.
        actual = client.query(f"""SELECT security_id, trade_date, ifNull(isFinite(factor_value), false)
            FROM {database}.source ORDER BY security_id, trade_date""").result_rows
        assert actual == [
            ("SEC_KR_003410", date(2020, 1, 2), 1), ("SEC_KR_003410", date(2020, 1, 3), 0),
            ("SEC_KR_003410", date(2020, 1, 7), 1), ("SEC_KR_003410", date(2020, 1, 8), 0),
            ("SEC_KR_035480", date(2020, 1, 2), 0), ("SEC_KR_035480", date(2020, 1, 6), 1),
            ("SEC_KR_035480", date(2020, 1, 7), 0),
        ]
        query, parameters = build_factor_snapshot_insert_query(market="kr", financial_basis="annual",
            factor_ids=["ar_days"], snapshot_dates=days,
            source_table=f"{database}.source", snapshot_table=f"{database}.snapshots")
        client.command(query, parameters=parameters)
        states = client.query(f"""SELECT security_id, groupArray(valid) FROM (
            SELECT security_id, trade_date, ifNull(isFinite(factor_value), false) AS valid
            FROM {database}.snapshots ORDER BY security_id, trade_date) GROUP BY security_id ORDER BY security_id""").result_rows
        assert states == [("SEC_KR_003410", [1, 0, 0, 1, 0]), ("SEC_KR_035480", [0, 0, 1, 0, 0])]
