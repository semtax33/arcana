from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from api.repository.factor_screen_query import FactorCondition
from api.service.style_score_catalog import (
    DEFAULT_SCREEN_STYLE_PROFILE,
    STYLE_SCORE_FACTORS,
    is_style_score_factor,
    style_score_factor_definition,
)
from api.service.factor_identity import canonical_factor_id


RankDirection = Literal["catalog", "higher", "lower"]
DEFAULT_FACTOR_TABLE = "fact_daily_factors"
DEFAULT_FACTOR_SNAPSHOT_TABLE = "fact_daily_factor_snapshot"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BENCHMARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CLASSIFICATION_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class FactorSnapshotQuerySpec:
    query: str
    parameters: dict[str, Any]


def build_factor_snapshot_query(
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    signal_date: str | date,
    snapshot_date: str | date | None = None,
    market: str | None = None,
    financial_basis: str | None = "annual",
    style_profile: str | None = DEFAULT_SCREEN_STYLE_PROFILE,
    sector_codes: list[str] | None = None,
    industry_group_codes: list[str] | None = None,
    factor_table: str = DEFAULT_FACTOR_TABLE,
    factor_table_is_snapshot: bool = False,
    raw_lookback_days: int | None = None,
    style_score_table: str = "arcana.fact_daily_style_score",
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
) -> tuple[str, dict[str, Any]]:
    normalized_conditions = [_coerce_condition(condition) for condition in conditions]
    if not normalized_conditions:
        raise ValueError("at least one factor condition is required")
    normalized_sector_codes = (
        _validate_classification_codes(sector_codes, "sector_codes") if sector_codes else None
    )
    normalized_industry_group_codes = (
        _validate_classification_codes(industry_group_codes, "industry_group_codes")
        if industry_group_codes
        else None
    )
    normalized_market = _normalize_market(market)

    factor_ids = _validate_factor_ids(
        sorted({condition.factor_id for condition in normalized_conditions})
    )
    regular_factor_ids = [factor_id for factor_id in factor_ids if not is_style_score_factor(factor_id)]
    style_factor_ids = [factor_id for factor_id in factor_ids if is_style_score_factor(factor_id)]
    regular_factor_param = "regular_factor_ids" if style_factor_ids else "factor_ids"
    params: dict[str, Any] = {
        "signal_date": _resolve_date(signal_date),
        "factor_ids": factor_ids,
    }
    if snapshot_date is not None:
        params["snapshot_date"] = _resolve_date(snapshot_date)
        if params["snapshot_date"] > params["signal_date"]:
            raise ValueError("snapshot_date must not be later than signal_date")
    if regular_factor_ids:
        params[regular_factor_param] = regular_factor_ids
    if style_factor_ids:
        params["style_profile"] = _normalize_style_profile(style_profile)
    if normalized_sector_codes:
        params["sector_codes"] = normalized_sector_codes
    if normalized_industry_group_codes:
        params["industry_group_codes"] = normalized_industry_group_codes
    if normalized_market:
        params["market_country"] = normalized_market.upper()
    basis_filter = ""
    if financial_basis:
        params["financial_basis"] = financial_basis
        basis_filter = "\n        AND f.financial_basis = {financial_basis:String}"
    raw_lookback_filter = ""
    if not factor_table_is_snapshot and raw_lookback_days is not None and int(raw_lookback_days) > 0:
        params["raw_start_date"] = (
            date.fromisoformat(params["signal_date"]) - timedelta(days=int(raw_lookback_days))
        ).isoformat()
        raw_lookback_filter = "\n        AND f.trade_date >= {raw_start_date:Date}"

    ctes = []
    if factor_table_is_snapshot and regular_factor_ids:
        if snapshot_date is not None:
            ctes.append(
                """
latest_snapshot_date AS (
    SELECT {snapshot_date:Date} AS snapshot_date
)
""".strip()
            )
        else:
            snapshot_date_filter = (
                f"\n        AND has({{{regular_factor_param}:Array(String)}}, factor_id)"
                if regular_factor_ids
                else ""
            )
            ctes.append(
                f"""
latest_snapshot_date AS (
    SELECT
        max(trade_date) AS snapshot_date
    FROM {_validate_table_name(factor_table)}
    WHERE trade_date <= {{signal_date:Date}}{snapshot_date_filter}{basis_filter.replace("f.", "")}
)
""".strip()
            )
    needs_security_universe = bool(
        normalized_sector_codes or normalized_industry_group_codes or normalized_market
    )
    regular_security_universe_join = ""
    style_security_universe_join = ""
    if needs_security_universe:
        sector_filter = ""
        if normalized_sector_codes:
            sector_filter = "\n        AND has({sector_codes:Array(String)}, iss.sector_code)"
        industry_group_filter = ""
        if normalized_industry_group_codes:
            industry_group_filter = (
                "\n        AND has({industry_group_codes:Array(String)}, iss.industry_group_code)"
            )
        market_filter = ""
        if normalized_market:
            market_filter = "\n        AND sm.country = {market_country:String}"
        ctes.append(
            f"""
security_universe AS (
    SELECT
        sm.security_id AS security_id
    FROM {_validate_table_name(security_table)} AS sm
    LEFT JOIN {_validate_table_name(issuer_table)} AS iss
        ON iss.issuer_id = sm.issuer_id
    WHERE 1 = 1{market_filter}{sector_filter}{industry_group_filter}
    GROUP BY sm.security_id
)
""".strip()
        )
        regular_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = f.security_id"
        )
        style_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = s.security_id"
        )
    if regular_factor_ids:
        ctes.append(
            f"""
selected_catalog AS (
    SELECT
        factor_id,
        any(factor_name) AS factor_name,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{{regular_factor_param}:Array(String)}}, factor_id)
    GROUP BY factor_id
)
""".strip()
        )

    latest_factor_sources = []
    if regular_factor_ids:
        factor_date_predicate = (
            "f.trade_date = (SELECT snapshot_date FROM latest_snapshot_date)"
            if factor_table_is_snapshot
            else "f.trade_date <= {signal_date:Date}"
        )
        if raw_lookback_filter and not factor_table_is_snapshot:
            factor_date_predicate += raw_lookback_filter
        if factor_table_is_snapshot:
            factor_date_predicate += "\n        AND f.source_trade_date <= {signal_date:Date}"
        source_trade_date_expr = (
            "argMax(f.source_trade_date, tuple(f.trade_date, f.updated_at))"
            if factor_table_is_snapshot
            else "max(f.trade_date)"
        )
        latest_factor_sources.append(
            f"""
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        any(c.factor_name) AS factor_name,
        any(c.value_direction) AS value_direction,
        argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value,
        {source_trade_date_expr} AS trade_date
    FROM {_validate_table_name(factor_table)} AS f
    INNER JOIN selected_catalog AS c
        ON c.factor_id = f.factor_id{regular_security_universe_join}
    WHERE {factor_date_predicate}
        AND has({{{regular_factor_param}:Array(String)}}, f.factor_id)
        AND isFinite(f.factor_value){basis_filter}
    GROUP BY
        f.security_id,
        f.factor_id
""".strip()
        )
    latest_factor_sources.extend(
        _build_style_score_snapshot_sources(
            style_factor_ids,
            style_score_table=style_score_table,
            security_universe_join=style_security_universe_join,
        )
    )
    ctes.append(
        "latest_factor_values AS (\n"
        + "\nUNION ALL\n".join(latest_factor_sources)
        + "\n)"
    )
    ctes.append(
        """
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
""".strip()
    )
    with_sql = "WITH\n" + ",\n".join(ctes)

    query = f"""
{with_sql}
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


def build_factor_snapshot_batch_query(
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    signal_dates: list[str | date],
    snapshot_dates: list[str | date] | None = None,
    market: str | None = None,
    financial_basis: str | None = "annual",
    factor_table: str = DEFAULT_FACTOR_SNAPSHOT_TABLE,
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
    sector_codes: list[str] | None = None,
    industry_group_codes: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    normalized_conditions = [_coerce_condition(condition) for condition in conditions]
    if not normalized_conditions:
        raise ValueError("at least one factor condition is required")
    resolved_signal_dates = [_resolve_date(value) for value in signal_dates]
    if not resolved_signal_dates:
        raise ValueError("signal_dates must not be empty")
    resolved_snapshot_dates = (
        [_resolve_date(value) for value in snapshot_dates]
        if snapshot_dates is not None
        else list(resolved_signal_dates)
    )
    if len(resolved_snapshot_dates) != len(resolved_signal_dates):
        raise ValueError("snapshot_dates must have the same length as signal_dates")
    snapshot_by_signal: dict[str, str] = {}
    for signal_date, snapshot_date in zip(
        resolved_signal_dates,
        resolved_snapshot_dates,
        strict=True,
    ):
        existing = snapshot_by_signal.get(signal_date)
        if existing is not None and existing != snapshot_date:
            raise ValueError("duplicate signal_dates must map to the same snapshot_date")
        snapshot_by_signal[signal_date] = snapshot_date
    normalized_signal_dates = sorted(snapshot_by_signal)
    normalized_snapshot_dates = [
        snapshot_by_signal[signal_date] for signal_date in normalized_signal_dates
    ]
    if any(
        snapshot_date > signal_date
        for signal_date, snapshot_date in zip(
            normalized_signal_dates,
            normalized_snapshot_dates,
            strict=True,
        )
    ):
        raise ValueError("snapshot_dates must not be later than their signal_dates")

    factor_ids = _validate_factor_ids(
        sorted({condition.factor_id for condition in normalized_conditions})
    )
    if any(is_style_score_factor(factor_id) for factor_id in factor_ids):
        raise ValueError("batch factor snapshots only support regular factor ids")

    normalized_sector_codes = (
        _validate_classification_codes(sector_codes, "sector_codes") if sector_codes else None
    )
    normalized_industry_group_codes = (
        _validate_classification_codes(industry_group_codes, "industry_group_codes")
        if industry_group_codes
        else None
    )
    normalized_market = _normalize_market(market)
    params: dict[str, Any] = {
        "signal_dates": normalized_signal_dates,
        "snapshot_dates": normalized_snapshot_dates,
        "factor_ids": factor_ids,
    }
    basis_filter = ""
    if financial_basis:
        params["financial_basis"] = financial_basis
        basis_filter = "\n        AND f.financial_basis = {financial_basis:String}"
    if normalized_sector_codes:
        params["sector_codes"] = normalized_sector_codes
    if normalized_industry_group_codes:
        params["industry_group_codes"] = normalized_industry_group_codes
    if normalized_market:
        params["market_country"] = normalized_market.upper()

    ctes = [
        """
