from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from api.service.style_score_catalog import (
    DEFAULT_SCREEN_STYLE_PROFILE,
    STYLE_SCORE_FACTORS,
    canonical_style_score_factor_id,
    is_style_score_factor,
    style_score_factor_definition,
)


ConditionMode = Literal["top_percent", "threshold"]
MatchMode = Literal["all", "any"]
RankDirection = Literal["catalog", "higher", "lower"]


DEFAULT_FINANCIAL_BASIS = "annual"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SECTOR_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_OPERATORS = {
    ">": ">",
    "gt": ">",
    ">=": ">=",
    "gte": ">=",
    "<": "<",
    "lt": "<",
    "<=": "<=",
    "lte": "<=",
    "=": "=",
    "==": "=",
    "eq": "=",
    "!=": "!=",
    "<>": "!=",
    "ne": "!=",
}


def _factor_value_expr(alias: str) -> str:
    return f"{alias}.factor_value"


@dataclass(frozen=True)
class FactorCondition:
    """A single dynamic stock screening condition for a factor."""

    factor_id: str
    mode: ConditionMode
    top_percent: float | None = None
    rank_direction: RankDirection = "catalog"
    operator: str | None = None
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    alias: str | None = None

    @classmethod
    def top(
        cls,
        factor_id: str,
        top_percent: float,
        *,
        rank_direction: RankDirection = "catalog",
        alias: str | None = None,
    ) -> "FactorCondition":
        return cls(
            factor_id=factor_id,
            mode="top_percent",
            top_percent=top_percent,
            rank_direction=rank_direction,
            alias=alias,
        )

    @classmethod
    def threshold(
        cls,
        factor_id: str,
        operator: str,
        value: float | None = None,
        *,
        min_value: float | None = None,
        max_value: float | None = None,
        alias: str | None = None,
    ) -> "FactorCondition":
        return cls(
            factor_id=factor_id,
            mode="threshold",
            operator=operator,
            value=value,
            min_value=min_value,
            max_value=max_value,
            alias=alias,
        )


def build_latest_factor_values_query(
    factor_ids: list[str],
    *,
    as_of_date: str | date | None = None,
    financial_basis: str | None = DEFAULT_FINANCIAL_BASIS,
    factor_table: str = "fact_daily_factors",
    catalog_table: str = "factor_catalog",
) -> tuple[str, dict[str, Any]]:
    """Build a ClickHouse query for the latest values per stock/factor as of a date."""

    factor_ids = _validate_factor_ids(factor_ids)
    params: dict[str, Any] = {
        "as_of_date": _resolve_as_of_date(as_of_date),
        "factor_ids": factor_ids,
    }
    basis_filter = ""
    basis_date_filter = ""
    if financial_basis:
        params["financial_basis"] = financial_basis
        basis_filter = "\n    AND f.financial_basis = {financial_basis:String}"
        basis_date_filter = "\n    AND financial_basis = {financial_basis:String}"

    query = f"""
WITH
selected_catalog AS (
    SELECT
        factor_id AS factor_id,
        any(factor_name) AS factor_name,
        any(factor_type) AS factor_type,
        any(factor_group) AS factor_group,
        any(unit) AS unit,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{factor_ids:Array(String)}}, factor_id)
    GROUP BY factor_id
),
latest_trade_date AS (
    SELECT
        trade_date AS trade_date
    FROM {_validate_table_name(factor_table)}
    WHERE trade_date <= {{as_of_date:Date}}{basis_date_filter}
    ORDER BY trade_date DESC
    LIMIT 1
)
SELECT
    f.security_id AS security_id,
    f.factor_id AS factor_id,
    c.factor_name AS factor_name,
    c.factor_type AS factor_type,
    c.factor_group AS factor_group,
    c.unit AS unit,
    c.value_direction AS value_direction,
    argMax({_factor_value_expr("f")}, tuple(f.trade_date, f.updated_at)) AS factor_value,
    max(f.trade_date) AS trade_date,
    argMax(f.financial_basis, tuple(f.trade_date, f.updated_at)) AS financial_basis,
    argMax(f.fiscal_year, tuple(f.trade_date, f.updated_at)) AS fiscal_year,
    argMax(f.financial_period, tuple(f.trade_date, f.updated_at)) AS financial_period,
    argMax(f.currency, tuple(f.trade_date, f.updated_at)) AS currency,
    max(f.updated_at) AS updated_at
FROM {_validate_table_name(factor_table)} AS f
INNER JOIN selected_catalog AS c
    ON c.factor_id = f.factor_id
WHERE f.trade_date = (SELECT trade_date FROM latest_trade_date)
    AND has({{factor_ids:Array(String)}}, f.factor_id)
    AND isFinite(f.factor_value){basis_filter}
GROUP BY
    f.security_id,
    f.factor_id,
    c.factor_name,
    c.factor_type,
    c.factor_group,
    c.unit,
    c.value_direction
HAVING factor_value >= 0
""".strip()
    return query, params


