from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from api.config.clickhouse import get_clickhouse_client
from api.model.sector import IndustryGroup, Sector


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GICS_RULES_PATH = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "gics_rules.yaml"


class SectorService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        gics_rules_path: Path = DEFAULT_GICS_RULES_PATH,
    ) -> None:
        self._client_factory = client_factory
        self._gics_rules_path = gics_rules_path

    def get_sectors(self) -> list[Sector]:
        sector_names = self._load_sector_names()
        stock_counts = self._load_stock_counts("sector_code")

        return [
            Sector(
                sector_code=sector_code,
                sector_name=sector_name,
                stock_count=stock_counts.get(sector_code, 0),
            )
            for sector_code, sector_name in sector_names.items()
        ]

    def get_industry_groups(self) -> list[IndustryGroup]:
        sector_names = self._load_sector_names()
        industry_group_names = self._load_industry_group_names()
        stock_counts = self._load_stock_counts("industry_group_code")

        return [
            IndustryGroup(
                industry_group_code=industry_group_code,
                industry_group_name=industry_group_name,
                sector_code=industry_group_code[:2],
                sector_name=sector_names.get(industry_group_code[:2], industry_group_code[:2]),
                stock_count=stock_counts.get(industry_group_code, 0),
            )
            for industry_group_code, industry_group_name in industry_group_names.items()
        ]

    def _load_sector_names(self) -> dict[str, str]:
        with self._gics_rules_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        sectors = config.get("sectors", {})
        return {str(code): str(name) for code, name in sectors.items()}

    def _load_industry_group_names(self) -> dict[str, str]:
        with self._gics_rules_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        industry_groups = config.get("industry_groups", {})
        return {str(code): str(name) for code, name in industry_groups.items()}

    def _load_stock_counts(self, column_name: str) -> dict[str, int]:
        if column_name not in {"sector_code", "industry_group_code"}:
            raise ValueError(f"unsupported stock count column: {column_name}")
        query = f"""
SELECT
    {column_name} AS classification_code,
    count() AS stock_count
FROM issuers
WHERE is_active
    AND industry_schema = 'GICS'
    AND {column_name} != ''
    AND {column_name} != 'UNMAPPED'
GROUP BY {column_name}
""".strip()
        client = self._client_factory()
        try:
            rows = client.query_df(query).to_dict("records")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return {
            str(row["classification_code"]): int(row["stock_count"])
            for row in rows
            if row.get("classification_code") is not None
        }
