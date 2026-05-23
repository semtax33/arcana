from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from api.repository.factor_screen_query import FactorCondition


RankDirection = Literal["catalog", "higher", "lower"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BENCHMARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class FactorSnapshotQuerySpec:
    query: str
    parameters: dict[str, Any]


def build_factor_snapshot_query(
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    signal_date: str | date,
    financial_basis: str | None = "annual",
    factor_table: str = "fact_daily_factors",
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
) -> tuple[str, dict[str, Any]]:
    normalized_conditions = [_coerce_condition(condition) for condition in conditions]
    if not normalized_conditions:
        raise ValueError("at least one factor condition is required")

    factor_ids = _validate_factor_ids(
        sorted({condition.factor_id for condition in normalized_conditions})
    )
    params: dict[str, Any] = {
        "signal_date": _resolve_date(signal_date),
        "factor_ids": factor_ids,
    }
    basis_filter = ""
    if financial_basis:
        params["financial_basis"] = financial_basis
        basis_filter = "\n        AND f.financial_basis = {financial_basis:String}"

    query = f"""
WITH
selected_catalog AS (
    SELECT
        factor_id,
        any(factor_name) AS factor_name,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{factor_ids:Array(String)}}, factor_id)
    GROUP BY factor_id
),
latest_factor_values AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        any(c.factor_name) AS factor_name,
        any(c.value_direction) AS value_direction,
        argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value,
        max(f.trade_date) AS trade_date
    FROM {_validate_table_name(factor_table)} AS f
    INNER JOIN selected_catalog AS c
        ON c.factor_id = f.factor_id
    WHERE f.trade_date <= {{signal_date:Date}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
        AND isFinite(f.factor_value){basis_filter}
    GROUP BY
        f.security_id,
        f.factor_id
    HAVING factor_value >= 0
),
ranked_factor_values AS (
    SELECT
        security_id,
        factor_id,
        factor_name,
        value_direction,
        factor_value,
        trade_date,
        row_number() OVER (PARTITION BY factor_id ORDER BY factor_value DESC, security_id ASC) AS rank_high,
        row_number() OVER (PARTITION BY factor_id ORDER BY factor_value ASC, security_id ASC) AS rank_low,
        count() OVER (PARTITION BY factor_id) AS factor_count
    FROM latest_factor_values
)
SELECT
    rf.security_id AS security_id,
    any(id.id_value) AS ticker,
    any(iss.legal_name_ko) AS stock_name,
    rf.factor_id AS factor_id,
    any(rf.factor_name) AS factor_name,
    any(rf.value_direction) AS value_direction,
    any(rf.factor_value) AS factor_value,
    max(rf.trade_date) AS factor_trade_date,
    any(rf.rank_high) AS rank_high,
    any(rf.rank_low) AS rank_low,
    any(rf.factor_count) AS factor_count,
    if(
        any(rf.factor_count) <= 1,
        100.0,
        if(
            any(rf.value_direction) = 'LOWER_BETTER',
            (any(rf.factor_count) - any(rf.rank_low)) / (any(rf.factor_count) - 1) * 100.0,
            (any(rf.factor_count) - any(rf.rank_high)) / (any(rf.factor_count) - 1) * 100.0
        )
    ) AS percentile_score
FROM ranked_factor_values AS rf
LEFT JOIN {_validate_table_name(security_table)} AS sm
    ON sm.security_id = rf.security_id
LEFT JOIN {_validate_table_name(issuer_table)} AS iss
    ON iss.issuer_id = sm.issuer_id
LEFT JOIN {_validate_table_name(identifier_table)} AS id
    ON id.security_id = rf.security_id
    AND id.id_type = 'TICKER'
    AND id.is_primary
GROUP BY
    rf.security_id,
    rf.factor_id
ORDER BY
    rf.security_id ASC,
    rf.factor_id ASC
""".strip()
    return query, params


def build_trading_days_query(
    *,
    start_date: str | date,
    end_date: str | date,
    lookback_days: int = 10,
    price_table: str = "price_daily",
) -> tuple[str, dict[str, Any]]:
    start = date.fromisoformat(_resolve_date(start_date)) - timedelta(days=lookback_days)
    query = f"""
SELECT DISTINCT trade_date
FROM {_validate_table_name(price_table)}
WHERE trade_date >= {{start_date:Date}}
    AND trade_date <= {{end_date:Date}}
ORDER BY trade_date ASC
""".strip()
    return query, {
        "start_date": start.isoformat(),
        "end_date": _resolve_date(end_date),
    }


def build_price_history_query(
    *,
    security_ids: list[str],
    start_date: str | date,
    end_date: str | date,
    price_table: str = "price_daily",
) -> tuple[str, dict[str, Any]]:
    if not security_ids:
        raise ValueError("security_ids must not be empty")
    query = f"""
SELECT
    security_id,
    trade_date,
    close
FROM {_validate_table_name(price_table)}
WHERE has({{security_ids:Array(String)}}, security_id)
    AND trade_date >= {{start_date:Date}}
    AND trade_date <= {{end_date:Date}}
    AND close IS NOT NULL
ORDER BY trade_date ASC, security_id ASC
""".strip()
    return query, {
        "security_ids": sorted(set(str(security_id) for security_id in security_ids)),
        "start_date": _resolve_date(start_date),
        "end_date": _resolve_date(end_date),
    }


def build_benchmark_history_query(
    *,
    benchmark_ids: list[str],
    start_date: str | date,
    end_date: str | date,
    benchmark_table: str = "benchmark_price_daily",
) -> tuple[str, dict[str, Any]]:
    normalized_ids = _validate_benchmark_ids(benchmark_ids)
    query = f"""
SELECT
    benchmark_id,
    trade_date,
    close
FROM {_validate_table_name(benchmark_table)}
WHERE has({{benchmark_ids:Array(String)}}, benchmark_id)
    AND trade_date >= {{start_date:Date}}
    AND trade_date <= {{end_date:Date}}
    AND close IS NOT NULL
ORDER BY trade_date ASC, benchmark_id ASC
""".strip()
    return query, {
        "benchmark_ids": normalized_ids,
        "start_date": _resolve_date(start_date),
        "end_date": _resolve_date(end_date),
    }


def _coerce_condition(condition: FactorCondition | dict[str, Any]) -> FactorCondition:
    if isinstance(condition, FactorCondition):
        _validate_factor_id(condition.factor_id)
        return condition
    if isinstance(condition, dict):
        coerced = FactorCondition(**condition)
        _validate_factor_id(coerced.factor_id)
        return coerced
    raise TypeError("conditions must contain FactorCondition instances or dictionaries")


def _validate_factor_ids(factor_ids: list[str]) -> list[str]:
    if not factor_ids:
        raise ValueError("factor_ids must not be empty")
    return [_validate_factor_id(factor_id) for factor_id in factor_ids]


def _validate_factor_id(factor_id: str) -> str:
    if not isinstance(factor_id, str) or not _FACTOR_ID_RE.match(factor_id):
        raise ValueError(f"invalid factor_id: {factor_id!r}")
    return factor_id


def _validate_benchmark_ids(benchmark_ids: list[str]) -> list[str]:
    if not benchmark_ids:
        return []
    normalized_ids = []
    for benchmark_id in benchmark_ids:
        text = str(benchmark_id).strip().upper()
        if not _BENCHMARK_ID_RE.match(text):
            raise ValueError(f"invalid benchmark_id: {benchmark_id!r}")
        normalized_ids.append(text)
    return sorted(set(normalized_ids))


def _validate_table_name(table_name: str) -> str:
    if not isinstance(table_name, str) or not _IDENTIFIER_RE.match(table_name):
        raise ValueError(f"invalid table name: {table_name!r}")
    return table_name


def _resolve_date(value: str | date) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    raise TypeError("date value must be a date or ISO date string")


def today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()