def query_latest_factor_values(
    client: Any,
    factor_ids: list[str],
    *,
    as_of_date: str | date | None = None,
    financial_basis: str | None = DEFAULT_FINANCIAL_BASIS,
    factor_table: str = "fact_daily_factors",
    catalog_table: str = "factor_catalog",
):
    """Execute the latest-factor-values query with a clickhouse-connect client."""

    query, params = build_latest_factor_values_query(
        factor_ids,
        as_of_date=as_of_date,
        financial_basis=financial_basis,
        factor_table=factor_table,
        catalog_table=catalog_table,
    )
    return client.query_df(query, parameters=params)


def build_factor_screen_query(
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    as_of_date: str | date | None = None,
    financial_basis: str | None = DEFAULT_FINANCIAL_BASIS,
    style_profile: str | None = DEFAULT_SCREEN_STYLE_PROFILE,
    sector_codes: list[str] | None = None,
    industry_group_codes: list[str] | None = None,
    match_mode: MatchMode = "all",
    limit: int | None = None,
    factor_table: str = "fact_daily_factors",
    style_score_table: str = "arcana.fact_daily_style_score",
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
    include_security_metadata: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build a ClickHouse stock screening query from dynamic factor conditions.

    The query first fetches the latest value per stock/factor as of ``as_of_date``.
    Then each condition can independently apply either a top-N-percent rule or a
    threshold rule. ``match_mode="all"`` requires every condition to match.
    """

    normalized_conditions = [_coerce_condition(condition) for condition in conditions]
    if not normalized_conditions:
        raise ValueError("at least one factor condition is required")
    if match_mode not in {"all", "any"}:
        raise ValueError("match_mode must be 'all' or 'any'")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")

    normalized_sector_codes = _validate_sector_codes(sector_codes) if sector_codes else None
    normalized_industry_group_codes = (
        _validate_sector_codes(industry_group_codes) if industry_group_codes else None
    )
    factor_ids = _validate_factor_ids(
        sorted({condition.factor_id for condition in normalized_conditions})
    )
    regular_factor_ids = [factor_id for factor_id in factor_ids if not is_style_score_factor(factor_id)]
    style_factor_ids = [factor_id for factor_id in factor_ids if is_style_score_factor(factor_id)]
    regular_factor_param = "regular_factor_ids" if style_factor_ids else "factor_ids"
    params: dict[str, Any] = {
        "as_of_date": _resolve_as_of_date(as_of_date),
        "factor_ids": factor_ids,
        "required_condition_count": len(normalized_conditions) if match_mode == "all" else 1,
    }
    if regular_factor_ids:
        params[regular_factor_param] = regular_factor_ids
    if style_factor_ids:
        params["style_profile"] = _normalize_style_profile(style_profile)
    if financial_basis:
        params["financial_basis"] = financial_basis
    if normalized_sector_codes:
        params["sector_codes"] = normalized_sector_codes
    if normalized_industry_group_codes:
        params["industry_group_codes"] = normalized_industry_group_codes
    if limit is not None:
        params["limit"] = int(limit)

    basis_filter = ""
    basis_date_filter = ""
    if financial_basis:
        basis_filter = "\n        AND f.financial_basis = {financial_basis:String}"
        basis_date_filter = "\n        AND financial_basis = {financial_basis:String}"

    needs_security_universe = bool(
        normalized_sector_codes
        or normalized_industry_group_codes
        or include_security_metadata
    )
    security_universe_cte = ""
    regular_security_universe_join = ""
    style_security_universe_join = ""
    metadata_select_sql = ""
    if needs_security_universe:
        sector_filter = ""
        if normalized_sector_codes:
            sector_filter = "\n        AND has({sector_codes:Array(String)}, iss.sector_code)"
        industry_group_filter = ""
        if normalized_industry_group_codes:
            industry_group_filter = (
                "\n        AND has({industry_group_codes:Array(String)}, iss.industry_group_code)"
            )
        security_universe_cte = f""",
security_universe AS (
    SELECT
        sm.security_id AS security_id,
        any(id.ticker) AS ticker,
        any(iss.legal_name_ko) AS issuer_name,
        any(iss.legal_name_en) AS issuer_name_en,
        any(iss.domicile_country) AS country,
        any(iss.sector_code) AS sector_code,
        any(iss.industry_group_code) AS industry_group_code,
        any(iss.industry_group_name) AS industry_group_name,
        any(mcap.market_cap) AS market_cap
    FROM {_validate_table_name(security_table)} AS sm
    LEFT JOIN {_validate_table_name(issuer_table)} AS iss
        ON iss.issuer_id = sm.issuer_id
    LEFT JOIN (
        SELECT
            security_id,
            any(id_value) AS ticker
        FROM {_validate_table_name(identifier_table)}
        WHERE id_type = 'TICKER'
            AND is_primary
        GROUP BY security_id
    ) AS id
        ON id.security_id = sm.security_id
    LEFT JOIN (
        SELECT
            security_id,
            argMax(factor_value, tuple(trade_date, updated_at)) AS market_cap
        FROM {_validate_table_name(factor_table)}
        WHERE factor_id = 'mcap_mil'
            AND trade_date = (SELECT trade_date FROM latest_trade_date)
            AND isFinite(factor_value)
        GROUP BY security_id
    ) AS mcap
        ON mcap.security_id = sm.security_id
    WHERE sm.is_active{sector_filter}{industry_group_filter}
    GROUP BY sm.security_id
)"""
        regular_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = f.security_id"
        )
        style_security_universe_join = (
            "\n    INNER JOIN security_universe AS u\n        ON u.security_id = s.security_id"
        )
        if include_security_metadata:
            metadata_select_sql = (
                "    any(u.ticker) AS ticker,\n"
                "    any(u.issuer_name) AS issuer_name,\n"
                "    any(u.issuer_name_en) AS issuer_name_en,\n"
                "    any(u.country) AS country,\n"
                "    any(u.sector_code) AS sector_code,\n"
                "    any(u.industry_group_code) AS industry_group_code,\n"
                "    any(u.industry_group_name) AS industry_group_name,\n"
                "    any(u.market_cap) AS market_cap,\n"
            )

    match_count_parts: list[str] = []
    matched_condition_parts: list[str] = []
    value_selects: list[str] = []
    for index, condition in enumerate(normalized_conditions):
        condition_id_param = f"condition_{index}_id"
        factor_id_param = f"condition_{index}_factor_id"
        condition_id = _condition_id(condition, index)
        alias = _condition_alias(condition, index)
        params[condition_id_param] = condition_id
        params[factor_id_param] = condition.factor_id

        match_expr = (
            f"(factor_id = {{{factor_id_param}:String}} "
            f"AND {_condition_predicate(condition, index, params)})"
        )
        match_count_parts.append(f"countIf({match_expr})")
        matched_condition_parts.append(
            f"if(countIf({match_expr}) > 0, [{{{condition_id_param}:String}}], emptyArrayString())"
        )
        value_selects.extend(
            [
                f"    maxIf(factor_value, {match_expr}) AS {alias}_value",
                f"    maxIf(trade_date, {match_expr}) AS {alias}_trade_date",
            ]
        )

    limit_clause = "\nLIMIT {limit:UInt64}" if limit is not None else ""
    having_clause = "matched_condition_count >= {required_condition_count:UInt32}"
    match_count_sql = " + ".join(match_count_parts)
    matched_conditions_sql = "arrayConcat(" + ", ".join(matched_condition_parts) + ")"
    value_select_sql = ",\n".join(value_selects)

    ctes = [
        f"""
latest_trade_date AS (
    SELECT
        trade_date AS trade_date
    FROM {_validate_table_name(factor_table)}
    WHERE trade_date <= {{as_of_date:Date}}{basis_date_filter}
    ORDER BY trade_date DESC
    LIMIT 1
)""".strip()
    ]
    if regular_factor_ids:
        ctes.append(
            f"""
selected_catalog AS (
    SELECT
        factor_id AS factor_id,
        any(factor_name) AS factor_name,
        any(value_direction) AS value_direction
    FROM {_validate_table_name(catalog_table)}
    WHERE is_active
        AND has({{{regular_factor_param}:Array(String)}}, factor_id)
    GROUP BY factor_id
)
""".strip()
        )
    if security_universe_cte:
        ctes.append(security_universe_cte.lstrip(",").strip())
    if style_factor_ids:
        ctes.append(
            f"""
latest_style_trade_date AS (
    SELECT
        trade_date AS trade_date
    FROM {_validate_table_name(style_score_table)}
    WHERE trade_date <= {{as_of_date:Date}}
        AND style_profile = {{style_profile:String}}
    ORDER BY trade_date DESC
    LIMIT 1
)
""".strip()
        )

    latest_factor_sources = []
    if regular_factor_ids:
        latest_factor_sources.append(
            f"""
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        c.factor_name AS factor_name,
        c.value_direction AS value_direction,
        argMax({_factor_value_expr("f")}, tuple(f.trade_date, f.updated_at)) AS factor_value,
        max(f.trade_date) AS trade_date
    FROM {_validate_table_name(factor_table)} AS f
    INNER JOIN selected_catalog AS c
        ON c.factor_id = f.factor_id{regular_security_universe_join}
    WHERE f.trade_date = (SELECT trade_date FROM latest_trade_date)
        AND has({{{regular_factor_param}:Array(String)}}, f.factor_id)
        AND isFinite(f.factor_value){basis_filter}
    GROUP BY
        f.security_id,
        f.factor_id,
        c.factor_name,
        c.value_direction
    HAVING factor_value >= 0
""".strip()
        )
    latest_factor_sources.extend(
        _build_style_score_value_sources(
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
scored_factors AS (
    SELECT
        lv.security_id AS security_id,
        lv.factor_id AS factor_id,
        lv.factor_name AS factor_name,
        lv.value_direction AS value_direction,
        lv.factor_value AS factor_value,
        lv.trade_date AS trade_date,
        row_number() OVER (PARTITION BY lv.factor_id ORDER BY lv.factor_value DESC, lv.security_id ASC) AS rank_high,
        row_number() OVER (PARTITION BY lv.factor_id ORDER BY lv.factor_value ASC, lv.security_id ASC) AS rank_low,
        count() OVER (PARTITION BY lv.factor_id) AS factor_count
    FROM latest_factor_values AS lv
)
""".strip()
    )
    with_sql = "WITH\n" + ",\n".join(ctes)

    query = f"""
{with_sql}
SELECT
    sf.security_id AS security_id,
{metadata_select_sql}    {match_count_sql} AS matched_condition_count,
    {matched_conditions_sql} AS matched_conditions,
    max(trade_date) AS latest_trade_date,
{value_select_sql}
FROM scored_factors AS sf
{"LEFT JOIN security_universe AS u ON u.security_id = sf.security_id" if include_security_metadata else ""}
GROUP BY sf.security_id
HAVING {having_clause}
ORDER BY security_id ASC{limit_clause}
""".strip()
    query = _clean_query(query)
    return query, params


