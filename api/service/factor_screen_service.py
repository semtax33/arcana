from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.screening import (
    FactorScreenColumn,
    FactorScreenResult,
    FactorScreenValue,
    ScreenedStockRow,
)
from api.repository.factor_screen_query import (
    DEFAULT_FINANCIAL_BASIS,
    DEFAULT_FACTOR_SNAPSHOT_TABLE,
    DEFAULT_FACTOR_TABLE,
    FactorCondition,
    screen_stocks_by_factors,
)
from api.service.dto import FactorConditionDto, FactorScreenRequestDto
from api.service.factor_identity import canonical_factor_id
from api.service.style_score_catalog import (
    DEFAULT_FACTOR_SCREEN_STYLE_PROFILE,
    DEFAULT_SCREEN_STYLE_PROFILE,
    is_style_score_factor,
    style_score_factor_metadata,
)


FIXED_COLUMNS = [
    FactorScreenColumn(key="rank", label="#", column_type="rank", order=1),
    FactorScreenColumn(key="ticker", label="티커", column_type="ticker", order=2),
    FactorScreenColumn(key="stock_name", label="종목명", column_type="name", order=3),
    FactorScreenColumn(key="country", label="국가", column_type="country", order=4),
    FactorScreenColumn(key="market_cap", label="시가총액", column_type="market_cap", order=5),
    FactorScreenColumn(key="percentile", label="퍼센타일", column_type="percentile", order=10_000),
]


class FactorScreenService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def screen_stocks(self, request: FactorScreenRequestDto) -> FactorScreenResult:
        conditions = [_to_repository_condition(condition) for condition in request.conditions]

        client = self._client_factory()
        try:
            factor_meta = _load_factor_metadata(client, conditions)
            (
                factor_table,
                factor_table_is_snapshot,
                effective_snapshot_date,
            ) = _resolve_factor_table(
                client,
                conditions=conditions,
                financial_basis=request.financial_basis or DEFAULT_FINANCIAL_BASIS,
                as_of_date=request.as_of_date,
                market=request.market,
            )
            result_df = screen_stocks_by_factors(
                client,
                conditions,
                as_of_date=request.as_of_date,
                market=request.market,
                financial_basis=request.financial_basis or DEFAULT_FINANCIAL_BASIS,
                style_profile=_resolve_style_profile(request.style_profile, conditions),
                sector_codes=request.sector_codes,
                industry_group_codes=request.industry_group_codes,
                match_mode=request.match_mode,
                limit=request.limit,
                factor_table=factor_table,
                factor_table_is_snapshot=factor_table_is_snapshot,
                effective_snapshot_date=effective_snapshot_date,
                raw_lookback_days=None if factor_table_is_snapshot else _raw_lookback_days(),
                include_security_metadata=True,
            )
            all_rows = result_df.to_dict("records")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        display_condition_indexes = _unique_condition_indexes_by_factor_id(conditions)
        factor_columns = _build_factor_columns(
            conditions,
            factor_meta,
            condition_indexes=display_condition_indexes,
        )
        total_count = _extract_total_count(all_rows)
        rows = all_rows
        screened_rows = [
            _to_screened_row(
                index,
                row,
                conditions,
                factor_columns,
                factor_meta,
                condition_indexes=display_condition_indexes,
            )
            for index, row in enumerate(rows, start=1)
        ]

        return FactorScreenResult(
            total_count=total_count,
            fixed_columns=FIXED_COLUMNS,
            factor_columns=factor_columns,
            rows=screened_rows,
        )


def _to_repository_condition(condition: FactorConditionDto) -> FactorCondition:
    data = _model_dump(condition)
    data["factor_id"] = canonical_factor_id(str(data["factor_id"]))
    return FactorCondition(**data)


def _load_factor_metadata(
    client: Any,
    conditions: list[FactorCondition],
) -> dict[str, dict[str, Any]]:
    factor_ids = sorted(
        {
            condition.factor_id
            for condition in conditions
            if not is_style_score_factor(condition.factor_id)
        }
    )
    metadata = {
        condition.factor_id: style_score_factor_metadata(condition.factor_id)
        for condition in conditions
        if is_style_score_factor(condition.factor_id)
    }
    if not factor_ids:
        return metadata
    query = """
SELECT
    factor_id,
    factor_name,
    unit,
    value_direction
FROM factor_catalog
WHERE has({factor_ids:Array(String)}, factor_id)
""".strip()
    rows = client.query_df(query, parameters={"factor_ids": factor_ids}).to_dict("records")
    metadata.update({str(row["factor_id"]): row for row in rows})
    return metadata


