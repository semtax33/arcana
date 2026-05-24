from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.style_score_definitions import (
    STYLE_FACTOR_DEFINITIONS,
    STYLE_WEIGHTS,
    VALUE_LIMITS,
    canonical_factor_id,
    factor_direction,
    style_profile_weights,
)


FACTOR_TABLE_CANDIDATES = (
    "arcana.fact_daily_factor",
    "arcana.fact_daily_factors",
    "fact_daily_factor",
    "fact_daily_factors",
)
FACTOR_VALUE_COLUMNS = ("factor_value", "value")
MIN_INDUSTRY_GROUP_PEERS = 20
MIN_SECTOR_PEERS = 10
MIN_SCORE_PEERS = 5
STYLE_SCORE_COLUMNS = {
    "VALUE": "value_score",
    "QUALITY": "quality_score",
    "GROWTH": "growth_score",
    "MOMENTUM": "momentum_score",
    "RISK": "risk_score",
    "DIVIDEND": "dividend_score",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


@dataclass(frozen=True)
class FactorTableSchema:
    table_name: str
    value_column: str
    has_financial_basis: bool
    has_updated_at: bool
    has_source_trade_date: bool


@dataclass(frozen=True)
class StyleScoreRangeBuildResult:
    start_date: date
    end_date: date
    style_profile: str
    trade_dates: list[date]
    processed_dates: list[date]
    skipped_dates: list[date]
    empty_factor_score_dates: list[date]
    empty_style_score_dates: list[date]
    factor_score_rows: int = 0
    industry_snapshot_rows: int = 0
    style_score_rows: int = 0


def build_factor_scores(
    trade_date: str | date,
    *,
    factor_asof_mode: str = "exact",
    include_price_factors: bool = True,
    exclude_financials: bool = True,
    financial_basis: str | None = "annual",
    client_factory: Callable[[], Any] = get_clickhouse_client,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    target_date = _resolve_date(trade_date)
    client = client_factory()
    try:
        schema = detect_factor_table_schema(client)
        universe = load_universe(client, target_date)
        factor_ids = sorted(STYLE_FACTOR_DEFINITIONS)
        factors = load_factor_values(
            client,
            target_date,
            schema=schema,
            factor_ids=factor_ids,
            factor_asof_mode=factor_asof_mode,
            financial_basis=financial_basis,
        )
        factor_scores = calculate_factor_scores(
            universe,
            factors,
            trade_date=target_date,
            exclude_financials=exclude_financials,
        )
        snapshot = build_industry_snapshot(factor_scores)
        if not factor_scores.empty:
            client.insert_df(
                "fact_daily_factor_score",
                factor_scores,
                column_names=list(factor_scores.columns),
            )
        if not snapshot.empty:
            client.insert_df(
                "industry_factor_daily_snapshot",
                snapshot,
                column_names=list(snapshot.columns),
            )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return factor_scores, snapshot


def build_style_scores(
    trade_date: str | date,
    *,
    style_profile: str = "DEFAULT",
    client_factory: Callable[[], Any] = get_clickhouse_client,
) -> pd.DataFrame:
    target_date = _resolve_date(trade_date)
    client = client_factory()
    try:
        factor_scores = client.query_df(
            """
SELECT *
FROM arcana.fact_daily_factor_score FINAL
WHERE trade_date = {trade_date:Date}
""".strip(),
            parameters={"trade_date": target_date.isoformat()},
        )
        style_scores = calculate_style_scores(
            factor_scores,
            trade_date=target_date,
            style_profile=style_profile,
        )
        if not style_scores.empty:
            client.insert_df(
                "fact_daily_style_score",
                style_scores,
                column_names=list(style_scores.columns),
            )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return style_scores


def build_style_score_range(
    start_date: str | date,
    end_date: str | date,
    *,
    style_profile: str = "DEFAULT",
    factor_asof_mode: str = "asof",
    build_factor_scores_first: bool = True,
    exclude_financials: bool = True,
    financial_basis: str | None = "annual",
    skip_existing: bool = False,
    price_table: str = "arcana.price_daily",
    client_factory: Callable[[], Any] = get_clickhouse_client,
) -> StyleScoreRangeBuildResult:
    start = _resolve_date(start_date)
    end = _resolve_date(end_date)
    if start > end:
        raise ValueError("start_date must be earlier than or equal to end_date")

    profile = str(style_profile or "DEFAULT").strip().upper()
    style_profile_weights(profile)

    client = client_factory()
    try:
        trade_dates = load_trade_dates(client, start, end, price_table=price_table)
        existing_dates = (
            load_existing_style_score_dates(client, start, end, style_profile=profile)
            if skip_existing
            else set()
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    processed_dates: list[date] = []
    skipped_dates: list[date] = []
    empty_factor_score_dates: list[date] = []
    empty_style_score_dates: list[date] = []
    factor_score_rows = 0
    industry_snapshot_rows = 0
    style_score_rows = 0

    for trade_date in trade_dates:
        if trade_date in existing_dates:
            skipped_dates.append(trade_date)
            continue

        if build_factor_scores_first:
            factor_scores, snapshot = build_factor_scores(
                trade_date,
                factor_asof_mode=factor_asof_mode,
                exclude_financials=exclude_financials,
                financial_basis=financial_basis,
                client_factory=client_factory,
            )
            factor_score_rows += len(factor_scores)
            industry_snapshot_rows += len(snapshot)
            if factor_scores.empty:
                empty_factor_score_dates.append(trade_date)

        style_scores = build_style_scores(
            trade_date,
            style_profile=profile,
            client_factory=client_factory,
        )
        style_score_rows += len(style_scores)
        if style_scores.empty:
            empty_style_score_dates.append(trade_date)
        processed_dates.append(trade_date)

    return StyleScoreRangeBuildResult(
        start_date=start,
        end_date=end,
        style_profile=profile,
        trade_dates=trade_dates,
        processed_dates=processed_dates,
        skipped_dates=skipped_dates,
        empty_factor_score_dates=empty_factor_score_dates,
        empty_style_score_dates=empty_style_score_dates,
        factor_score_rows=factor_score_rows,
        industry_snapshot_rows=industry_snapshot_rows,
        style_score_rows=style_score_rows,
    )


def validate_style_scores(
    trade_date: str | date,
    *,
    style_profile: str = "DEFAULT",
    client_factory: Callable[[], Any] = get_clickhouse_client,
) -> dict[str, Any]:
    target_date = _resolve_date(trade_date)
    client = client_factory()
    try:
        factor_rows = _records(
            client.query_df(
                """
SELECT
    factor_id,
    count() AS row_count,
    countIf(is_valid) AS valid_count,
    countIf(is_missing) AS missing_count,
    countIf(NOT is_valid) AS invalid_count,
    countIf(is_winsorized) AS winsorized_count
FROM arcana.fact_daily_factor_score FINAL
WHERE trade_date = {trade_date:Date}
GROUP BY factor_id
ORDER BY factor_id
""".strip(),
                parameters={"trade_date": target_date.isoformat()},
            )
        )
        style_rows = _records(
            client.query_df(
                """
SELECT
    count() AS scored_count,
    avg(score_confidence) AS avg_confidence,
    min(total_score) AS min_total_score,
    max(total_score) AS max_total_score
FROM arcana.fact_daily_style_score FINAL
WHERE trade_date = {trade_date:Date}
    AND style_profile = {style_profile:String}
""".strip(),
                parameters={
                    "trade_date": target_date.isoformat(),
                    "style_profile": style_profile.upper(),
                },
            )
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return {
        "trade_date": target_date.isoformat(),
        "style_profile": style_profile.upper(),
        "factor_coverage": factor_rows,
        "style_summary": style_rows[0] if style_rows else {},
    }


def debug_single_security_score(
    trade_date: str | date,
    security_id: str,
    *,
    style_profile: str = "DEFAULT",
    client_factory: Callable[[], Any] = get_clickhouse_client,
) -> dict[str, Any]:
    target_date = _resolve_date(trade_date)
    client = client_factory()
    try:
        style_rows = _records(
            client.query_df(
                """
SELECT *
FROM arcana.fact_daily_style_score FINAL
WHERE trade_date = {trade_date:Date}
    AND style_profile = {style_profile:String}
    AND security_id = {security_id:String}
LIMIT 1
""".strip(),
                parameters={
                    "trade_date": target_date.isoformat(),
                    "style_profile": style_profile.upper(),
                    "security_id": security_id,
                },
            )
        )
        factor_rows = _records(
            client.query_df(
                """
SELECT *
FROM arcana.fact_daily_factor_score FINAL
WHERE trade_date = {trade_date:Date}
    AND security_id = {security_id:String}
ORDER BY style_group ASC, factor_id ASC
""".strip(),
                parameters={
                    "trade_date": target_date.isoformat(),
                    "security_id": security_id,
                },
            )
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return {
        "trade_date": target_date.isoformat(),
        "style_profile": style_profile.upper(),
        "security_id": security_id,
        "style_score": style_rows[0] if style_rows else None,
        "factor_breakdown": factor_rows,
    }


def detect_factor_table_schema(client: Any) -> FactorTableSchema:
    last_error: Exception | None = None
    for table_name in FACTOR_TABLE_CANDIDATES:
        try:
            rows = _records(client.query_df(f"DESCRIBE TABLE {table_name}"))
        except Exception as exc:
            last_error = exc
            continue
        column_names = {str(row.get("name") or row.get("column") or "") for row in rows}
        value_column = next((column for column in FACTOR_VALUE_COLUMNS if column in column_names), None)
        required = {"security_id", "trade_date", "factor_id"}
        if value_column and required.issubset(column_names):
            return FactorTableSchema(
                table_name=table_name,
                value_column=value_column,
                has_financial_basis="financial_basis" in column_names,
                has_updated_at="updated_at" in column_names,
                has_source_trade_date="source_trade_date" in column_names,
            )
    if last_error is not None:
        raise RuntimeError("could not detect fact_daily_factor schema") from last_error
    raise RuntimeError("could not detect fact_daily_factor schema")


def load_universe(client: Any, trade_date: date) -> pd.DataFrame:
    return client.query_df(_build_universe_query(), parameters={"trade_date": trade_date.isoformat()})


def load_trade_dates(
    client: Any,
    start_date: str | date,
    end_date: str | date,
    *,
    price_table: str = "arcana.price_daily",
) -> list[date]:
    start = _resolve_date(start_date)
    end = _resolve_date(end_date)
    query = f"""
SELECT DISTINCT trade_date
FROM {_validate_table_name(price_table)}
WHERE trade_date >= {{start_date:Date}}
    AND trade_date <= {{end_date:Date}}
ORDER BY trade_date ASC
""".strip()
    rows = _records(
        client.query_df(
            query,
            parameters={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
    )
    return [_resolve_date(row["trade_date"]) for row in rows]


def load_existing_style_score_dates(
    client: Any,
    start_date: str | date,
    end_date: str | date,
    *,
    style_profile: str = "DEFAULT",
) -> set[date]:
    start = _resolve_date(start_date)
    end = _resolve_date(end_date)
    profile = str(style_profile or "DEFAULT").strip().upper()
    rows = _records(
        client.query_df(
            """
SELECT trade_date
FROM arcana.fact_daily_style_score FINAL
WHERE trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
    AND style_profile = {style_profile:String}
GROUP BY trade_date
ORDER BY trade_date ASC
""".strip(),
            parameters={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "style_profile": profile,
            },
        )
    )
    return {_resolve_date(row["trade_date"]) for row in rows}


def load_factor_values(
    client: Any,
    trade_date: date,
    *,
    schema: FactorTableSchema,
    factor_ids: list[str],
    factor_asof_mode: str = "exact",
    financial_basis: str | None = "annual",
) -> pd.DataFrame:
    mode = str(factor_asof_mode or "exact").lower()
    if mode not in {"exact", "asof"}:
        raise ValueError("factor_asof_mode must be 'exact' or 'asof'")

    basis_filter = ""
    params: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "factor_ids": sorted({canonical_factor_id(factor_id) for factor_id in factor_ids}),
    }
    if financial_basis and schema.has_financial_basis:
        basis_filter = "\n    AND f.financial_basis = {financial_basis:String}"
        params["financial_basis"] = financial_basis

    value_expr = f"f.{schema.value_column}"
    updated_expr = "f.updated_at" if schema.has_updated_at else "now64(3)"
    source_date_expr = "f.source_trade_date" if schema.has_source_trade_date else "f.trade_date"

    if mode == "exact":
        query = f"""
SELECT
    f.security_id,
    f.trade_date,
    f.factor_id,
    toFloat64({value_expr}) AS factor_value,
    {source_date_expr} AS source_trade_date,
    {updated_expr} AS updated_at
FROM {schema.table_name} AS f
WHERE f.trade_date = {{trade_date:Date}}
    AND has({{factor_ids:Array(String)}}, f.factor_id)
    AND {value_expr} IS NOT NULL{basis_filter}
""".strip()
        return client.query_df(query, parameters=params)

    query = f"""
WITH
    (
        SELECT max(f.trade_date)
        FROM {schema.table_name} AS f
        WHERE f.trade_date <= {{trade_date:Date}}
            AND has({{factor_ids:Array(String)}}, f.factor_id)
            AND {value_expr} IS NOT NULL{basis_filter}
    ) AS source_date
SELECT
    f.security_id,
    f.trade_date,
    f.factor_id,
    toFloat64({value_expr}) AS factor_value,
    {source_date_expr} AS source_trade_date,
    {updated_expr} AS updated_at
FROM {schema.table_name} AS f
WHERE f.trade_date = source_date
    AND has({{factor_ids:Array(String)}}, f.factor_id)
    AND {value_expr} IS NOT NULL{basis_filter}
""".strip()
    return client.query_df(query, parameters=params)


def calculate_factor_scores(
    universe_df: pd.DataFrame,
    factor_df: pd.DataFrame,
    *,
    trade_date: str | date,
    exclude_financials: bool = True,
) -> pd.DataFrame:
    target_date = _resolve_date(trade_date)
    if universe_df.empty or factor_df.empty:
        return _empty_factor_score_frame()

    universe = universe_df.copy()
    factors = factor_df.copy()
    factors["factor_id"] = factors["factor_id"].map(canonical_factor_id)
    factors = factors.loc[factors["factor_id"].isin(STYLE_FACTOR_DEFINITIONS)].copy()
    if factors.empty:
        return _empty_factor_score_frame()

    factors["raw_factor_value"] = pd.to_numeric(factors["factor_value"], errors="coerce")
    factors["source_trade_date"] = pd.to_datetime(
        factors.get("source_trade_date", factors.get("trade_date")),
        errors="coerce",
    ).dt.date

    merged = factors.merge(universe, on="security_id", how="inner", suffixes=("", "_u"))
    if "is_financial" not in merged.columns:
        merged["is_financial"] = False
    merged["is_financial"] = merged["is_financial"].fillna(False).astype(bool)
    if exclude_financials:
        merged = merged.loc[~merged["is_financial"]].copy()
    if merged.empty:
        return _empty_factor_score_frame()

    merged = _mark_validity(merged)
    merged = _resolve_industry_fallback(merged)
    merged = _apply_winsorization(merged)
    merged = _apply_percentile_and_robust_z(merged)
    return _format_factor_score_frame(merged, target_date)


def build_industry_snapshot(factor_score_df: pd.DataFrame) -> pd.DataFrame:
    if factor_score_df.empty:
        return _empty_snapshot_frame()
    valid = factor_score_df.loc[factor_score_df["is_valid"]].copy()
    if valid.empty:
        return _empty_snapshot_frame()

    rows = []
    group_columns = [
        "trade_date",
        "industry_schema",
        "industry_level",
        "industry_code",
        "industry_name",
        "factor_id",
    ]
    for keys, group in valid.groupby(group_columns, dropna=False):
        raw = pd.to_numeric(group["raw_factor_value"], errors="coerce").dropna()
        winsor = pd.to_numeric(group["winsorized_value"], errors="coerce").dropna()
        rows.append(
            {
                **dict(zip(group_columns, keys)),
                "n_companies": int(len(raw)),
                "avg_value": _mean_or_none(raw),
                "median_value": _quantile_or_none(raw, 0.50),
                "p10_value": _quantile_or_none(raw, 0.10),
                "p25_value": _quantile_or_none(raw, 0.25),
                "p75_value": _quantile_or_none(raw, 0.75),
                "p90_value": _quantile_or_none(raw, 0.90),
                "winsor_avg_value": _mean_or_none(winsor),
                "updated_at": _now_kst_naive(),
            }
        )
    return pd.DataFrame(rows, columns=_snapshot_columns())


def calculate_style_scores(
    factor_score_df: pd.DataFrame,
    *,
    trade_date: str | date,
    style_profile: str = "DEFAULT",
) -> pd.DataFrame:
    target_date = _resolve_date(trade_date)
    profile = str(style_profile or "DEFAULT").strip().upper()
    weights = style_profile_weights(profile)
    if factor_score_df.empty:
        return _empty_style_score_frame()

    frame = factor_score_df.copy()
    frame["factor_id"] = frame["factor_id"].map(canonical_factor_id)
    frame["percentile_score"] = pd.to_numeric(frame["percentile_score"], errors="coerce")
    rows = []
    for security_id, group in frame.groupby("security_id", sort=False):
        first = group.iloc[0]
        score_row: dict[str, Any] = {
            "trade_date": target_date,
            "security_id": security_id,
            "issuer_id": _string_or_empty(first.get("issuer_id")),
            "stock_code": _string_or_empty(first.get("stock_code")),
            "country": _string_or_empty(first.get("country")),
            "market_mic": _string_or_empty(first.get("market_mic")),
            "company_name": _string_or_empty(first.get("company_name")),
            "industry_schema": _string_or_empty(first.get("industry_schema")),
            "industry_code": _string_or_empty(first.get("industry_code")),
            "industry_name": _string_or_empty(first.get("industry_name")),
            "style_profile": profile,
            "updated_at": _now_kst_naive(),
        }
        available_factor_ids: set[str] = set()
        invalid_factor_ids: set[str] = set()
        style_confidences: dict[str, float] = {}

        for style_group, factor_weights in STYLE_WEIGHTS.items():
            style_score, confidence, available, invalid = _weighted_style_score(
                group,
                factor_weights,
            )
            score_row[STYLE_SCORE_COLUMNS[style_group]] = style_score
            style_confidences[style_group] = confidence
            available_factor_ids.update(available)
            invalid_factor_ids.update(invalid)

        total_score, total_confidence = _weighted_total_score(score_row, style_confidences, weights)
        required_factor_ids = set().union(*(set(items) for items in STYLE_WEIGHTS.values()))
        missing_factor_ids = sorted(required_factor_ids - available_factor_ids - invalid_factor_ids)
        score_row.update(
            {
                "total_score": total_score,
                "total_score_sort": total_score if total_score is not None else -1.0,
                "available_factor_count": len(available_factor_ids),
                "required_factor_count": len(required_factor_ids),
                "score_confidence": total_confidence,
                "missing_factor_ids": missing_factor_ids,
                "invalid_factor_ids": sorted(invalid_factor_ids),
            }
        )
        rows.append(score_row)

    result = pd.DataFrame(rows, columns=_style_score_columns())
    return result.sort_values(["total_score_sort", "security_id"], ascending=[False, True]).reset_index(drop=True)


def _build_universe_query() -> str:
    return """
WITH
    toDate({trade_date:Date}) AS target_date,
    latest_shares AS
    (
        SELECT
            security_id,
            argMax(toFloat64(market_cap), trade_date) AS market_cap
        FROM arcana.stock_shares
        WHERE trade_date <= target_date
        GROUP BY security_id
    ),
    recent_liquidity AS
    (
        SELECT
            security_id,
            avg(toFloat64(close) * toFloat64(volume)) AS avg_trading_value_20d
        FROM
        (
            SELECT
                security_id,
                trade_date,
                close,
                volume,
                row_number() OVER (PARTITION BY security_id ORDER BY trade_date DESC) AS rn
            FROM arcana.price_daily
            WHERE trade_date <= target_date
                AND close IS NOT NULL
                AND volume IS NOT NULL
        )
        WHERE rn <= 20
        GROUP BY security_id
    ),
    stock_codes AS
    (
        SELECT
            security_id,
            any(id_value) AS stock_code
        FROM arcana.identifiers
        WHERE id_type IN ('TICKER', 'STOCK_CODE')
            AND is_primary
        GROUP BY security_id
    )
SELECT
    sm.security_id AS security_id,
    sm.issuer_id AS issuer_id,
    ifNull(sc.stock_code, '') AS stock_code,
    sm.country AS country,
    sm.primary_market_mic AS market_mic,
    i.legal_name_ko AS company_name,
    i.industry_schema AS industry_schema,
    i.sector_code AS sector_code,
    i.industry_group_code AS industry_group_code,
    i.industry_group_name AS industry_group_name,
    multiIf(
        i.industry_group_name LIKE '%금융%', 1,
        i.sector_code IN ('FINANCIALS', '40'), 1,
        i.industry_group_code IN ('FINANCIALS', '40'), 1,
        0
    ) AS is_financial,
    ls.market_cap AS market_cap,
    rl.avg_trading_value_20d AS avg_trading_value_20d
FROM arcana.security_master AS sm
LEFT JOIN arcana.issuers AS i
    ON sm.issuer_id = i.issuer_id
LEFT JOIN latest_shares AS ls
    ON sm.security_id = ls.security_id
LEFT JOIN recent_liquidity AS rl
    ON sm.security_id = rl.security_id
LEFT JOIN stock_codes AS sc
    ON sm.security_id = sc.security_id
WHERE sm.is_active = 1
    AND i.is_active = 1
    AND sm.share_class IN ('COMMON', 'ORDINARY', 'ORD', '')
    AND sm.sec_type NOT IN ('ETF', 'ETN', 'FUND')
    AND sm.asset_subtype NOT IN ('SPAC', 'REIT')
    AND ifNull(ls.market_cap, 0) >= 50000000000
    AND ifNull(rl.avg_trading_value_20d, 0) >= 500000000
""".strip()


def _mark_validity(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["is_missing"] = result["raw_factor_value"].isna()
    result["is_valid"] = result["raw_factor_value"].map(lambda value: value is not None and math.isfinite(value))
    result["invalid_reason"] = ""
    result.loc[result["is_missing"], "invalid_reason"] = "MISSING"
    result.loc[~result["is_valid"] & ~result["is_missing"], "invalid_reason"] = "NON_FINITE"
    for factor_id, (lower, upper) in VALUE_LIMITS.items():
        mask = (
            (result["factor_id"] == factor_id)
            & result["raw_factor_value"].notna()
            & ((result["raw_factor_value"] < lower) | (result["raw_factor_value"] > upper))
        )
        result.loc[mask, "invalid_reason"] = "OUT_OF_RANGE"
    return result


def _resolve_industry_fallback(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["industry_group_code"] = result["industry_group_code"].fillna("").astype(str)
    result["sector_code"] = result["sector_code"].fillna("").astype(str)
    valid = result.loc[result["is_valid"]].copy()

    group_counts = valid.groupby(["factor_id", "industry_group_code"]).size().to_dict()
    sector_counts = valid.groupby(["factor_id", "sector_code"]).size().to_dict()
    all_non_financial_counts = valid.loc[~valid["is_financial"]].groupby("factor_id").size().to_dict()
    all_financial_counts = valid.loc[valid["is_financial"]].groupby("factor_id").size().to_dict()

    levels = []
    codes = []
    names = []
    n_peers = []
    for row in result.itertuples(index=False):
        factor_id = row.factor_id
        if bool(row.is_financial):
            levels.append("ALL_FINANCIAL")
            codes.append("ALL_FINANCIAL")
            names.append("All Financial")
            n_peers.append(int(all_financial_counts.get(factor_id, 0)))
            continue

        industry_group_code = str(row.industry_group_code or "")
        sector_code = str(row.sector_code or "")
        industry_group_count = group_counts.get((factor_id, industry_group_code), 0)
        sector_count = sector_counts.get((factor_id, sector_code), 0)
        if industry_group_code and industry_group_count >= MIN_INDUSTRY_GROUP_PEERS:
            levels.append("INDUSTRY_GROUP")
            codes.append(industry_group_code)
            names.append(_string_or_empty(row.industry_group_name) or industry_group_code)
            n_peers.append(int(industry_group_count))
        elif sector_code and sector_count >= MIN_SECTOR_PEERS:
            levels.append("SECTOR")
            codes.append(sector_code)
            names.append(sector_code)
            n_peers.append(int(sector_count))
        else:
            levels.append("ALL_NON_FINANCIAL")
            codes.append("ALL_NON_FINANCIAL")
            names.append("All Non-Financial")
            n_peers.append(int(all_non_financial_counts.get(factor_id, 0)))

    result["industry_level"] = levels
    result["industry_code"] = codes
    result["industry_name"] = names
    result["n_peers"] = n_peers
    result["fallback_level"] = result["industry_level"]
    return result


def _apply_winsorization(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["winsorized_value"] = pd.to_numeric(result["raw_factor_value"], errors="coerce").astype(float)
    result["is_winsorized"] = False
    for factor_id, target_index, peer_index in _iter_assigned_peer_groups(result):
        valid_peer_index = result.loc[peer_index].index[result.loc[peer_index, "is_valid"]]
        valid_target_index = result.loc[target_index].index[result.loc[target_index, "is_valid"]]
        if len(valid_peer_index) == 0 or len(valid_target_index) == 0:
            continue
        definition = STYLE_FACTOR_DEFINITIONS[factor_id]
        peer_values = result.loc[valid_peer_index, "raw_factor_value"].astype(float)
        low = peer_values.quantile(definition.winsor_low)
        high = peer_values.quantile(definition.winsor_high)
        target_values = result.loc[valid_target_index, "raw_factor_value"].astype(float)
        clipped = target_values.clip(lower=low, upper=high)
        result.loc[valid_target_index, "winsorized_value"] = clipped
        result.loc[valid_target_index, "is_winsorized"] = target_values.ne(clipped)
    return result


def _apply_percentile_and_robust_z(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["percentile_score"] = math.nan
    result["robust_z_score"] = math.nan
    result["score_confidence"] = 1.0
    for factor_id, target_index, peer_index in _iter_assigned_peer_groups(result):
        valid_peer_index = result.loc[peer_index].index[result.loc[peer_index, "is_valid"]]
        valid_target_index = result.loc[target_index].index[result.loc[target_index, "is_valid"]]
        n = len(valid_peer_index)
        result.loc[target_index, "n_peers"] = n
        if n < MIN_SCORE_PEERS:
            result.loc[target_index, "score_confidence"] = 0.25
            continue
        direction = factor_direction(factor_id)
        values = result.loc[valid_peer_index, "winsorized_value"].astype(float)
        adjusted = values * direction
        ranks = adjusted.rank(method="average", ascending=True)
        result.loc[valid_target_index, "percentile_score"] = (ranks.loc[valid_target_index] - 1) / (n - 1) * 100.0

        median = values.median()
        mad = (values - median).abs().median()
        if mad == 0 or pd.isna(mad):
            z_scores = pd.Series(0.0, index=valid_peer_index)
        else:
            z_scores = (0.6745 * (values - median) / mad * direction).clip(-3, 3)
        result.loc[valid_target_index, "robust_z_score"] = z_scores.loc[valid_target_index]
    return result


def _iter_assigned_peer_groups(frame: pd.DataFrame):
    for (factor_id, industry_level, industry_code), target_index in frame.groupby(
        ["factor_id", "industry_level", "industry_code"],
        dropna=False,
    ).groups.items():
        factor_mask = frame["factor_id"] == factor_id
        if industry_level == "INDUSTRY_GROUP":
            peer_mask = factor_mask & (frame["industry_group_code"].fillna("").astype(str) == str(industry_code or ""))
        elif industry_level == "SECTOR":
            peer_mask = factor_mask & (frame["sector_code"].fillna("").astype(str) == str(industry_code or ""))
        elif industry_level == "ALL_FINANCIAL":
            peer_mask = factor_mask & frame["is_financial"].fillna(False).astype(bool)
        else:
            peer_mask = factor_mask & ~frame["is_financial"].fillna(False).astype(bool)
        yield factor_id, target_index, frame.index[peer_mask]


def _format_factor_score_frame(frame: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    result = frame.copy()
    result["trade_date"] = trade_date
    result["style_group"] = result["factor_id"].map(lambda factor_id: STYLE_FACTOR_DEFINITIONS[factor_id].style_group)
    result["factor_direction"] = result["factor_id"].map(factor_direction)
    result["score_method"] = "INDUSTRY_PERCENTILE"
    result["updated_at"] = _now_kst_naive()
    for column in ["issuer_id", "stock_code", "country", "market_mic", "company_name", "industry_schema"]:
        if column not in result.columns:
            result[column] = ""
        result[column] = result[column].fillna("").astype(str)
    result["source_trade_date"] = pd.to_datetime(result["source_trade_date"], errors="coerce").dt.date
    result["source_trade_date"] = result["source_trade_date"].fillna(trade_date)
    return result[_factor_score_columns()].reset_index(drop=True)


def _weighted_style_score(
    group: pd.DataFrame,
    factor_weights: dict[str, float],
) -> tuple[float | None, float, set[str], set[str]]:
    total = 0.0
    available_weight = 0.0
    available: set[str] = set()
    invalid: set[str] = set()
    for factor_id, weight in factor_weights.items():
        rows = group.loc[group["factor_id"] == factor_id]
        if rows.empty:
            continue
        row = rows.iloc[0]
        if bool(row.get("is_valid")) and pd.notna(row.get("percentile_score")):
            total += float(row["percentile_score"]) * weight
            available_weight += weight
            available.add(factor_id)
        else:
            invalid.add(factor_id)
    required_weight = sum(factor_weights.values())
    confidence = available_weight / required_weight if required_weight else 0.0
    if available_weight == 0:
        return None, confidence, available, invalid
    return total / available_weight, confidence, available, invalid


def _weighted_total_score(
    score_row: dict[str, Any],
    style_confidences: dict[str, float],
    weights: dict[str, float],
) -> tuple[float | None, float]:
    total = 0.0
    available_weight = 0.0
    confidence_total = 0.0
    for style_group, weight in weights.items():
        column = STYLE_SCORE_COLUMNS[style_group]
        value = score_row.get(column)
        if value is None or pd.isna(value):
            continue
        total += float(value) * weight
        available_weight += weight
        confidence_total += style_confidences.get(style_group, 0.0) * weight
    if available_weight == 0:
        return None, 0.0
    return total / available_weight, confidence_total / available_weight


def _factor_score_columns() -> list[str]:
    return [
        "trade_date",
        "security_id",
        "issuer_id",
        "stock_code",
        "country",
        "market_mic",
        "company_name",
        "industry_schema",
        "industry_level",
        "industry_code",
        "industry_name",
        "factor_id",
        "style_group",
        "factor_direction",
        "raw_factor_value",
        "winsorized_value",
        "percentile_score",
        "robust_z_score",
        "n_peers",
        "score_method",
        "fallback_level",
        "is_valid",
        "invalid_reason",
        "is_winsorized",
        "is_missing",
        "score_confidence",
        "source_trade_date",
        "updated_at",
    ]


def _style_score_columns() -> list[str]:
    return [
        "trade_date",
        "security_id",
        "issuer_id",
        "stock_code",
        "country",
        "market_mic",
        "company_name",
        "industry_schema",
        "industry_code",
        "industry_name",
        "style_profile",
        "value_score",
        "quality_score",
        "growth_score",
        "momentum_score",
        "risk_score",
        "dividend_score",
        "total_score",
        "total_score_sort",
        "available_factor_count",
        "required_factor_count",
        "score_confidence",
        "missing_factor_ids",
        "invalid_factor_ids",
        "updated_at",
    ]


def _snapshot_columns() -> list[str]:
    return [
        "trade_date",
        "industry_schema",
        "industry_level",
        "industry_code",
        "industry_name",
        "factor_id",
        "n_companies",
        "avg_value",
        "median_value",
        "p10_value",
        "p25_value",
        "p75_value",
        "p90_value",
        "winsor_avg_value",
        "updated_at",
    ]


def _empty_factor_score_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_factor_score_columns())


def _empty_style_score_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_style_score_columns())


def _empty_snapshot_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_snapshot_columns())


def _resolve_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _validate_table_name(table_name: str) -> str:
    if not isinstance(table_name, str) or not _IDENTIFIER_RE.match(table_name):
        raise ValueError(f"invalid table name: {table_name!r}")
    return table_name


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def _now_kst_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def _mean_or_none(values: pd.Series) -> float | None:
    if values.empty:
        return None
    return float(values.mean())


def _quantile_or_none(values: pd.Series, q: float) -> float | None:
    if values.empty:
        return None
    return float(values.quantile(q))
