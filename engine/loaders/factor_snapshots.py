from __future__ import annotations

import argparse
from datetime import date, timedelta
from typing import Any

from engine.core.clickhouse import get_clickhouse_client


FACTOR_SNAPSHOT_TABLE = "fact_daily_factor_snapshot"
FACTOR_SOURCE_TABLE = "fact_daily_factors"
PRICE_TABLE = "price_daily"
DEFAULT_DATE_BATCH_SIZE = 1
DEFAULT_FACTOR_CHUNK_SIZE = 16
DEFAULT_MAX_THREADS = 2

FACTOR_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS {snapshot_table}
(
    trade_date Date,
    security_id String,
    factor_id LowCardinality(String),
    financial_basis LowCardinality(String) DEFAULT 'annual',
    factor_value Nullable(Float64),
    source_trade_date Date,
    fiscal_year Nullable(UInt16),
    financial_period Nullable(Date),
    currency LowCardinality(String) DEFAULT 'KRW',
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, factor_id, financial_basis, security_id)
SETTINGS index_granularity = 8192
""".strip()


def ensure_factor_snapshot_table(
    client: Any | None = None,
    *,
    snapshot_table: str = FACTOR_SNAPSHOT_TABLE,
) -> None:
    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        _execute(client, FACTOR_SNAPSHOT_DDL.format(snapshot_table=_validate_table_name(snapshot_table)))
    finally:
        if owns_client:
            client.close()


def insert_factor_snapshots(
    *,
    market: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    financial_basis: str | None = None,
    factor_ids: list[str] | None = None,
    source_table: str = FACTOR_SOURCE_TABLE,
    snapshot_table: str = FACTOR_SNAPSHOT_TABLE,
    price_table: str = PRICE_TABLE,
    carry_forward: bool = True,
    incremental: bool = True,
    date_batch_size: int = DEFAULT_DATE_BATCH_SIZE,
    factor_chunk_size: int = DEFAULT_FACTOR_CHUNK_SIZE,
    max_lookback_days: int | None = None,
    max_threads: int | None = DEFAULT_MAX_THREADS,
    truncate: bool = False,
    client: Any | None = None,
) -> int:
    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        ensure_factor_snapshot_table(client, snapshot_table=snapshot_table)
        if truncate:
            if market is None:
                _execute(client, f"TRUNCATE TABLE {_validate_table_name(snapshot_table)}")
            else:
                _delete_market_snapshots(
                    client,
                    market=market,
                    start_date=start_date,
                    end_date=end_date,
                    financial_basis=financial_basis,
                    factor_ids=factor_ids,
                    snapshot_table=snapshot_table,
                )
        if carry_forward:
            insert_func = (
                _insert_factor_snapshots_incremental
                if incremental
                else _insert_factor_snapshots_chunked
            )
            insert_func(
                client,
                market=market,
                start_date=start_date,
                end_date=end_date,
                financial_basis=financial_basis,
                factor_ids=factor_ids,
                source_table=source_table,
                snapshot_table=snapshot_table,
                price_table=price_table,
                date_batch_size=date_batch_size,
                factor_chunk_size=factor_chunk_size,
                max_lookback_days=max_lookback_days,
                max_threads=max_threads,
                truncate=truncate,
            )
        else:
            query, params = build_factor_snapshot_insert_query(
                start_date=start_date,
                end_date=end_date,
                market=market,
                financial_basis=financial_basis,
                factor_ids=factor_ids,
                source_table=source_table,
                snapshot_table=snapshot_table,
                price_table=price_table,
                carry_forward=False,
            )
            _execute(client, query, params, settings=_memory_safe_settings(max_threads))
        return _count_inserted_rows(
            client,
            market=market,
            start_date=start_date,
            end_date=end_date,
            financial_basis=financial_basis,
            factor_ids=factor_ids,
            snapshot_table=snapshot_table,
        )
    finally:
        if owns_client:
            client.close()


def build_factor_snapshot_insert_query(
    *,
    market: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    financial_basis: str | None = None,
    factor_ids: list[str] | None = None,
    source_table: str = FACTOR_SOURCE_TABLE,
    snapshot_table: str = FACTOR_SNAPSHOT_TABLE,
    price_table: str = PRICE_TABLE,
    carry_forward: bool = True,
    snapshot_dates: list[str | date] | None = None,
    max_lookback_days: int | None = None,
    security_ids: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    if not carry_forward:
        return _build_raw_copy_snapshot_insert_query(
            market=market,
            start_date=start_date,
            end_date=end_date,
            financial_basis=financial_basis,
            factor_ids=factor_ids,
            source_table=source_table,
            snapshot_table=snapshot_table,
            security_ids=security_ids,
        )

    params: dict[str, Any] = {}
    source_filters = ["isFinite(factor_value)"]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        source_filters.append("startsWith(security_id, {security_prefix:String})")
    if security_ids:
        params["security_ids"] = sorted({str(value).strip() for value in security_ids if str(value).strip()})
        source_filters.append("security_id IN {security_ids:Array(String)}")
    normalized_snapshot_dates = (
        sorted({_date_iso(value) for value in snapshot_dates})
        if snapshot_dates is not None
        else None
    )
    if normalized_snapshot_dates:
        params["snapshot_dates"] = normalized_snapshot_dates
        params["end_date"] = normalized_snapshot_dates[-1]
        source_filters.append("trade_date <= {end_date:Date}")
        if max_lookback_days is not None:
            lookback_start = date.fromisoformat(normalized_snapshot_dates[0]) - timedelta(
                days=max(0, int(max_lookback_days))
            )
            params["lookback_start_date"] = lookback_start.isoformat()
            source_filters.append("trade_date >= {lookback_start_date:Date}")
    else:
        if start_date is None or end_date is None:
            raise ValueError("carry-forward snapshots require start_date/end_date or snapshot_dates")
        start_date_iso = _date_iso(start_date)
        end_date_iso = _date_iso(end_date)
        params["start_date"] = start_date_iso
        params["end_date"] = end_date_iso
        params["snapshot_dates"] = _date_range(start_date_iso, end_date_iso)
        source_filters.append("trade_date <= {end_date:Date}")
        if max_lookback_days is not None:
            lookback_start = date.fromisoformat(start_date_iso) - timedelta(
                days=max(0, int(max_lookback_days))
            )
            params["lookback_start_date"] = lookback_start.isoformat()
            source_filters.append("trade_date >= {lookback_start_date:Date}")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        source_filters.append("financial_basis = {financial_basis:String}")
    if factor_ids:
        params["factor_ids"] = sorted({str(factor_id).strip().lower() for factor_id in factor_ids if str(factor_id).strip()})
        source_filters.append("has({factor_ids:Array(String)}, factor_id)")

    source_where_sql = "\n        AND ".join(source_filters)
    query = f"""