def screen_stocks_by_factors(
    client: Any,
    conditions: list[FactorCondition | dict[str, Any]],
    *,
    as_of_date: str | date | None = None,
    financial_basis: str | None = DEFAULT_FINANCIAL_BASIS,
    style_profile: str | None = DEFAULT_SCREEN_STYLE_PROFILE,
    sector_codes: list[str] | None = None,
    industry_group_codes: list[str] | None = None,
    match_mode: MatchMode = "all",
    limit: int | None = None,
    factor_table: str = "fact_daily_factors",
    style_score_table: str = "arcana.fact_daily_style_score",
    catalog_table: str = "factor_catalog",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
    include_security_metadata: bool = False,
):
    """Execute a dynamic factor screen with a clickhouse-connect client."""

    query, params = build_factor_screen_query(
        conditions,
        as_of_date=as_of_date,
        financial_basis=financial_basis,
        style_profile=style_profile,
        sector_codes=sector_codes,
        industry_group_codes=industry_group_codes,
        match_mode=match_mode,
        limit=limit,
        factor_table=factor_table,
        style_score_table=style_score_table,
        catalog_table=catalog_table,
        security_table=security_table,
        issuer_table=issuer_table,
        identifier_table=identifier_table,
        include_security_metadata=include_security_metadata,
    )
    return client.query_df(query, parameters=params)