def _resolve_factor_table(
    client: Any,
    *,
    conditions: list[FactorCondition],
    financial_basis: str,
    as_of_date: Any,
    market: str | None,
) -> tuple[str, bool, str | None]:
    factor_ids = sorted(
        {
            condition.factor_id
            for condition in conditions
            if not is_style_score_factor(condition.factor_id)
        }
    )
    if factor_ids:
        snapshot_date = _resolve_snapshot_date(
            client,
            DEFAULT_FACTOR_SNAPSHOT_TABLE,
            factor_ids=factor_ids,
            financial_basis=financial_basis,
            as_of_date=as_of_date,
            market=market,
        )
        if snapshot_date is not None:
            return DEFAULT_FACTOR_SNAPSHOT_TABLE, True, snapshot_date
    return DEFAULT_FACTOR_TABLE, False, None


def _resolve_snapshot_date(
    client: Any,
    table_name: str,
    *,
    factor_ids: list[str],
    financial_basis: str,
    as_of_date: Any,
    market: str | None,
) -> str | None:
    query = getattr(client, "query", None)
    if not callable(query):
        return None
    if not _table_exists(client, table_name):
        return None
    requested_date = date.fromisoformat(_screen_as_of_date(as_of_date))
    candidate_dates = [
        (requested_date - timedelta(days=offset)).isoformat()
        for offset in range(_snapshot_candidate_lookback_days() + 1)
    ]
    params: dict[str, Any] = {
        "factor_ids": factor_ids,
        "factor_count": len(factor_ids),
        "financial_basis": financial_basis,
        "as_of_date": requested_date.isoformat(),
        "candidate_dates": candidate_dates,
    }
    market_filter = ""
    normalized_market = str(market or "").strip().upper()
    if normalized_market and normalized_market != "ALL":
        params["market_security_prefix"] = f"SEC_{normalized_market}_"
        market_filter = "\n    AND startsWith(security_id, {market_security_prefix:String})"
    try:
        rows = query(
            f"""
WITH
latest_raw_date AS (
    SELECT nullIf(max(trade_date), toDate(0)) AS latest_date
    FROM {DEFAULT_FACTOR_TABLE}
    PREWHERE trade_date IN {{candidate_dates:Array(Date)}}
    WHERE factor_id IN {{factor_ids:Array(String)}}
        AND financial_basis = {{financial_basis:String}}{market_filter}
),
eligible_snapshots AS (
    SELECT
        trade_date,
        countDistinct(factor_id) AS factor_count
    FROM {table_name}
    PREWHERE trade_date IN {{candidate_dates:Array(Date)}}
    WHERE factor_id IN {{factor_ids:Array(String)}}
        AND financial_basis = {{financial_basis:String}}{market_filter}
        AND source_trade_date <= {{as_of_date:Date}}
    GROUP BY trade_date
    HAVING factor_count >= {{factor_count:UInt64}}
)
SELECT
    nullIf(max(trade_date), toDate(0)) AS resolved_snapshot_date,
    (SELECT latest_date FROM latest_raw_date) AS raw_latest_date
FROM eligible_snapshots
WHERE trade_date >= coalesce((SELECT latest_date FROM latest_raw_date), toDate(0))
""".strip(),
            parameters=params,
        ).result_rows
    except Exception:
        return None
    if not rows or rows[0][0] is None:
        return None
    return _screen_as_of_date(rows[0][0])


def _screen_as_of_date(value: Any) -> str:
    if value is None:
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _snapshot_candidate_lookback_days() -> int:
    value = os.getenv("ARCANA_FACTOR_SNAPSHOT_CANDIDATE_DAYS", "14").strip()
    try:
        days = int(value)
    except ValueError:
        return 14
    return max(1, min(days, 366))


def _table_exists(client: Any, table_name: str) -> bool:
    query = getattr(client, "query", None)
    if not callable(query):
        return False
    try:
        rows = query(f"EXISTS TABLE {table_name}").result_rows
    except Exception:
        return False
    return bool(rows and rows[0][0])


def _extract_total_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    raw_value = rows[0].get("total_count")
    if raw_value is None:
        return len(rows)
    if hasattr(raw_value, "item"):
        raw_value = raw_value.item()
    return int(raw_value)


