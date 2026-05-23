from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.dto import IndustryGroupDto, SectorDto
from api.service.sector_service import SectorService


router = APIRouter(prefix="/api/sectors", tags=["sectors"])


@router.get("", response_model=list[SectorDto])
def get_sectors() -> list[SectorDto]:
    try:
        sectors = SectorService().get_sectors()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [SectorDto(**sector.__dict__) for sector in sectors]


@router.get("/industry-groups", response_model=list[IndustryGroupDto])
def get_industry_groups() -> list[IndustryGroupDto]:
    try:
        industry_groups = SectorService().get_industry_groups()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [IndustryGroupDto(**industry_group.__dict__) for industry_group in industry_groups]