def _condition_predicate(
    condition: FactorCondition,
    index: int,
    params: dict[str, Any],
) -> str:
    if condition.mode == "top_percent":
        if condition.top_percent is None:
            raise ValueError(f"condition {index} is missing top_percent")
        top_percent = float(condition.top_percent)
        if not math.isfinite(top_percent) or top_percent <= 0 or top_percent > 100:
            raise ValueError(f"condition {index} top_percent must be > 0 and <= 100")
        params[f"condition_{index}_top_percent"] = top_percent
        rank_expr = _rank_expression(condition.rank_direction)
        return (
            f"{rank_expr} <= greatest("
            "toUInt64(1), "
            f"toUInt64(ceil(factor_count * ({{condition_{index}_top_percent:Float64}} / 100.0)))"
            ")"
        )

    if condition.mode == "threshold":
        operator = (condition.operator or "").lower()
        if operator == "between":
            if condition.min_value is None or condition.max_value is None:
                raise ValueError(f"condition {index} between requires min_value and max_value")
            min_value = float(condition.min_value)
            max_value = float(condition.max_value)
            if not math.isfinite(min_value) or not math.isfinite(max_value):
                raise ValueError(f"condition {index} between values must be finite")
            if min_value > max_value:
                raise ValueError(f"condition {index} min_value must be <= max_value")
            params[f"condition_{index}_min_value"] = min_value
            params[f"condition_{index}_max_value"] = max_value
            return (
                f"factor_value BETWEEN {{condition_{index}_min_value:Float64}} "
                f"AND {{condition_{index}_max_value:Float64}}"
            )

        if operator not in _OPERATORS:
            allowed = ", ".join(sorted([*set(_OPERATORS), "between"]))
            raise ValueError(f"condition {index} operator must be one of: {allowed}")
        if condition.value is None:
            raise ValueError(f"condition {index} threshold requires value")
        value = float(condition.value)
        if not math.isfinite(value):
            raise ValueError(f"condition {index} threshold value must be finite")
        params[f"condition_{index}_value"] = value
        return f"factor_value {_OPERATORS[operator]} {{condition_{index}_value:Float64}}"

    raise ValueError(f"condition {index} mode must be 'top_percent' or 'threshold'")


