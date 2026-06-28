from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException

from api.service.dto import (
    EstimateComponentsResponseDto,
    EstimateComponentRowDto,
    EstimateConsensusResponseDto,
    EstimateConsensusRowDto,
    OperatingMetricDriversResponseDto,
    OperatingMetricDriverRowDto,
    OperatingMetricStockDto,
)
from api.service.estimate_service import EstimateService, EstimatesNotFoundError
from api.service.operating_metrics_service import OperatingMetricsNotFoundError, OperatingMetricsService


router = APIRouter(prefix="/api/estimates", tags=["estimates"])


@router.get("/{stock_code}", response_model=EstimateComponentsResponseDto)
def get_estimates(stock_code: str) -> EstimateComponentsResponseDto:
    try:
        result = EstimateService().get_components(stock_code)
    except EstimatesNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return EstimateComponentsResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        target_period=result.target_period,
        rows=[EstimateComponentRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )


@router.get("/{stock_code}/consensus", response_model=EstimateConsensusResponseDto)
def get_estimate_consensus(stock_code: str) -> EstimateConsensusResponseDto:
    try:
        result = EstimateService().get_consensus(stock_code)
    except EstimatesNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return EstimateConsensusResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        target_period=result.target_period,
        rows=[EstimateConsensusRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )


@router.get("/{stock_code}/consensus/history", response_model=EstimateConsensusResponseDto)
def get_estimate_consensus_history(
    stock_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
    metric_id: str | None = None,
    target_period: str | None = None,
) -> EstimateConsensusResponseDto:
    try:
        result = EstimateService().get_consensus_history(
            stock_code,
            start_date=start_date,
            end_date=end_date,
            metric_id=metric_id,
            target_period=target_period,
        )
    except EstimatesNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return EstimateConsensusResponseDto(
        stock=OperatingMetricStockDto(**result.stock.__dict__),
        as_of_date=result.as_of_date,
        target_period=result.target_period,
        rows=[EstimateConsensusRowDto(**row.__dict__) for row in result.rows],
        source=result.source,
        warnings=result.warnings,
    )


@router.get("/{stock_code}/drivers", response_model=OperatingMetricDriversResponseDto)
def get_estimate_drivers(stock_code: str) -> OperatingMetricDriversResponseDto:
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