def _raw_lookback_days() -> int | None:
    value = os.getenv("ARCANA_FACTOR_RAW_LOOKBACK_DAYS", "540").strip()
    if not value:
        return None
    try:
        days = int(value)
    except ValueError:
        return 540
    return days if days > 0 else None


def _resolve_style_profile(
    requested_profile: str | None,
    conditions: list[FactorCondition],
) -> str | None:
    if not any(is_style_score_factor(condition.factor_id) for condition in conditions):
        return requested_profile

    profile = str(requested_profile or "").strip().upper()
    if not profile or profile == DEFAULT_SCREEN_STYLE_PROFILE:
        return DEFAULT_FACTOR_SCREEN_STYLE_PROFILE
    return profile


def _build_factor_columns(
    conditions: list[FactorCondition],
    factor_meta: dict[str, dict[str, Any]],
    *,
    condition_indexes: list[int] | None = None,
) -> list[FactorScreenColumn]:
    columns = []
    indexes = condition_indexes if condition_indexes is not None else list(range(len(conditions)))
    for index in indexes:
        condition = conditions[index]
        meta = factor_meta.get(condition.factor_id, {})
        factor_name = _factor_name(condition, meta)
        columns.append(
            FactorScreenColumn(
                key=_condition_alias(condition, index),
                label=factor_name,
                column_type="factor",
                order=100 + index,
                factor_id=condition.factor_id,
                factor_name=factor_name,
                unit=_clean_value(meta.get("unit")),
                value_direction=_clean_value(meta.get("value_direction")),
            )
        )
    return columns


def _to_screened_row(
    rank: int,
    row: dict[str, Any],
    conditions: list[FactorCondition],
    factor_columns: list[FactorScreenColumn],
    factor_meta: dict[str, dict[str, Any]],
    *,
    condition_indexes: list[int] | None = None,
) -> ScreenedStockRow:
    factor_values = {}
    indexes = condition_indexes if condition_indexes is not None else list(range(len(conditions)))
    for column, index in zip(factor_columns, indexes):
        condition = conditions[index]
        meta = factor_meta.get(condition.factor_id, {})
        factor_name = _factor_name(condition, meta)
        factor_values[column.key] = FactorScreenValue(
            factor_id=condition.factor_id,
            factor_name=factor_name,
            condition_id=_condition_id(condition, index),
            value=_clean_value(row.get(f"{column.key}_value")),
            trade_date=_clean_value(row.get(f"{column.key}_trade_date")),
            unit=_clean_value(meta.get("unit")),
            value_direction=_clean_value(meta.get("value_direction")),
        )

    return ScreenedStockRow(
        rank=rank,
        security_id=str(row["security_id"]),
        ticker=_clean_value(row.get("ticker")),
        stock_name=_clean_value(row.get("issuer_name")),
        country=_clean_value(row.get("country")) or "KR",
        market_cap=_clean_value(row.get("market_cap")),
        sector_code=_clean_value(row.get("sector_code")),
        industry_group_code=_clean_value(row.get("industry_group_code")),
        industry_group_name=_clean_value(row.get("industry_group_name")),
        percentile=None,
        matched_condition_count=int(row.get("matched_condition_count", 0)),
        matched_conditions=_as_string_list(row.get("matched_conditions")),
        latest_trade_date=_clean_value(row.get("latest_trade_date")),
        factor_values=factor_values,
        raw_values=row,
    )


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _unique_condition_indexes_by_factor_id(conditions: list[FactorCondition]) -> list[int]:
    seen = set()
    indexes = []
    for index, condition in enumerate(conditions):
        key = condition.factor_id.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        indexes.append(index)
    return indexes


def _factor_name(condition: FactorCondition, meta: dict[str, Any]) -> str:
    return str(condition.alias or meta.get("factor_name") or condition.factor_id)


def _condition_id(condition: FactorCondition, index: int) -> str:
    alias = condition.alias or condition.factor_id
    return f"{index}:{condition.mode}:{alias}"


def _condition_alias(condition: FactorCondition, index: int) -> str:
    raw_alias = condition.alias or condition.factor_id
    alias = re.sub(r"[^A-Za-z0-9_]+", "_", raw_alias).strip("_").lower()
    if not alias or alias[0].isdigit():
        alias = f"factor_{alias}"
    return f"{alias}_{index}"


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return [str(value)]


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        try:
            return _clean_value(value.item())
        except ValueError:
            return value
    return value