snapshot_date_map AS (
    SELECT
        tupleElement(pair, 1) AS signal_date,
        tupleElement(pair, 2) AS snapshot_date
    FROM (
        SELECT arrayJoin(
            arrayZip(
                {signal_dates:Array(Date)},
                {snapshot_dates:Array(Date)}
            )
        ) AS pair
    )
)
""".strip(),
        f"""
selected_catalog AS (
    SELECT
        factor_id,
        any(factor_name) AS factor_name,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{factor_ids:Array(String)}}, factor_id)
    GROUP BY factor_id
)
""".strip(),
    ]

    needs_security_universe = bool(
        normalized_sector_codes or normalized_industry_group_codes or normalized_market
    )
    regular_security_universe_join = ""
    if needs_security_universe:
        sector_filter = ""
        if normalized_sector_codes:
            sector_filter = "\n        AND has({sector_codes:Array(String)}, iss.sector_code)"
        industry_group_filter = ""
        if normalized_industry_group_codes:
            industry_group_filter = (
                "\n        AND has({industry_group_codes:Array(String)}, iss.industry_group_code)"
            )
        market_filter = ""
        if normalized_market:
            market_filter = "\n        AND sm.country = {market_country:String}"
        ctes.append(
            f"""
security_universe AS (
    SELECT
        sm.security_id AS security_id
    FROM {_validate_table_name(security_table)} AS sm
    LEFT JOIN {_validate_table_name(issuer_table)} AS iss
        ON iss.issuer_id = sm.issuer_id
    WHERE 1 = 1{market_filter}{sector_filter}{industry_group_filter}
    GROUP BY sm.security_id
)
""".strip()
        )
        regular_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = f.security_id"
        )

    ctes.append(
        f"""
