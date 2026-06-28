from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.dto import (
    OperatingMetricDriversResponseDto,
    OperatingMetricDriverRowDto,
    OperatingMetricsResponseDto,
    OperatingMetricRowDto,
    OperatingMetricStockDto,
    UnitEconomicsResponseDto,
    UnitEconomicsRowDto,
)
from api.service.operating_metrics_service import OperatingMetricsNotFoundError, OperatingMetricsService


router = APIRouter(prefix="/api/operating-metrics", tags=["operating-metrics"])


@router.get("/{stock_code}", response_model=OperatingMetricsResponseDto)
def get_operating_metrics(stock_code: str) -> OperatingMetricsResponseDto:
    try:
        result = OperatingMetricsService().get_metrics(stock_code)
    except OperatingMetricsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return OperatingMetricsResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        rows=[OperatingMetricRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )


@router.get("/{stock_code}/unit-economics", response_model=UnitEconomicsResponseDto)
def get_unit_economics(stock_code: str) -> UnitEconomicsResponseDto:
    try:
        result = OperatingMetricsService().get_unit_economics(stock_code)
    except OperatingMetricsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return UnitEconomicsResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        rows=[UnitEconomicsRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )


@router.get("/{stock_code}/drivers", response_model=OperatingMetricDriversResponseDto)
def get_operating_metric_drivers(stock_code: str) -> OperatingMetricDriversResponseDto:
    try:
        result = OperatingMetricsService().get_drivers(stock_code)
    except OperatingMetricsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return OperatingMetricDriversResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        rows=[OperatingMetricDriverRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )
