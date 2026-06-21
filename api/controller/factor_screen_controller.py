from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.dto import (
    FactorScreenColumnDto,
    FactorScreenRequestDto,
    FactorScreenResponseDto,
    FactorScreenSummaryDto,
    FactorScreenValueDto,
    ScreenerStrategyDeleteResponseDto,
    ScreenerStrategyDetailDto,
    ScreenerStrategyListResponseDto,
    ScreenerStrategySaveRequestDto,
    ScreenedStockRowDto,
)
from api.service.factor_screen_service import FactorScreenService
from api.service.screener_strategy_service import ScreenerStrategyService

router = APIRouter(prefix="/api/factor-screen", tags=["factor-screen"])


@router.get("/strategies", response_model=ScreenerStrategyListResponseDto)
def list_screener_strategies() -> ScreenerStrategyListResponseDto:
    strategies = ScreenerStrategyService().list_strategies()
    return ScreenerStrategyListResponseDto(strategies=strategies)


@router.post("/strategies", response_model=ScreenerStrategyDetailDto)
def save_screener_strategy(
    request: ScreenerStrategySaveRequestDto,
) -> ScreenerStrategyDetailDto:
    try:
        return ScreenerStrategyService().save_strategy(request.name, request.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/strategies/{strategy_id}", response_model=ScreenerStrategyDetailDto)
def get_screener_strategy(strategy_id: int) -> ScreenerStrategyDetailDto:
    try:
        return ScreenerStrategyService().get_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="screener strategy not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/strategies/{strategy_id}", response_model=ScreenerStrategyDeleteResponseDto)
def delete_screener_strategy(strategy_id: int) -> ScreenerStrategyDeleteResponseDto:
    try:
        deleted = ScreenerStrategyService().delete_strategy(strategy_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="screener strategy not found")
    return ScreenerStrategyDeleteResponseDto(deleted=True)


@router.post("/screen", response_model=FactorScreenResponseDto)
def screen_stocks(request: FactorScreenRequestDto) -> FactorScreenResponseDto:
    try:
        result = FactorScreenService().screen_stocks(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = [
        ScreenedStockRowDto(
            rank=row.rank,
            security_id=row.security_id,
            ticker=row.ticker,
            stock_name=row.stock_name,
            country=row.country,
            market_cap=row.market_cap,
            sector_code=row.sector_code,
            industry_group_code=row.industry_group_code,
            industry_group_name=row.industry_group_name,
            percentile=row.percentile,
            matched_condition_count=row.matched_condition_count,
            matched_conditions=row.matched_conditions,
            latest_trade_date=row.latest_trade_date,
            factor_values={
                key: FactorScreenValueDto(
                    factor_id=value.factor_id,
                    factor_name=value.factor_name,
                    condition_id=value.condition_id,
                    value=value.value,
                    trade_date=value.trade_date,
                    unit=value.unit,
                    value_direction=value.value_direction,
                )
                for key, value in row.factor_values.items()
            },
        )
        for row in result.rows
    ]

    return FactorScreenResponseDto(
        summary=FactorScreenSummaryDto(
            screening_result="OK" if result.total_count > 0 else "EMPTY",
            total_count=result.total_count,
            displayed_count=len(rows),
        ),
        total_count=result.total_count,
        fixed_columns=[
            FactorScreenColumnDto(**column.__dict__) for column in result.fixed_columns
        ],
        factor_columns=[
            FactorScreenColumnDto(**column.__dict__) for column in result.factor_columns
        ],
        rows=rows,
    )