INSERT INTO {_validate_table_name(snapshot_table)}
(
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
)
WITH
snapshot_dates AS (
    SELECT arrayJoin({{snapshot_dates:Array(Date)}}) AS snapshot_date
),
source_rows AS (
    SELECT
        trade_date,
        security_id,
        factor_id,
        financial_basis,
        factor_value,
        fiscal_year,
        financial_period,
        currency,
        updated_at
    FROM {_validate_table_name(source_table)}
    WHERE {source_where_sql}
)
SELECT
    d.snapshot_date AS trade_date,
    f.security_id AS security_id,
    f.factor_id AS factor_id,
    f.financial_basis AS financial_basis,
    argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value,
    max(f.trade_date) AS source_trade_date,
    argMax(f.fiscal_year, tuple(f.trade_date, f.updated_at)) AS fiscal_year,
    argMax(f.financial_period, tuple(f.trade_date, f.updated_at)) AS financial_period,
    argMax(f.currency, tuple(f.trade_date, f.updated_at)) AS currency,
    max(f.updated_at) AS updated_at
FROM source_rows AS f
CROSS JOIN snapshot_dates AS d
WHERE f.trade_date <= d.snapshot_date
GROUP BY
    d.snapshot_date,
    f.security_id,
    f.factor_id,
    f.financial_basis
