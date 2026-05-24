from __future__ import annotations

from typing import Any, Callable

from api.config.clickhouse import get_clickhouse_client
from api.model.factor import Factor
from api.service.style_score_catalog import STYLE_SCORE_FACTORS


class FactorService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def get_factors(
        self,
        *,
        factor_type: str | None = None,
        factor_group: str | None = None,
        search: str | None = None,
        active_only: bool = True,
    ) -> list[Factor]:
        filters = []
        params: dict[str, Any] = {}
        if active_only:
            filters.append("is_active")
        if factor_type:
            filters.append("factor_type = {factor_type:String}")
            params["factor_type"] = factor_type
        if factor_group:
            filters.append("factor_group = {factor_group:String}")
            params["factor_group"] = factor_group
        if search:
            filters.append(
                "("
                "positionCaseInsensitive(factor_id, {search:String}) > 0 "
                "OR positionCaseInsensitive(factor_name, {search:String}) > 0"
                ")"
            )
            params["search"] = search

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
SELECT
    factor_id,
    factor_name,
    factor_type,
    factor_group,
    unit,
    value_direction,
    description,
    is_active
FROM factor_catalog
{where_clause}
ORDER BY factor_type ASC, factor_group ASC, factor_id ASC
""".strip()

        client = self._client_factory()
        try:
            rows = client.query_df(query, parameters=params).to_dict("records")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        factors = [
            Factor(
                factor_id=str(row["factor_id"]),
                factor_name=str(row["factor_name"]),
                factor_type=str(row["factor_type"]),
                factor_group=str(row["factor_group"]),
                unit=_optional_str(row.get("unit")),
                value_direction=str(row["value_direction"]),
                description=_optional_str(row.get("description")),
                is_active=bool(row["is_active"]),
            )
            for row in rows
        ]
        factors.extend(_style_score_factors())
        return _filter_and_sort_factors(
            factors,
            factor_type=factor_type,
            factor_group=factor_group,
            search=search,
            active_only=active_only,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _style_score_factors() -> list[Factor]:
    return [
        Factor(
            factor_id=definition.factor_id,
            factor_name=definition.factor_name,
            factor_type=definition.factor_type,
            factor_group=definition.factor_group,
            unit=definition.unit,
            value_direction=definition.value_direction,
            description=definition.description,
            is_active=definition.is_active,
        )
        for definition in STYLE_SCORE_FACTORS.values()
    ]


def _filter_and_sort_factors(
    factors: list[Factor],
    *,
    factor_type: str | None,
    factor_group: str | None,
    search: str | None,
    active_only: bool,
) -> list[Factor]:
    search_text = search.lower() if search else None
    result = []
    for factor in factors:
        if active_only and not factor.is_active:
            continue
        if factor_type and factor.factor_type != factor_type:
            continue
        if factor_group and factor.factor_group != factor_group:
            continue
        if search_text and (
            search_text not in factor.factor_id.lower()
            and search_text not in factor.factor_name.lower()
        ):
            continue
        result.append(factor)
    return sorted(result, key=lambda item: (item.factor_type, item.factor_group, item.factor_id))