latest_factor_values AS (
    SELECT
        lsd.signal_date AS signal_date,
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        any(c.factor_name) AS factor_name,
        any(c.value_direction) AS value_direction,
        argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value,
        argMax(f.source_trade_date, tuple(f.trade_date, f.updated_at)) AS trade_date
    FROM {_validate_table_name(factor_table)} AS f
    INNER JOIN snapshot_date_map AS lsd
        ON lsd.snapshot_date = f.trade_date
    INNER JOIN selected_catalog AS c
        ON c.factor_id = f.factor_id{regular_security_universe_join}
    WHERE f.trade_date IN {{snapshot_dates:Array(Date)}}
        AND f.source_trade_date <= lsd.signal_date
        AND has({{factor_ids:Array(String)}}, f.factor_id)
        AND isFinite(f.factor_value){basis_filter}
    GROUP BY
        lsd.signal_date,
        f.security_id,
        f.factor_id
)
""".strip()
    )
    ctes.append(
        """
ranked_factor_values AS (
    SELECT
        signal_date,
        security_id,
        factor_id,
        factor_name,
        value_direction,
        factor_value,
        trade_date,
        row_number() OVER (PARTITION BY signal_date, factor_id ORDER BY factor_value DESC, security_id ASC) AS rank_high,
        row_number() OVER (PARTITION BY signal_date, factor_id ORDER BY factor_value ASC, security_id ASC) AS rank_low,
        count() OVER (PARTITION BY signal_date, factor_id) AS factor_count
    FROM latest_factor_values
)
""".strip()
    )
    with_sql = "WITH\n" + ",\n".join(ctes)

    query = f"""
{with_sql}
SELECT
    rf.signal_date AS signal_date,
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
    rf.signal_date,
    rf.security_id,
    rf.factor_id