""".strip()
    return query, params


def build_incremental_factor_snapshot_insert_query(
    *,
    market: str | None = None,
    snapshot_date: str | date,
    previous_snapshot_date: str | date,
    financial_basis: str | None = None,
    factor_ids: list[str] | None = None,
    source_table: str = FACTOR_SOURCE_TABLE,
    snapshot_table: str = FACTOR_SNAPSHOT_TABLE,
    security_ids: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "snapshot_date": _date_iso(snapshot_date),
        "previous_snapshot_date": _date_iso(previous_snapshot_date),
    }
    current_filters = [
        "f.trade_date = {snapshot_date:Date}",
        "isFinite(f.factor_value)",
    ]
    previous_filters = [
        "s.trade_date = {previous_snapshot_date:Date}",
    ]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        current_filters.append("startsWith(f.security_id, {security_prefix:String})")
        previous_filters.append("startsWith(s.security_id, {security_prefix:String})")
    if security_ids:
        params["security_ids"] = sorted({str(value).strip() for value in security_ids if str(value).strip()})
        current_filters.append("f.security_id IN {security_ids:Array(String)}")
        previous_filters.append("s.security_id IN {security_ids:Array(String)}")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        current_filters.append("f.financial_basis = {financial_basis:String}")
        previous_filters.append("s.financial_basis = {financial_basis:String}")
    if factor_ids:
        params["factor_ids"] = _normalize_factor_ids(factor_ids)
        current_filters.append("has({factor_ids:Array(String)}, f.factor_id)")
        previous_filters.append("has({factor_ids:Array(String)}, s.factor_id)")

    current_where_sql = "\n        AND ".join(current_filters)
    previous_where_sql = "\n        AND ".join(previous_filters)
    query = f"""
