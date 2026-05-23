from __future__ import annotations

import math
import re
from typing import Any, Callable

from api.config.clickhouse import get_clickhouse_client
from api.model.screening import (
    FactorScreenColumn,
    FactorScreenResult,
    FactorScreenValue,
    ScreenedStockRow,
)
from api.repository.factor_screen_query import (
    DEFAULT_FINANCIAL_BASIS,
    FactorCondition,
    screen_stocks_by_factors,
)
from api.service.dto import FactorConditionDto, FactorScreenRequestDto


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
            result_df = screen_stocks_by_factors(
                client,
                conditions,
                as_of_date=request.as_of_date,
                financial_basis=request.financial_basis or DEFAULT_FINANCIAL_BASIS,
                sector_codes=request.sector_codes,
                industry_group_codes=request.industry_group_codes,
                match_mode=request.match_mode,
                limit=None,
                include_security_metadata=True,
            )
            all_rows = result_df.to_dict("records")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        factor_columns = _build_factor_columns(conditions, factor_meta)
        rows = all_rows[: request.limit] if request.limit is not None else all_rows
        screened_rows = [
            _to_screened_row(index, row, conditions, factor_columns, factor_meta)
            for index, row in enumerate(rows, start=1)
        ]

        return FactorScreenResult(
            total_count=len(all_rows),
            fixed_columns=FIXED_COLUMNS,
            factor_columns=factor_columns,
            rows=screened_rows,
        )


def _to_repository_condition(condition: FactorConditionDto) -> FactorCondition:
    data = _model_dump(condition)
    return FactorCondition(**data)


def _load_factor_metadata(
    client: Any,
    conditions: list[FactorCondition],
) -> dict[str, dict[str, Any]]:
    factor_ids = sorted({condition.factor_id for condition in conditions})
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
    return {str(row["factor_id"]): row for row in rows}


def _build_factor_columns(
    conditions: list[FactorCondition],
    factor_meta: dict[str, dict[str, Any]],
) -> list[FactorScreenColumn]:
    columns = []
    for index, condition in enumerate(conditions):
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
) -> ScreenedStockRow:
    factor_values = {}
    for index, condition in enumerate(conditions):
        column = factor_columns[index]
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
