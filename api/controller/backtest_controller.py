from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.backtest_service import BacktestService
from api.service.dto import (
    BacktestAnnualReturnDto,
    BacktestEquityCurvePointDto,
    BacktestPositionDto,
    BacktestRebalanceDto,
    BacktestSummaryDto,
    FactorBacktestRequestDto,
    FactorBacktestResponseDto,
)


router = APIRouter(prefix="/api/backtests", tags=["backtests"])


@router.post("/factor", response_model=FactorBacktestResponseDto)
def run_factor_backtest(request: FactorBacktestRequestDto) -> FactorBacktestResponseDto:
    try:
        result = BacktestService().run_factor_backtest(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FactorBacktestResponseDto(
        summary=BacktestSummaryDto(**result.summary.__dict__),
        equity_curve=[
            BacktestEquityCurvePointDto(**point.__dict__)
            for point in result.equity_curve
        ],
        rebalance_history=[
            BacktestRebalanceDto(
                rebalance_date=rebalance.rebalance_date,
                signal_date=rebalance.signal_date,
                positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.positions
                ],
                entered_positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.entered_positions
                ],
                exited_positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.exited_positions
                ],
            )
            for rebalance in result.rebalance_history
        ],
        annual_returns=[
            BacktestAnnualReturnDto(**annual_return.__dict__)
            for annual_return in result.annual_returns
        ],
        warnings=result.warnings,
    )