INSERT INTO {_validate_table_name(snapshot_table)}
(
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
)
WITH
current_raw AS (
    SELECT
        {{snapshot_date:Date}} AS trade_date,
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        f.financial_basis AS financial_basis,
        argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value,
        max(f.trade_date) AS source_trade_date,
        argMax(f.fiscal_year, tuple(f.trade_date, f.updated_at)) AS fiscal_year,
        argMax(f.financial_period, tuple(f.trade_date, f.updated_at)) AS financial_period,
        argMax(f.currency, tuple(f.trade_date, f.updated_at)) AS currency,
        max(f.updated_at) AS updated_at
    FROM {_validate_table_name(source_table)} AS f
    WHERE {current_where_sql}
    GROUP BY
        f.security_id,
        f.factor_id,
        f.financial_basis
),
previous_snapshot AS (
    SELECT
        {{snapshot_date:Date}} AS trade_date,
        s.security_id AS security_id,
        s.factor_id AS factor_id,
        s.financial_basis AS financial_basis,
        argMax(s.factor_value, tuple(s.updated_at, s.source_trade_date)) AS factor_value,
        argMax(s.source_trade_date, tuple(s.updated_at, s.source_trade_date)) AS source_trade_date,
        argMax(s.fiscal_year, tuple(s.updated_at, s.source_trade_date)) AS fiscal_year,
        argMax(s.financial_period, tuple(s.updated_at, s.source_trade_date)) AS financial_period,
        argMax(s.currency, tuple(s.updated_at, s.source_trade_date)) AS currency,
        max(s.updated_at) AS updated_at
    FROM {_validate_table_name(snapshot_table)} AS s
    LEFT JOIN current_raw AS r
        ON r.security_id = s.security_id
        AND r.factor_id = s.factor_id
        AND r.financial_basis = s.financial_basis
    WHERE {previous_where_sql}
        AND r.security_id = ''
    GROUP BY
        s.security_id,
        s.factor_id,
        s.financial_basis
)
SELECT
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
FROM previous_snapshot
UNION ALL
SELECT
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
FROM current_raw
""".strip()
    return query, params


def _insert_factor_snapshots_incremental(
    client: Any,
    *,
    market: str | None,
    start_date: str | date | None,
    end_date: str | date | None,
    financial_basis: str | None,
    factor_ids: list[str] | None,
    source_table: str,
    snapshot_table: str,
    price_table: str,
    date_batch_size: int,
    factor_chunk_size: int,
    max_lookback_days: int | None,
    max_threads: int | None,
    truncate: bool,
) -> None:
    if start_date is None or end_date is None:
        raise ValueError("carry-forward snapshot builds require --start-date and --end-date")
    snapshot_dates = _load_snapshot_dates(
        client,
        market=market,
        start_date=start_date,
        end_date=end_date,
        price_table=price_table,
    )
    if not snapshot_dates:
        return
    resolved_factor_ids = _resolve_factor_ids(
        client,
        market=market,
        factor_ids=factor_ids,
        financial_basis=financial_basis,
        source_table=source_table,
        end_date=end_date,
    )
    if not resolved_factor_ids:
        return

    factor_chunk_size = max(1, int(factor_chunk_size or DEFAULT_FACTOR_CHUNK_SIZE))
    settings = _memory_safe_settings(max_threads)
    factor_chunks = list(_chunks(resolved_factor_ids, factor_chunk_size))
    previous_dates_by_chunk: dict[tuple[str, ...], str | None] = {
        tuple(factor_chunk): None for factor_chunk in factor_chunks
    }
    total_batches = len(snapshot_dates) * len(factor_chunks)
    batch_index = 0
    for snapshot_date in snapshot_dates:
        for factor_chunk in factor_chunks:
            batch_index += 1
            chunk_key = tuple(factor_chunk)
            previous_snapshot_date = previous_dates_by_chunk[chunk_key]
            if previous_snapshot_date is None and not truncate:
                previous_snapshot_date = _latest_snapshot_date_before(
                    client,
                    market=market,
                    snapshot_date=snapshot_date,
                    financial_basis=financial_basis,
                    factor_ids=factor_chunk,
                    snapshot_table=snapshot_table,
                )
            if previous_snapshot_date:
                query, params = build_incremental_factor_snapshot_insert_query(
                    snapshot_date=snapshot_date,
                    previous_snapshot_date=previous_snapshot_date,
                    market=market,
                    financial_basis=financial_basis,
                    factor_ids=factor_chunk,
                    source_table=source_table,
                    snapshot_table=snapshot_table,
                )
                mode = "incremental"
            else:
                query, params = build_factor_snapshot_insert_query(
                    market=market,
                    financial_basis=financial_basis,
                    factor_ids=factor_chunk,
                    source_table=source_table,
                    snapshot_table=snapshot_table,
                    price_table=price_table,
                    carry_forward=True,
                    snapshot_dates=[snapshot_date],
                    max_lookback_days=max_lookback_days,
                )
                mode = "seed"
            _execute(client, query, params, settings=settings)
            previous_dates_by_chunk[chunk_key] = snapshot_date
            print(
                "[PROGRESS] factor snapshots "
                f"mode={mode}, batch={batch_index:,}/{total_batches:,}, "
                f"date={snapshot_date}, factors={len(factor_chunk):,}",
                flush=True,
            )


def _insert_factor_snapshots_chunked(
    client: Any,
    *,
    market: str | None,
    start_date: str | date | None,
    end_date: str | date | None,
    financial_basis: str | None,
    factor_ids: list[str] | None,
    source_table: str,
    snapshot_table: str,
    price_table: str,
    date_batch_size: int,
    factor_chunk_size: int,
    max_lookback_days: int | None,
    max_threads: int | None,
    truncate: bool = False,
) -> None:
    if start_date is None or end_date is None:
        raise ValueError("carry-forward snapshot builds require --start-date and --end-date")
    snapshot_dates = _load_snapshot_dates(
        client,
        market=market,
        start_date=start_date,
        end_date=end_date,
        price_table=price_table,
    )
    if not snapshot_dates:
        return
    resolved_factor_ids = _resolve_factor_ids(
        client,
        market=market,
        factor_ids=factor_ids,
        financial_basis=financial_basis,
        source_table=source_table,
        end_date=end_date,
    )
    if not resolved_factor_ids:
        return

    date_batch_size = max(1, int(date_batch_size or DEFAULT_DATE_BATCH_SIZE))
    factor_chunk_size = max(1, int(factor_chunk_size or DEFAULT_FACTOR_CHUNK_SIZE))
    settings = _memory_safe_settings(max_threads)
    total_batches = math_ceil_div(len(snapshot_dates), date_batch_size) * math_ceil_div(
        len(resolved_factor_ids),
        factor_chunk_size,
    )
    batch_index = 0
    for date_batch in _chunks(snapshot_dates, date_batch_size):
        for factor_chunk in _chunks(resolved_factor_ids, factor_chunk_size):
            batch_index += 1
            query, params = build_factor_snapshot_insert_query(
                market=market,
                financial_basis=financial_basis,
                factor_ids=factor_chunk,
                source_table=source_table,
                snapshot_table=snapshot_table,
                price_table=price_table,
                carry_forward=True,
                snapshot_dates=date_batch,
                max_lookback_days=max_lookback_days,
            )
            _execute(client, query, params, settings=settings)
            print(
                "[PROGRESS] factor snapshots "
                f"batch={batch_index:,}/{total_batches:,}, "
                f"dates={date_batch[0]}..{date_batch[-1]}, "
                f"factors={len(factor_chunk):,}",
                flush=True,
            )


def _build_raw_copy_snapshot_insert_query(
    *,
    market: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    financial_basis: str | None = None,
    factor_ids: list[str] | None = None,
    source_table: str = FACTOR_SOURCE_TABLE,
    snapshot_table: str = FACTOR_SNAPSHOT_TABLE,
    security_ids: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    filters = ["isFinite(factor_value)"]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        filters.append("startsWith(security_id, {security_prefix:String})")
    if security_ids:
        params["security_ids"] = sorted({str(value).strip() for value in security_ids if str(value).strip()})
        filters.append("security_id IN {security_ids:Array(String)}")
    if start_date is not None:
        params["start_date"] = _date_iso(start_date)
        filters.append("trade_date >= {start_date:Date}")
    if end_date is not None:
        params["end_date"] = _date_iso(end_date)
        filters.append("trade_date <= {end_date:Date}")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    if factor_ids:
        params["factor_ids"] = sorted({str(factor_id).strip().lower() for factor_id in factor_ids if str(factor_id).strip()})
        filters.append("has({factor_ids:Array(String)}, factor_id)")

    where_sql = "\n    AND ".join(filters)
    query = f"""
