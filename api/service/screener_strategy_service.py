from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from api.repository.screener_strategy_repository import ScreenerStrategyRepository
from api.service.dto import (
    FactorScreenRequestDto,
    ScreenerStrategyDetailDto,
    ScreenerStrategySummaryDto,
)
from engine.core.paths import DATA_LAKE


DEFAULT_SCREENER_STRATEGY_DB = DATA_LAKE.meta("screener_strategies.sqlite3")


class ScreenerStrategyService:
    def __init__(self, repository: ScreenerStrategyRepository | None = None) -> None:
        self._repository = repository or ScreenerStrategyRepository(_resolve_db_path())

    def list_strategies(self) -> list[ScreenerStrategySummaryDto]:
        return [ScreenerStrategySummaryDto(**row) for row in self._repository.list()]

    def get_strategy(self, strategy_id: int) -> ScreenerStrategyDetailDto:
        row = self._repository.get(strategy_id)
        if row is None:
            raise KeyError(f"screener strategy not found: {strategy_id}")
        return _to_detail(row)

    def save_strategy(
        self,
        name: str,
        strategy: FactorScreenRequestDto,
    ) -> ScreenerStrategyDetailDto:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("strategy name is required")

        row = self._repository.save(normalized_name, _model_dump_json(strategy))
        return _to_detail(row)

    def delete_strategy(self, strategy_id: int) -> bool:
        return self._repository.delete(strategy_id)


def _resolve_db_path() -> Path:
    configured_path = os.getenv("ARCANA_SCREENER_STRATEGY_DB")
    if configured_path:
        return Path(configured_path)
    return DEFAULT_SCREENER_STRATEGY_DB


def _to_detail(row: dict[str, Any]) -> ScreenerStrategyDetailDto:
    return ScreenerStrategyDetailDto(
        id=int(row["id"]),
        name=str(row["name"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        strategy=FactorScreenRequestDto(**row["strategy"]),
    )


def _model_dump_json(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "json"):
        return json.loads(model.json())
    raise TypeError("model must be a pydantic model")