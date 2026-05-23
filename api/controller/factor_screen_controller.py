from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.dto import (
    FactorScreenColumnDto,
    FactorScreenRequestDto,
    FactorScreenResponseDto,
    FactorScreenSummaryDto,
    FactorScreenValueDto,
    ScreenedStockRowDto,
)
from api.service.factor_screen_service import FactorScreenService


router = APIRouter(prefix="/api/factor-screen", tags=["factor-screen"])


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