INSERT INTO {_validate_table_name(snapshot_table)}
(
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
)
SELECT
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    trade_date AS source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
FROM {_validate_table_name(source_table)}
WHERE {where_sql}
""".strip()
    return query, params


def _count_inserted_rows(
    client: Any,
    *,
    market: str | None,
    start_date: str | date | None,
    end_date: str | date | None,
    financial_basis: str | None,
    factor_ids: list[str] | None,
    snapshot_table: str,
) -> int:
    query, params = _build_count_query(
        market=market,
        start_date=start_date,
        end_date=end_date,
        financial_basis=financial_basis,
        factor_ids=factor_ids,
        snapshot_table=snapshot_table,
    )
    rows = _query_rows(client, query, params)
    return int(rows[0][0]) if rows else 0


def _build_count_query(
    *,
    market: str | None,
    start_date: str | date | None,
    end_date: str | date | None,
    financial_basis: str | None,
    factor_ids: list[str] | None,
    snapshot_table: str,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    filters = ["1 = 1"]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        filters.append("startsWith(security_id, {security_prefix:String})")
    if start_date is not None:
        params["start_date"] = _date_iso(start_date)
        filters.append("trade_date >= {start_date:Date}")
    if end_date is not None:
        params["end_date"] = _date_iso(end_date)
        filters.append("trade_date <= {end_date:Date}")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    if factor_ids:
        params["factor_ids"] = sorted({str(factor_id).strip().lower() for factor_id in factor_ids if str(factor_id).strip()})
        filters.append("has({factor_ids:Array(String)}, factor_id)")
    query = f"""
SELECT count()
FROM {_validate_table_name(snapshot_table)}
WHERE {" AND ".join(filters)}
""".strip()
    return query, params


def _load_snapshot_dates(
    client: Any,
    *,
    market: str | None,
    start_date: str | date,
    end_date: str | date,
    price_table: str,
) -> list[str]:
    security_prefix = _security_prefix_for_market(market)
    market_filter = (
        "\n    AND startsWith(security_id, {security_prefix:String})"
        if security_prefix
        else ""
    )
    parameters = {
        "start_date": _date_iso(start_date),
        "end_date": _date_iso(end_date),
    }
    if security_prefix:
        parameters["security_prefix"] = security_prefix
    rows = _query_rows(
        client,
        f"""
