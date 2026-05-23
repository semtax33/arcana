from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.model.sector_leader import SectorLeaderMetric, SectorLeaderRow
from api.service.dto import (
    SectorLeaderMetricDto,
    SectorLeaderResponseDto,
    SectorLeaderRowDto,
    SectorLeaderLevel,
    SectorLeaderSortBy,
    SortDirection,
)
from api.service.sector_leader_service import SectorLeaderService


router = APIRouter(prefix="/api/sector-leaders", tags=["sector-leaders"])


@router.get("", response_model=SectorLeaderResponseDto)
def get_sector_leaders(
    as_of_date: date | None = Query(default=None),
    sort_by: SectorLeaderSortBy = Query(default="strong_stock_ratio"),
    direction: SortDirection | None = Query(default=None),
    limit: int | None = Query(default=None, gt=0),
    near_high_pct: float = Query(default=3.0, ge=0, lt=100),
    financial_basis: str = Query(default="annual"),
    level: SectorLeaderLevel = Query(default="industry_group"),
) -> SectorLeaderResponseDto:
    try:
        result = SectorLeaderService().get_sector_leaders(
            as_of_date=as_of_date,
            sort_by=sort_by,
            direction=direction,
            limit=limit,
            near_high_pct=near_high_pct,
            financial_basis=financial_basis,
            level=level,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SectorLeaderResponseDto(
        as_of_date=result.as_of_date,
        level=result.level,
        sort_by=result.sort_by,
        direction=result.direction,
        near_high_pct=result.near_high_pct,
        financial_basis=result.financial_basis,
        factor_source=result.factor_source,
        eps_growth_factor_id=result.eps_growth_factor_id,
        rows=[_to_row_dto(row) for row in result.rows],
    )


def _to_row_dto(row: SectorLeaderRow) -> SectorLeaderRowDto:
    return SectorLeaderRowDto(
        rank=row.rank,
        sector_code=row.sector_code,
        sector_name=row.sector_name,
        stock_count=row.stock_count,
        strong_stock_count=row.strong_stock_count,
        strong_stock_ratio=_to_metric_dto(row.strong_stock_ratio),
        eps_expected_growth=_to_metric_dto(row.eps_expected_growth),
        return_1d=_to_metric_dto(row.return_1d),
        return_1w=_to_metric_dto(row.return_1w),
        roe=_to_metric_dto(row.roe),
        per=_to_metric_dto(row.per),
        pbr=_to_metric_dto(row.pbr),
    )


def _to_metric_dto(metric: SectorLeaderMetric) -> SectorLeaderMetricDto:
    return SectorLeaderMetricDto(
        value=metric.value,
        display_value=metric.display_value,
    )
