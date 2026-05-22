from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.model.financials import (
    FinancialAccountRow,
    FinancialStatementSection,
)
from api.model.financial_ratios import (
    FinancialRatioGroup,
    FinancialRatioRow,
    FinancialRatioSection,
)
from api.service.dto import (
    FinancialAccountDetailResponseDto,
    FinancialAccountRowDto,
    FinancialAccountStatisticsDto,
    FinancialChartPointDto,
    FinancialRatioGroupDto,
    FinancialRatioRowDto,
    FinancialRatiosResponseDto,
    FinancialRatioSectionDto,
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
from api.service.financial_ratios_service import (
    FinancialRatiosNotFoundError,
    FinancialRatiosService,
)


router = APIRouter(prefix="/api/financials", tags=["financials"])


@router.get("/{stock_code}/ratios", response_model=FinancialRatiosResponseDto)
def get_financial_ratios(
    stock_code: str,
    period: str = Query(default="annual"),
) -> FinancialRatiosResponseDto:
    try:
        result = FinancialRatiosService().get_ratios(stock_code, period=period)
    except FinancialRatiosNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FinancialRatiosResponseDto(
        stock=FinancialStatementMetadataDto(**result.stock.__dict__),
        period=result.period,
        financial_basis=result.financial_basis,
        columns=[FinancialPeriodColumnDto(**column.__dict__) for column in result.columns],
        sections=[_ratio_section_to_dto(section) for section in result.sections],
        source=result.source,
        auxiliary_sources=result.auxiliary_sources,
    )


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


def _ratio_section_to_dto(section: FinancialRatioSection) -> FinancialRatioSectionDto:
    return FinancialRatioSectionDto(
        statement_type=section.statement_type,
        title=section.title,
        title_en=section.title_en,
        groups=[_ratio_group_to_dto(group) for group in section.groups],
    )


def _ratio_group_to_dto(group: FinancialRatioGroup) -> FinancialRatioGroupDto:
    return FinancialRatioGroupDto(
        group_key=group.group_key,
        title=group.title,
        title_en=group.title_en,
        ratios=[_ratio_to_dto(ratio) for ratio in group.ratios],
    )


def _ratio_to_dto(ratio: FinancialRatioRow) -> FinancialRatioRowDto:
    return FinancialRatioRowDto(
        factor_id=ratio.factor_id,
        factor_name=ratio.factor_name,
        statement_type=ratio.statement_type,
        group_key=ratio.group_key,
        group_name=ratio.group_name,
        unit=ratio.unit,
        value_direction=ratio.value_direction,
        description=ratio.description,
        values=[FinancialStatementCellDto(**cell.__dict__) for cell in ratio.values],
        trend=[FinancialChartPointDto(**point.__dict__) for point in ratio.trend],
        growth_chart=[FinancialChartPointDto(**point.__dict__) for point in ratio.growth_chart],
        statistics=FinancialAccountStatisticsDto(**ratio.statistics.__dict__),
    )
