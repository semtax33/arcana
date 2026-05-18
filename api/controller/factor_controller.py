from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.service.dto import FactorDto
from api.service.factor_service import FactorService


router = APIRouter(prefix="/api/factors", tags=["factors"])


@router.get("", response_model=list[FactorDto])
def get_factors(
    factor_type: str | None = Query(default=None),
    factor_group: str | None = Query(default=None),
    search: str | None = Query(default=None),
    active_only: bool = Query(default=True),
) -> list[FactorDto]:
    try:
        factors = FactorService().get_factors(
            factor_type=factor_type,
            factor_group=factor_group,
            search=search,
            active_only=active_only,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [FactorDto(**factor.__dict__) for factor in factors]

