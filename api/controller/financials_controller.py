from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.model.financials import (
    FinancialAccountRow,
    FinancialStatementSection,
)
from api.service.dto import (
    FinancialAccountDetailResponseDto,
    FinancialAccountRowDto,
    FinancialAccountStatisticsDto,
    FinancialChartPointDto,
    FinancialPeriodColumnDto,
    FinancialStatementCellDto,
    FinancialStatementFilter,
    FinancialStatementMetadataDto,
    FinancialStatementPeriod,
    FinancialStatementSectionDto,
    FinancialStatementsResponseDto,
)
from api.service.financials_service import (
    FinancialStatementsNotFoundError,
    FinancialStatementsService,
)


router = APIRouter(prefix="/api/financials", tags=["financials"])


@router.get("/{stock_code}", response_model=FinancialStatementsResponseDto)
def get_financial_statements(
    stock_code: str,
    period: FinancialStatementPeriod = Query(default="annual"),
    statement: FinancialStatementFilter = Query(default="all"),
) -> FinancialStatementsResponseDto:
    try:
        result = FinancialStatementsService().get_statements(
            stock_code,
            period=period,
            statement=statement,
        )
    except FinancialStatementsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FinancialStatementsResponseDto(
        stock=FinancialStatementMetadataDto(**result.stock.__dict__),
        period=result.period,
        statement=result.statement,
        columns=[FinancialPeriodColumnDto(**column.__dict__) for column in result.columns],
        sections=[_section_to_dto(section) for section in result.sections],
        source=result.source,
    )


@router.get("/{stock_code}/accounts/{canonical_id}", response_model=FinancialAccountDetailResponseDto)
def get_financial_account_detail(
    stock_code: str,
    canonical_id: str,
    period: FinancialStatementPeriod = Query(default="annual"),
) -> FinancialAccountDetailResponseDto:
    try:
        result = FinancialStatementsService().get_account_detail(
            stock_code,
            canonical_id=canonical_id,
            period=period,
        )
    except FinancialStatementsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FinancialAccountDetailResponseDto(
        stock=FinancialStatementMetadataDto(**result.stock.__dict__),
        period=result.period,
        statement_type=result.statement_type,
        account=_account_to_dto(result.account),
        columns=[FinancialPeriodColumnDto(**column.__dict__) for column in result.columns],
        source=result.source,
    )


def _section_to_dto(section: FinancialStatementSection) -> FinancialStatementSectionDto:
    return FinancialStatementSectionDto(
        statement_type=section.statement_type,
        title=section.title,
        title_en=section.title_en,
        accounts=[_account_to_dto(account) for account in section.accounts],
    )


def _account_to_dto(account: FinancialAccountRow) -> FinancialAccountRowDto:
    return FinancialAccountRowDto(
        canonical_id=account.canonical_id,
        account_name=account.account_name,
        statement_type=account.statement_type,
        is_derived=account.is_derived,
        formula=account.formula,
        description=account.description,
        unit=account.unit,
        currency=account.currency,
        values=[FinancialStatementCellDto(**cell.__dict__) for cell in account.values],
        trend=[FinancialChartPointDto(**point.__dict__) for point in account.trend],
        growth_chart=[FinancialChartPointDto(**point.__dict__) for point in account.growth_chart],
        statistics=FinancialAccountStatisticsDto(**account.statistics.__dict__),
    )
