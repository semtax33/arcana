from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.dto import (
    BusinessAreaBadgeDto,
    CompanyIntroductionDto,
    StockIntroductionMetadataDto,
    StockIntroductionMetricsDto,
    StockIntroductionResponseDto,
)
from api.service.introduction_service import (
    IntroductionService,
    StockIntroductionNotFoundError,
)


router = APIRouter(prefix="/api/introduction", tags=["introduction"])


@router.get("/{stock_code}", response_model=StockIntroductionResponseDto)
def get_stock_introduction(stock_code: str) -> StockIntroductionResponseDto:
    try:
        result = IntroductionService().get_introduction(stock_code)
    except StockIntroductionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StockIntroductionResponseDto(
        stock=StockIntroductionMetadataDto(**result.stock.__dict__),
        metrics=StockIntroductionMetricsDto(**result.metrics.__dict__),
        company=CompanyIntroductionDto(**result.company.__dict__),
        business_areas=[BusinessAreaBadgeDto(**area.__dict__) for area in result.business_areas],
        factor_source=result.factor_source,
    )