def _rank_expression(rank_direction: RankDirection) -> str:
    if rank_direction == "higher":
        return "rank_high"
    if rank_direction == "lower":
        return "rank_low"
    if rank_direction == "catalog":
        return "if(value_direction = 'LOWER_BETTER', rank_low, rank_high)"
    raise ValueError("rank_direction must be 'catalog', 'higher', or 'lower'")


def _coerce_condition(condition: FactorCondition | dict[str, Any]) -> FactorCondition:
    if isinstance(condition, FactorCondition):
        factor_id = _validate_factor_id(canonical_style_score_factor_id(condition.factor_id))
        if factor_id == condition.factor_id:
            return condition
        return FactorCondition(
            factor_id=factor_id,
            mode=condition.mode,
            top_percent=condition.top_percent,
            rank_direction=condition.rank_direction,
            operator=condition.operator,
            value=condition.value,
            min_value=condition.min_value,
            max_value=condition.max_value,
            alias=condition.alias,
        )
    if isinstance(condition, dict):
        condition = {**condition, "factor_id": canonical_style_score_factor_id(condition["factor_id"])}
        coerced = FactorCondition(**condition)
        _validate_factor_id(coerced.factor_id)
        return coerced
    raise TypeError("conditions must contain FactorCondition instances or dictionaries")