ORDER BY
    rf.signal_date ASC,
    rf.security_id ASC,
    rf.factor_id ASC
""".strip()
    return query, params


def build_factor_raw_batch_query(
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    signal_dates: list[str | date],
    market: str | None = None,
    financial_basis: str | None = "annual",
    factor_table: str = DEFAULT_FACTOR_TABLE,
    raw_lookback_days: int | None = None,
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
    sector_codes: list[str] | None = None,
    industry_group_codes: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one raw-factor query for every requested signal date."""

    normalized_conditions = [_coerce_condition(condition) for condition in conditions]
    if not normalized_conditions:
        raise ValueError("at least one factor condition is required")
    normalized_signal_dates = sorted({_resolve_date(value) for value in signal_dates})
    if not normalized_signal_dates:
        raise ValueError("signal_dates must not be empty")

    factor_ids = _validate_factor_ids(
        sorted({condition.factor_id for condition in normalized_conditions})
    )
    if any(is_style_score_factor(factor_id) for factor_id in factor_ids):
        raise ValueError("batch raw factors only support regular factor ids")

    normalized_sector_codes = (
        _validate_classification_codes(sector_codes, "sector_codes") if sector_codes else None
    )
    normalized_industry_group_codes = (
        _validate_classification_codes(industry_group_codes, "industry_group_codes")
        if industry_group_codes
        else None
    )
    normalized_market = _normalize_market(market)
    earliest_signal_date = date.fromisoformat(normalized_signal_dates[0])
    latest_signal_date = normalized_signal_dates[-1]
    params: dict[str, Any] = {
        "signal_dates": normalized_signal_dates,
        "factor_ids": factor_ids,
        "max_signal_date": latest_signal_date,
    }
    raw_lookback_filter = ""
    lookback_days: int | None = None
    if raw_lookback_days is not None and int(raw_lookback_days) > 0:
        lookback_days = int(raw_lookback_days)
        params["raw_start_date"] = (
            earliest_signal_date - timedelta(days=lookback_days)
        ).isoformat()
        raw_lookback_filter = "\n        AND f.trade_date >= {raw_start_date:Date}"
    basis_filter = ""
    if financial_basis:
        params["financial_basis"] = financial_basis
        basis_filter = "\n        AND f.financial_basis = {financial_basis:String}"
    if normalized_sector_codes:
        params["sector_codes"] = normalized_sector_codes
    if normalized_industry_group_codes:
        params["industry_group_codes"] = normalized_industry_group_codes
    if normalized_market:
        params["market_country"] = normalized_market.upper()

    aggregate_columns: list[str] = []
    expanded_points: list[str] = []
    for index, signal_date in enumerate(normalized_signal_dates):
        signal_param = f"signal_date_{index}"
        params[signal_param] = signal_date
        condition = f"f.trade_date <= {{{signal_param}:Date}}"
        if lookback_days is not None:
            raw_start_param = f"signal_raw_start_date_{index}"
            params[raw_start_param] = (
                date.fromisoformat(signal_date) - timedelta(days=lookback_days)
            ).isoformat()
            condition += f" AND f.trade_date >= {{{raw_start_param}:Date}}"
        aggregate_columns.extend(
            [
                (
                    "argMaxIf(f.factor_value, tuple(f.trade_date, f.updated_at), "
                    f"{condition}) AS factor_value_{index}"
                ),
                f"maxIf(f.trade_date, {condition}) AS factor_trade_date_{index}",
            ]
        )
        expanded_points.append(
            f"tuple({{{signal_param}:Date}}, factor_value_{index}, factor_trade_date_{index})"
        )

    ctes = [
        f"""
selected_catalog AS (
    SELECT
        factor_id,
        any(factor_name) AS factor_name,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{factor_ids:Array(String)}}, factor_id)
    GROUP BY factor_id
)
""".strip(),
    ]

    needs_security_universe = bool(
        normalized_sector_codes or normalized_industry_group_codes or normalized_market
    )
    regular_security_universe_join = ""
    if needs_security_universe:
        sector_filter = ""
        if normalized_sector_codes:
            sector_filter = "\n        AND has({sector_codes:Array(String)}, iss.sector_code)"
        industry_group_filter = ""
        if normalized_industry_group_codes:
            industry_group_filter = (
                "\n        AND has({industry_group_codes:Array(String)}, iss.industry_group_code)"
            )
        market_filter = ""
        if normalized_market:
            market_filter = "\n        AND sm.country = {market_country:String}"
        ctes.append(
            f"""
security_universe AS (
    SELECT
        sm.security_id AS security_id
    FROM {_validate_table_name(security_table)} AS sm
    LEFT JOIN {_validate_table_name(issuer_table)} AS iss
        ON iss.issuer_id = sm.issuer_id
    WHERE 1 = 1{market_filter}{sector_filter}{industry_group_filter}
    GROUP BY sm.security_id
)
""".strip()
        )
        regular_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = f.security_id"
        )

    aggregate_sql = ",\n        ".join(aggregate_columns)
    expanded_points_sql = ",\n                ".join(expanded_points)
    ctes.append(
        f"""
wide_latest_factor_values AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        any(c.factor_name) AS factor_name,
        any(c.value_direction) AS value_direction,
        {aggregate_sql}
    FROM {_validate_table_name(factor_table)} AS f
    INNER JOIN selected_catalog AS c
        ON c.factor_id = f.factor_id{regular_security_universe_join}
    WHERE f.trade_date <= {{max_signal_date:Date}}
        {raw_lookback_filter.strip()}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
        AND isFinite(f.factor_value){basis_filter}
    GROUP BY
        f.security_id,
        f.factor_id
),
latest_factor_values AS (
    SELECT
        tupleElement(point, 1) AS signal_date,
        security_id,
        factor_id,
        factor_name,
        value_direction,
        tupleElement(point, 2) AS factor_value,
        tupleElement(point, 3) AS trade_date
    FROM (
        SELECT
            security_id,
            factor_id,
            factor_name,
            value_direction,
            arrayJoin([
                {expanded_points_sql}
            ]) AS point
        FROM wide_latest_factor_values
    )
    WHERE tupleElement(point, 3) > toDate(0)
)
""".strip()
    )
    ctes.append(
        """
ranked_factor_values AS (
    SELECT
        signal_date,
        security_id,
        factor_id,
        factor_name,
        value_direction,
        factor_value,
        trade_date,
        row_number() OVER (PARTITION BY signal_date, factor_id ORDER BY factor_value DESC, security_id ASC) AS rank_high,
        row_number() OVER (PARTITION BY signal_date, factor_id ORDER BY factor_value ASC, security_id ASC) AS rank_low,
        count() OVER (PARTITION BY signal_date, factor_id) AS factor_count
    FROM latest_factor_values
)
""".strip()
    )
    with_sql = "WITH\n" + ",\n".join(ctes)
    query = f"""
{with_sql}
SELECT
    rf.signal_date AS signal_date,
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
    rf.signal_date,
    rf.security_id,
    rf.factor_id
ORDER BY
    rf.signal_date ASC,
    rf.security_id ASC,
    rf.factor_id ASC
""".strip()
    return query, params


