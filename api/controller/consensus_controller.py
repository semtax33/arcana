from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.service.consensus_service import ConsensusReportsNotFoundError, ConsensusService
from api.service.dto import RealConsensusReportDto, RealConsensusReportsResponseDto


router = APIRouter(prefix="/api/consensus", tags=["consensus"])


@router.get("/kr/{stock_code}/reports", response_model=RealConsensusReportsResponseDto)
def get_kr_consensus_reports(stock_code: str) -> RealConsensusReportsResponseDto:
    try:
        result = ConsensusService().get_kr_reports(stock_code)
    except ConsensusReportsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RealConsensusReportsResponseDto(
        stock_code=result.stock_code,
        as_of_date=result.as_of_date,
        average_target_price=result.average_target_price,
        target_price_analyst_count=result.target_price_analyst_count,
        currency=result.currency,
        reports=[RealConsensusReportDto(**report.__dict__) for report in result.reports],
        source=result.source,
    )