SELECT DISTINCT trade_date
FROM {_validate_table_name(price_table)}
WHERE trade_date >= {{start_date:Date}}
    AND trade_date <= {{end_date:Date}}
    {market_filter}
ORDER BY trade_date ASC
""".strip(),
        parameters,
    )
    return [_date_iso(row[0]) for row in rows]


def _resolve_factor_ids(
    client: Any,
    *,
    market: str | None,
    factor_ids: list[str] | None,
    financial_basis: str | None,
    source_table: str,
    end_date: str | date,
) -> list[str]:
    if factor_ids:
        return _normalize_factor_ids(factor_ids)

    params: dict[str, Any] = {"end_date": _date_iso(end_date)}
    filters = ["trade_date <= {end_date:Date}", "isFinite(factor_value)"]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        filters.append("startsWith(security_id, {security_prefix:String})")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    rows = _query_rows(
        client,
        f"""
SELECT DISTINCT factor_id
FROM {_validate_table_name(source_table)}
WHERE {" AND ".join(filters)}
ORDER BY factor_id ASC
""".strip(),
        params,
    )
    return [str(row[0]) for row in rows]


def _latest_snapshot_date_before(
    client: Any,
    *,
    market: str | None,
    snapshot_date: str | date,
    financial_basis: str | None,
    factor_ids: list[str],
    snapshot_table: str,
) -> str | None:
    normalized_factor_ids = _normalize_factor_ids(factor_ids)
    if not normalized_factor_ids:
        return None
    params: dict[str, Any] = {
        "snapshot_date": _date_iso(snapshot_date),
        "factor_ids": normalized_factor_ids,
        "factor_count": len(normalized_factor_ids),
    }
    filters = [
        "trade_date < {snapshot_date:Date}",
        "has({factor_ids:Array(String)}, factor_id)",
    ]
    security_prefix = _security_prefix_for_market(market)
    if security_prefix:
        params["security_prefix"] = security_prefix
        filters.append("startsWith(security_id, {security_prefix:String})")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    rows = _query_rows(
        client,
        f"""
