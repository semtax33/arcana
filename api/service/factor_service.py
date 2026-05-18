from __future__ import annotations

from typing import Any, Callable

from api.config.clickhouse import get_clickhouse_client
from api.model.factor import Factor


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

        return [
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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
