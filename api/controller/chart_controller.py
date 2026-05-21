from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.service.chart_service import ChartService, StockChartNotFoundError
from api.service.dto import (
    ChartRange,
    RecentStockChartRowDto,
    StockChartMetadataDto,
    StockChartPointDto,
    StockChartResponseDto,
)


router = APIRouter(prefix="/api/chart", tags=["chart"])


@router.get("/{stock_code}", response_model=StockChartResponseDto)
def get_stock_chart(
    stock_code: str,
    range: ChartRange = Query(default="1Y"),
) -> StockChartResponseDto:
    try:
        result = ChartService().get_chart(stock_code, range)
    except StockChartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StockChartResponseDto(
        stock=StockChartMetadataDto(**result.stock.__dict__),
        range=result.range,
        from_date=result.from_date,
        to_date=result.to_date,
        chart=[StockChartPointDto(**point.__dict__) for point in result.chart],
        recent=[RecentStockChartRowDto(**row.__dict__) for row in result.recent],
        factor_source=result.factor_source,
        factor_ids=result.factor_ids,
    )