def _build_style_score_value_sources(
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
        toFloat64(s.{column_name}) AS factor_value,
        s.trade_date AS trade_date
    FROM {_validate_table_name(style_score_table)} AS s{security_universe_join}
    WHERE s.trade_date = (SELECT trade_date FROM latest_style_trade_date)
        AND s.style_profile = {{style_profile:String}}
        AND s.{column_name} IS NOT NULL
        AND isFinite(toFloat64(s.{column_name}))
        AND toFloat64(s.{column_name}) >= 0
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


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _condition_id(condition: FactorCondition, index: int) -> str:
    alias = condition.alias or condition.factor_id
    return f"{index}:{condition.mode}:{alias}"


def _condition_alias(condition: FactorCondition, index: int) -> str:
    raw_alias = condition.alias or condition.factor_id
    alias = re.sub(r"[^A-Za-z0-9_]+", "_", raw_alias).strip("_").lower()
    if not alias or alias[0].isdigit():
        alias = f"factor_{alias}"
    return f"{alias}_{index}"


def _validate_factor_ids(factor_ids: list[str]) -> list[str]:
    if not factor_ids:
        raise ValueError("factor_ids must not be empty")
    return [_validate_factor_id(factor_id) for factor_id in factor_ids]


def _validate_factor_id(factor_id: str) -> str:
    if not isinstance(factor_id, str) or not _FACTOR_ID_RE.match(factor_id):
        raise ValueError(f"invalid factor_id: {factor_id!r}")
    return factor_id


def _validate_sector_codes(sector_codes: list[str]) -> list[str]:
    if not sector_codes:
        raise ValueError("sector_codes must not be empty")
    normalized_codes = []
    for sector_code in sector_codes:
        if not isinstance(sector_code, str) or not _SECTOR_CODE_RE.match(sector_code):
            raise ValueError(f"invalid sector_code: {sector_code!r}")
        normalized_codes.append(sector_code)
    return normalized_codes


def _validate_table_name(table_name: str) -> str:
    if not isinstance(table_name, str) or not _IDENTIFIER_RE.match(table_name):
        raise ValueError(f"invalid table name: {table_name!r}")
    return table_name


def _resolve_as_of_date(value: str | date | None) -> str:
    if value is None:
        try:
            return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        except Exception:
            return datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    raise TypeError("as_of_date must be a date, ISO date string, or None")


def _clean_query(query: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", query)
