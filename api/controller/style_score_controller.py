from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.model.style_score import (
    FactorScoreBreakdown,
    StyleScoreComponent,
    StyleScoreComponentFactor,
    StyleScoreRow,
)
from api.service.dto import (
    FactorScoreBreakdownDto,
    StyleProfile,
    StyleScoreComponentDetailResponseDto,
    StyleScoreComponentDto,
    StyleScoreComponentFactorDto,
    StyleScoreComponentsResponseDto,
    StyleScoreDetailResponseDto,
    StyleScoreResponseDto,
    StyleScoreRowDto,
)
from api.service.style_score_service import StyleScoreService


router = APIRouter(prefix="/api/style-scores", tags=["style-scores"])


@router.get("", response_model=StyleScoreResponseDto)
def get_style_scores(
    trade_date: date | None = Query(default=None),
    style_profile: StyleProfile = Query(default="DEFAULT"),
    limit: int = Query(default=100, gt=0, le=1000),
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    industry_group_code: str | None = Query(default=None),
    sector_code: str | None = Query(default=None),
) -> StyleScoreResponseDto:
    try:
        result = StyleScoreService().get_style_scores(
            trade_date=trade_date,
            style_profile=style_profile,
            limit=limit,
            min_confidence=min_confidence,
            industry_group_code=industry_group_code,
            sector_code=sector_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StyleScoreResponseDto(
        trade_date=result.trade_date,
        style_profile=result.style_profile,
        total_count=result.total_count,
        rows=[_to_style_row_dto(row) for row in result.rows],
    )


@router.get("/{security_id}", response_model=StyleScoreDetailResponseDto)
def get_style_score_detail(
    security_id: str,
    trade_date: date | None = Query(default=None),
    style_profile: StyleProfile = Query(default="DEFAULT"),
) -> StyleScoreDetailResponseDto:
    try:
        result = StyleScoreService().get_style_score_detail(
            security_id,
            trade_date=trade_date,
            style_profile=style_profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StyleScoreDetailResponseDto(
        row=_to_style_row_dto(result.row) if result.row is not None else None,
        factors=[_to_factor_dto(factor) for factor in result.factors],
    )


@router.get("/{security_id}/components", response_model=StyleScoreComponentsResponseDto)
def get_style_score_components(
    security_id: str,
    trade_date: date | None = Query(default=None),
    style_profile: StyleProfile = Query(default="DEFAULT"),
) -> StyleScoreComponentsResponseDto:
    try:
        result = StyleScoreService().get_style_score_components(
            security_id,
            trade_date=trade_date,
            style_profile=style_profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StyleScoreComponentsResponseDto(
        trade_date=result.trade_date,
        security_id=result.security_id,
        stock_code=result.stock_code,
        company_name=result.company_name,
        style_profile=result.style_profile,
        components=[_to_component_dto(component) for component in result.components],
    )


@router.get(
    "/{security_id}/components/{component_key}",
    response_model=StyleScoreComponentDetailResponseDto,
)
def get_style_score_component_detail(
    security_id: str,
    component_key: str,
    trade_date: date | None = Query(default=None),
    style_profile: StyleProfile = Query(default="DEFAULT"),
) -> StyleScoreComponentDetailResponseDto:
    try:
        result = StyleScoreService().get_style_score_component_detail(
            security_id,
            component_key,
            trade_date=trade_date,
            style_profile=style_profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StyleScoreComponentDetailResponseDto(
        trade_date=result.trade_date,
        security_id=result.security_id,
        stock_code=result.stock_code,
        company_name=result.company_name,
        style_profile=result.style_profile,
        component=_to_component_dto(result.component),
        factors=[_to_component_factor_dto(factor) for factor in result.factors],
    )


def _to_style_row_dto(row: StyleScoreRow) -> StyleScoreRowDto:
    return StyleScoreRowDto(**row.__dict__)


def _to_factor_dto(row: FactorScoreBreakdown) -> FactorScoreBreakdownDto:
    return FactorScoreBreakdownDto(**row.__dict__)


def _to_component_dto(row: StyleScoreComponent) -> StyleScoreComponentDto:
    return StyleScoreComponentDto(**row.__dict__)


def _to_component_factor_dto(row: StyleScoreComponentFactor) -> StyleScoreComponentFactorDto:
    return StyleScoreComponentFactorDto(**row.__dict__)