SELECT trade_date
FROM {_validate_table_name(snapshot_table)}
WHERE {" AND ".join(filters)}
GROUP BY trade_date
HAVING countDistinct(factor_id) >= {{factor_count:UInt64}}
ORDER BY trade_date DESC
LIMIT 1
""".strip(),
        params,
    )
    return _date_iso(rows[0][0]) if rows else None


def _chunks(values: list[Any], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def math_ceil_div(value: int, divisor: int) -> int:
    if divisor <= 0:
        raise ValueError("divisor must be positive")
    return (value + divisor - 1) // divisor


def _memory_safe_settings(max_threads: int | None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "max_bytes_before_external_group_by": 512 * 1024 * 1024,
        "max_bytes_before_external_sort": 512 * 1024 * 1024,
    }
    if max_threads is not None and int(max_threads) > 0:
        settings["max_threads"] = int(max_threads)
    return settings


def _execute(
    client: Any,
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
) -> None:
    parameters = parameters or {}
    settings = settings or {}
    command = getattr(client, "command", None)
    if callable(command):
        try:
            command(query, parameters=parameters, settings=settings)
        except TypeError:
            command(query, parameters=parameters)
        return
    query_method = getattr(client, "query", None)
    if callable(query_method):
        try:
            query_method(query, parameters=parameters, settings=settings)
        except TypeError:
            query_method(query, parameters=parameters)
        return
    client.query_df(query, parameters=parameters, settings=settings)


def _query_rows(client: Any, query: str, parameters: dict[str, Any]) -> list[tuple[Any, ...]]:
    query_method = getattr(client, "query", None)
    if callable(query_method):
        return list(query_method(query, parameters=parameters).result_rows)
    frame = client.query_df(query, parameters=parameters)
    if hasattr(frame, "itertuples"):
        return [tuple(row) for row in frame.itertuples(index=False, name=None)]
    return list(frame)


def _validate_table_name(table_name: str) -> str:
    parts = str(table_name).split(".")
    if not parts or len(parts) > 2:
        raise ValueError(f"invalid table name: {table_name!r}")
    for part in parts:
        if not part.replace("_", "").isalnum() or not part or part[0].isdigit():
            raise ValueError(f"invalid table name: {table_name!r}")
    return table_name


def _date_iso(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _date_range(start_date: str, end_date: str) -> list[str]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if current > end:
        raise ValueError("start_date must be earlier than or equal to end_date")
    values: list[str] = []
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _normalize_factor_ids(factor_ids: list[str]) -> list[str]:
    return sorted({str(factor_id).strip().lower() for factor_id in factor_ids if str(factor_id).strip()})


def _parse_factor_ids(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _security_prefix_for_market(market: str | None) -> str | None:
    if market is None:
        return None
    normalized = str(market).strip().lower()
    prefixes = {"kr": "SEC_KR_", "us": "SEC_US_"}
    if normalized not in prefixes:
        raise ValueError("market must be 'kr', 'us', or None")
    return prefixes[normalized]


def _delete_market_snapshots(
    client: Any,
    *,
    market: str,
    start_date: str | date | None,
    end_date: str | date | None,
    financial_basis: str | None,
    factor_ids: list[str] | None,
    snapshot_table: str,
) -> None:
    params: dict[str, Any] = {
        "security_prefix": _security_prefix_for_market(market),
    }
    filters = ["startsWith(security_id, {security_prefix:String})"]
    if start_date is not None:
        params["start_date"] = _date_iso(start_date)
        filters.append("trade_date >= {start_date:Date}")
    if end_date is not None:
        params["end_date"] = _date_iso(end_date)
        filters.append("trade_date <= {end_date:Date}")
    if financial_basis:
        params["financial_basis"] = str(financial_basis)
        filters.append("financial_basis = {financial_basis:String}")
    if factor_ids:
        params["factor_ids"] = _normalize_factor_ids(factor_ids)
        filters.append("has({factor_ids:Array(String)}, factor_id)")
    query = (
        f"ALTER TABLE {_validate_table_name(snapshot_table)} DELETE WHERE "
        f"{' AND '.join(filters)} SETTINGS mutations_sync = 2"
    )
    _execute(client, query, params)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the fast daily factor snapshot table.")
    parser.add_argument("--market", choices=["kr", "us"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--financial-basis", choices=["annual", "ttm", "quarterly"])
    parser.add_argument("--factor-ids", help="Comma-separated factor ids to copy.")
    parser.add_argument("--source-table", default=FACTOR_SOURCE_TABLE)
    parser.add_argument("--snapshot-table", default=FACTOR_SNAPSHOT_TABLE)
    parser.add_argument("--price-table", default=PRICE_TABLE)
    parser.add_argument("--copy-raw-only", action="store_true")
    parser.add_argument(
        "--full-asof",
        action="store_true",
        help="Rebuild each snapshot date from raw history instead of using incremental carry-forward.",
    )
    parser.add_argument("--date-batch-size", type=int, default=DEFAULT_DATE_BATCH_SIZE)
    parser.add_argument("--factor-chunk-size", type=int, default=DEFAULT_FACTOR_CHUNK_SIZE)
    parser.add_argument("--max-lookback-days", type=int)
    parser.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    parser.add_argument("--truncate", action="store_true")
    parser.add_argument("--create-only", action="store_true")
    args = parser.parse_args()

    client = get_clickhouse_client()
    try:
        ensure_factor_snapshot_table(client, snapshot_table=args.snapshot_table)
        if args.create_only:
            print(f"created snapshot table={args.snapshot_table}")
            return
        row_count = insert_factor_snapshots(
            market=args.market,
            start_date=args.start_date,
            end_date=args.end_date,
            financial_basis=args.financial_basis,
            factor_ids=_parse_factor_ids(args.factor_ids),
            source_table=args.source_table,
            snapshot_table=args.snapshot_table,
            price_table=args.price_table,
            carry_forward=not args.copy_raw_only,
            incremental=not args.full_asof,
            date_batch_size=args.date_batch_size,
            factor_chunk_size=args.factor_chunk_size,
            max_lookback_days=args.max_lookback_days,
            max_threads=args.max_threads,
            truncate=args.truncate,
            client=client,
        )
    finally:
        client.close()
    print(f"factor snapshot rows={row_count:,}, table={args.snapshot_table}")


if __name__ == "__main__":
    main()
