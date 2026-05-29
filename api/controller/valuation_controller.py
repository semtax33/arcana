from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.model.valuation import (
    MultipleValuationResponse,
    ValuationBand,
    ValuationBenchmarkComparison,
    ValuationFactorComparison,
)
from api.service.dto import (
    MultipleValuationBandBasis,
    MultipleValuationResponseDto,
    ValuationBandDto,
    ValuationBandSummaryDto,
    ValuationBenchmarkComparisonDto,
    ValuationFactorComparisonDto,
    ValuationHistoryPointDto,
    ValuationMetricDto,
    ValuationStockMetadataDto,
)
from api.service.valuation_service import (
    DEFAULT_BUY_MARGIN_PCT,
    DEFAULT_FINANCIAL_BASIS,
    DEFAULT_LOOKBACK_YEARS,
    DEFAULT_MARKET,
    DEFAULT_SELL_MARGIN_PCT,
    MultipleValuationNotFoundError,
    MultipleValuationService,
)


router = APIRouter(prefix="/api/valuations", tags=["valuations"])


@router.get("/{stock_code}/multiple-bands", response_model=MultipleValuationResponseDto)
def get_multiple_valuation_bands(
    stock_code: str,
    as_of_date: date | None = Query(default=None),
    factor_ids: list[str] | None = Query(default=None),
    financial_basis: str = Query(default=DEFAULT_FINANCIAL_BASIS),
    lookback_years: int = Query(default=DEFAULT_LOOKBACK_YEARS, ge=1, le=10),
    buy_margin_pct: float = Query(default=DEFAULT_BUY_MARGIN_PCT, ge=0, lt=100),
    sell_margin_pct: float = Query(default=DEFAULT_SELL_MARGIN_PCT, ge=0, lt=100),
    band_basis: MultipleValuationBandBasis = Query(default="blend"),
    market: str = Query(default=DEFAULT_MARKET),
    include_history: bool = Query(default=True),
) -> MultipleValuationResponseDto:
    try:
        result = MultipleValuationService().get_multiple_valuation(
            stock_code,
            as_of_date=as_of_date,
            factor_ids=factor_ids,
            financial_basis=financial_basis,
            lookback_years=lookback_years,
            buy_margin_pct=buy_margin_pct,
            sell_margin_pct=sell_margin_pct,
            band_basis=band_basis,
            market=market,
            include_history=include_history,
        )
        return _response_to_dto(result)
    except MultipleValuationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _response_to_dto(response: MultipleValuationResponse) -> MultipleValuationResponseDto:
    return MultipleValuationResponseDto(
        stock=ValuationStockMetadataDto(**response.stock.__dict__),
        as_of_date=response.as_of_date,
        price_date=response.price_date,
        current_price=ValuationMetricDto(**response.current_price.__dict__),
        financial_basis=response.financial_basis,
        lookback_years=response.lookback_years,
        buy_margin_pct=response.buy_margin_pct,
        sell_margin_pct=response.sell_margin_pct,
        band_basis=response.band_basis,
        factor_source=response.factor_source,
        factor_ids=response.factor_ids,
        comparisons=[_comparison_to_dto(row) for row in response.comparisons],
        bands=[_band_to_dto(row) for row in response.bands],
        central_band=(
            ValuationBandSummaryDto(
                fair_price=ValuationMetricDto(**response.central_band.fair_price.__dict__),
                buy_below_price=ValuationMetricDto(**response.central_band.buy_below_price.__dict__),
                sell_above_price=ValuationMetricDto(**response.central_band.sell_above_price.__dict__),
                valid_factor_count=response.central_band.valid_factor_count,
                excluded_factor_ids=response.central_band.excluded_factor_ids,
            )
            if response.central_band is not None
            else None
        ),
        history=[ValuationHistoryPointDto(**row.__dict__) for row in response.history],
        warnings=response.warnings,
    )


def _comparison_to_dto(row: ValuationFactorComparison) -> ValuationFactorComparisonDto:
    return ValuationFactorComparisonDto(
        factor_id=row.factor_id,
        factor_name=row.factor_name,
        unit=row.unit,
        direction=row.direction,
        current=ValuationMetricDto(**row.current.__dict__),
        comparisons=[_benchmark_to_dto(item) for item in row.comparisons],
    )


def _benchmark_to_dto(row: ValuationBenchmarkComparison) -> ValuationBenchmarkComparisonDto:
    return ValuationBenchmarkComparisonDto(
        benchmark_key=row.benchmark_key,
        benchmark_name=row.benchmark_name,
        value=ValuationMetricDto(**row.value.__dict__),
        difference_pct=row.difference_pct,
        signal=row.signal,
        signal_label=row.signal_label,
    )


def _band_to_dto(row: ValuationBand) -> ValuationBandDto:
    return ValuationBandDto(
        factor_id=row.factor_id,
        factor_name=row.factor_name,
        current_multiple=ValuationMetricDto(**row.current_multiple.__dict__),
        target_multiple=ValuationMetricDto(**row.target_multiple.__dict__),
        target_source=row.target_source,
        fair_price=ValuationMetricDto(**row.fair_price.__dict__),
        buy_below_price=ValuationMetricDto(**row.buy_below_price.__dict__),
        sell_above_price=ValuationMetricDto(**row.sell_above_price.__dict__),
        upside_pct=row.upside_pct,
        signal=row.signal,
        signal_label=row.signal_label,
        warning=row.warning,
    )
