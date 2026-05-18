from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from api.config.clickhouse import get_clickhouse_client
from api.model.sector import Sector


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
        stock_counts = self._load_stock_counts()

        return [
            Sector(
                sector_code=sector_code,
                sector_name=sector_name,
                stock_count=stock_counts.get(sector_code, 0),
            )
            for sector_code, sector_name in sector_names.items()
        ]

    def _load_sector_names(self) -> dict[str, str]:
        with self._gics_rules_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        sectors = config.get("sectors", {})
        return {str(code): str(name) for code, name in sectors.items()}

    def _load_stock_counts(self) -> dict[str, int]:
        query = """
SELECT
    industry_code AS sector_code,
    count() AS stock_count
FROM issuers
WHERE is_active
    AND industry_schema = 'GICS'
GROUP BY industry_code
""".strip()
        client = self._client_factory()
        try:
            rows = client.query_df(query).to_dict("records")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return {
            str(row["sector_code"]): int(row["stock_count"])
            for row in rows
            if row.get("sector_code") is not None
        }