def build_trading_days_query(
    *,
    start_date: str | date,
    end_date: str | date,
    market: str | None = None,
    lookback_days: int = 10,
    price_table: str = "price_daily",
    security_table: str = "security_master",
) -> tuple[str, dict[str, Any]]:
    start = date.fromisoformat(_resolve_date(start_date)) - timedelta(days=lookback_days)
    normalized_market = _normalize_market(market)
    market_join = ""
    market_filter = ""
    params: dict[str, Any] = {
        "start_date": start.isoformat(),
        "end_date": _resolve_date(end_date),
    }
    if normalized_market:
        params["market_country"] = normalized_market.upper()
        market_join = (
            f"\nINNER JOIN {_validate_table_name(security_table)} AS sm"
            "\n    ON sm.security_id = p.security_id"
        )
        market_filter = "\n    AND sm.country = {market_country:String}"
    query = f"""
SELECT DISTINCT trade_date
FROM {_validate_table_name(price_table)} AS p{market_join}
WHERE p.trade_date >= {{start_date:Date}}
    AND p.trade_date <= {{end_date:Date}}{market_filter}
ORDER BY trade_date ASC
""".strip()
    return query, params


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


def build_portfolio_return_query(
    *,
    segments: list[dict[str, Any]],
    trading_days: list[str | date],
    price_table: str = "price_daily",
) -> tuple[str, dict[str, Any], list[tuple[int, str, str, str, float]]]:
    """Calculate equal-weight segment returns in ClickHouse."""

    if not segments:
        raise ValueError("segments must not be empty")
    normalized_trading_days = sorted({_resolve_date(value) for value in trading_days})
    if not normalized_trading_days:
        raise ValueError("trading_days must not be empty")

    segment_ids: list[int] = []
    position_security_ids: list[str] = []
    segment_start_dates: list[str] = []
    segment_end_dates: list[str] = []
    transaction_cost_bps: list[float] = []
    all_security_ids: set[str] = set()
    all_segment_start_dates: list[str] = []
    all_segment_end_dates: list[str] = []
    for segment_id, segment in enumerate(segments):
        start_date = _resolve_date(segment["start_date"])
        end_date = _resolve_date(segment["end_date"])
        if start_date > end_date:
            raise ValueError("segment start_date must not be later than end_date")
        security_ids = sorted({str(value) for value in segment.get("security_ids") or []})
        if not security_ids:
            continue
        all_segment_start_dates.append(start_date)
        all_segment_end_dates.append(end_date)
        cost_bps = float(segment.get("transaction_cost_bps") or 0.0)
        for security_id in security_ids:
            segment_ids.append(segment_id)
            position_security_ids.append(security_id)
            segment_start_dates.append(start_date)
            segment_end_dates.append(end_date)
            transaction_cost_bps.append(cost_bps)
            all_security_ids.add(security_id)
    if not position_security_ids:
        raise ValueError("segments must contain at least one security_id")

    position_rows = list(
        zip(
            segment_ids,
            position_security_ids,
            segment_start_dates,
            segment_end_dates,
            transaction_cost_bps,
            strict=True,
        )
    )
    params = {
        "security_ids": sorted(all_security_ids),
        "trading_days": normalized_trading_days,
        "start_date": min(all_segment_start_dates),
        "end_date": max(all_segment_end_dates),
    }
    query = f"""
WITH
raw_prices AS (
    SELECT
        prices_source.security_id AS security_id,
        prices_source.trade_date AS trade_date,
        toFloat64(argMax(prices_source.close, prices_source.updated_at)) AS close
    FROM {_validate_table_name(price_table)} AS prices_source
    WHERE has({{security_ids:Array(String)}}, prices_source.security_id)
        AND prices_source.trade_date >= {{start_date:Date}}
        AND prices_source.trade_date <= {{end_date:Date}}
        AND prices_source.trade_date IN {{trading_days:Array(Date)}}
        AND prices_source.close IS NOT NULL
    GROUP BY prices_source.security_id, prices_source.trade_date
),
prices_with_lag AS (
    SELECT
        security_id,
        trade_date,
        close,
        lagInFrame(close, 1, NULL) OVER (
            PARTITION BY security_id
            ORDER BY trade_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        ) AS previous_close
    FROM raw_prices
),
security_returns AS (
    SELECT
        security_id,
        trade_date,
        if(
            close IS NULL OR previous_close IS NULL OR previous_close = 0,
            NULL,
            close / previous_close - 1
        ) AS daily_return
    FROM prices_with_lag
),
position_day_returns AS (
    SELECT
        positions.segment_id AS segment_id,
        positions.transaction_cost_bps AS transaction_cost_bps,
        returns.trade_date AS trade_date,
        returns.daily_return AS daily_return
    FROM portfolio_positions AS positions
    INNER JOIN security_returns AS returns
        ON returns.security_id = positions.security_id
        AND returns.trade_date >= positions.start_date
        AND returns.trade_date <= positions.end_date
),
segment_daily_returns AS (
    SELECT
        segment_id,
        trade_date,
        any(transaction_cost_bps) AS transaction_cost_bps,
        if(
            countIf(daily_return IS NOT NULL AND isFinite(daily_return)) = 0,
            0.0,
            avgIf(
                daily_return,
                daily_return IS NOT NULL AND isFinite(daily_return)
            )
        ) AS daily_return
    FROM position_day_returns
    GROUP BY segment_id, trade_date
),
ranked_segment_returns AS (
    SELECT
        segment_id,
        trade_date,
        transaction_cost_bps,
        daily_return,
        row_number() OVER (
            PARTITION BY segment_id
            ORDER BY trade_date
        ) AS segment_day_number
    FROM segment_daily_returns
)
SELECT
    segment_id,
    trade_date,
    daily_return - if(
        segment_day_number = 1,
        transaction_cost_bps / 10000.0,
        0.0
    ) AS daily_return
FROM ranked_segment_returns
ORDER BY segment_id ASC, trade_date ASC
""".strip()
    return query, params, position_rows


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
        factor_id = _validate_factor_id(canonical_factor_id(condition.factor_id))
        if factor_id == condition.factor_id:
            return condition
        return FactorCondition(
            factor_id=factor_id,
            mode=condition.mode,
            top_percent=condition.top_percent,
            rank_direction=condition.rank_direction,
            percentile_side=condition.percentile_side,
            operator=condition.operator,
            value=condition.value,
            min_value=condition.min_value,
            max_value=condition.max_value,
            alias=condition.alias,
        )
    if isinstance(condition, dict):
        condition = {**condition, "factor_id": canonical_factor_id(condition["factor_id"])}
        coerced = FactorCondition(**condition)
        _validate_factor_id(coerced.factor_id)
        return coerced
    raise TypeError("conditions must contain FactorCondition instances or dictionaries")


def _build_style_score_snapshot_sources(
    factor_ids: list[str],
    *,
    style_score_table: str,
    security_universe_join: str,
) -> list[str]:
    sources = []
    for factor_id in factor_ids:
        definition = style_score_factor_definition(factor_id)
        column_name = _validate_style_score_column(definition.column_name)
        sources.append(
            f"""
    SELECT
        s.security_id AS security_id,
        '{definition.factor_id}' AS factor_id,
        '{_escape_sql_string(definition.factor_name)}' AS factor_name,
        '{definition.value_direction}' AS value_direction,
        argMax(toFloat64(s.{column_name}), tuple(s.trade_date, s.updated_at)) AS factor_value,
        max(s.trade_date) AS trade_date
    FROM {_validate_table_name(style_score_table)} AS s{security_universe_join}
    WHERE s.trade_date <= {{signal_date:Date}}
        AND s.style_profile = {{style_profile:String}}
        AND s.{column_name} IS NOT NULL
        AND isFinite(toFloat64(s.{column_name}))
    GROUP BY s.security_id
    HAVING factor_value >= 0
""".strip()
        )
    return sources


def _validate_style_score_column(column_name: str) -> str:
    allowed = {definition.column_name for definition in STYLE_SCORE_FACTORS.values()}
    if column_name not in allowed:
        raise ValueError(f"invalid style score column: {column_name!r}")
    return column_name


def _normalize_style_profile(value: str | None) -> str:
    return str(value or DEFAULT_SCREEN_STYLE_PROFILE).strip().upper()


def _normalize_market(value: str | None) -> str | None:
    market = str(value or "").strip().lower()
    if not market or market == "all":
        return None
    if market not in {"kr", "us"}:
        raise ValueError("market must be one of: all, kr, us")
    return market


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _validate_factor_ids(factor_ids: list[str]) -> list[str]:
    if not factor_ids:
        raise ValueError("factor_ids must not be empty")
    normalized = []
    for factor_id in factor_ids:
        normalized.append(_validate_factor_id(canonical_factor_id(factor_id)))
    return list(dict.fromkeys(normalized))


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


def _validate_classification_codes(codes: list[str], field_name: str) -> list[str]:
    if not codes:
        raise ValueError(f"{field_name} must not be empty")
    normalized_codes = []
    for code in codes:
        if not isinstance(code, str) or not _CLASSIFICATION_CODE_RE.match(code):
            raise ValueError(f"invalid {field_name}: {code!r}")
        normalized_codes.append(code)
    return normalized_codes


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
