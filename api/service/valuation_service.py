from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from statistics import median
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.valuation import (
    MultipleValuationResponse,
    ValuationBand,
    ValuationBandSummary,
    ValuationBenchmarkComparison,
    ValuationFactorComparison,
    ValuationHistoryPoint,
    ValuationMetric,
    ValuationStockMetadata,
)


FACTOR_TABLES = ("fact_daily_factors",)
FACTOR_VALUE_COLUMNS = ("factor_value",)
DEFAULT_FINANCIAL_BASIS = "ttm"
SUPPORTED_FINANCIAL_BASES = {"annual", "quarterly", "ttm", "forward"}
DEFAULT_MARKET = "kr"
DEFAULT_LOOKBACK_YEARS = 3
DEFAULT_BUY_MARGIN_PCT = 20.0
DEFAULT_SELL_MARGIN_PCT = 10.0
DEFAULT_BAND_BASIS = "blend"
MARKET_STATS_MAX_EXECUTION_SECONDS = 6
CROSS_SECTION_LOOKBACK_DAYS = 45
MIN_HISTORICAL_STATS_POINTS = 6
MIN_CENTRAL_BAND_FACTORS = 2
MARKETS = {"kr", "us"}
BAND_BASES = {"blend", "historical", "industry", "market", "listing_market"}
SUPPORTED_MULTIPLE_FACTORS = [
    "ev_to_nopat",
    "ev_to_ebitda",
    "per",
    "pbr",
    "eps_yoy_pct",
    "fcfpr",
    "rnd_to_market_cap",
    "rpr",
    "fcf_to_ev_yield",
    "peg",
    "psr",
    "pcr",
]
FACTOR_ALIASES = {
    "ev/nopat": "ev_to_nopat",
    "ev_nopat": "ev_to_nopat",
    "ev-to-nopat": "ev_to_nopat",
    "ev/ebitda": "ev_to_ebitda",
    "ev_ebitda": "ev_to_ebitda",
    "ev-to-ebitda": "ev_to_ebitda",
    "eps yoy": "eps_yoy_pct",
    "eps_yoy": "eps_yoy_pct",
    "eps_yoy_pct": "eps_yoy_pct",
    "r&d / market cap": "rnd_to_market_cap",
    "r&d/market cap": "rnd_to_market_cap",
    "rd_to_market_cap": "rnd_to_market_cap",
    "r_d_to_market_cap": "rnd_to_market_cap",
    "fcf/ev yield": "fcf_to_ev_yield",
    "fcf_ev_yield": "fcf_to_ev_yield",
    "fcf_to_ev_yield": "fcf_to_ev_yield",
}
LOWER_IS_BETTER_FACTORS = {
    "ev_to_nopat",
    "ev_to_ebitda",
    "per",
    "pbr",
    "peg",
    "psr",
    "pcr",
}
HIGHER_IS_BETTER_FACTORS = {
    "eps_yoy_pct",
    "fcfpr",
    "rnd_to_market_cap",
    "rpr",
    "fcf_to_ev_yield",
}
PRICE_PROPORTIONAL_FACTORS = LOWER_IS_BETTER_FACTORS
PRICE_INVERSE_FACTORS = {
    "fcfpr",
    "rnd_to_market_cap",
    "rpr",
    "fcf_to_ev_yield",
}
FACTOR_LABELS = {
    "ev_to_nopat": "EV/NOPAT",
    "ev_to_ebitda": "EV/EBITDA",
    "per": "PER",
    "pbr": "PBR",
    "eps_yoy_pct": "EPS YoY",
    "fcfpr": "FCFPR",
    "rnd_to_market_cap": "R&D / Market Cap",
    "rpr": "RPR",
    "fcf_to_ev_yield": "FCF/EV Yield",
    "peg": "PEG",
    "psr": "PSR",
    "pcr": "PCR",
}
PERCENT_FACTORS = {"eps_yoy_pct", "rnd_to_market_cap", "fcf_to_ev_yield"}
_SYMBOL_RE = re.compile(r"^[0-9A-Za-z.\-_]{1,32}$")
DEFAULT_CENTRAL_BAND_FACTORS = (
    "ev_to_nopat",
    "ev_to_ebitda",
    "per",
    "pbr",
    "fcfpr",
    "fcf_to_ev_yield",
    "psr",
    "pcr",
)
SECTOR_CENTRAL_BAND_FACTORS = {
    "10": ("ev_to_ebitda", "fcf_to_ev_yield", "per", "pbr", "pcr"),
    "15": ("ev_to_ebitda", "pbr", "per", "fcf_to_ev_yield", "pcr"),
    "20": ("ev_to_ebitda", "ev_to_nopat", "per", "fcf_to_ev_yield", "pbr", "pcr"),
    "25": ("per", "ev_to_ebitda", "fcf_to_ev_yield", "psr", "pcr"),
    "30": ("per", "ev_to_ebitda", "fcf_to_ev_yield", "pcr", "psr"),
    "35": ("per", "ev_to_ebitda", "fcf_to_ev_yield", "psr"),
    "40": ("pbr", "per", "pcr"),
    "45": ("ev_to_nopat", "ev_to_ebitda", "fcf_to_ev_yield", "fcfpr", "pcr", "per", "pbr", "psr"),
    "50": ("ev_to_ebitda", "fcf_to_ev_yield", "per", "psr", "rpr"),
    "55": ("pbr", "per", "ev_to_ebitda", "fcf_to_ev_yield"),
    "60": ("pbr", "per"),
}
INDUSTRY_GROUP_CENTRAL_BAND_FACTORS = {
    "3510": ("per", "ev_to_ebitda", "fcf_to_ev_yield", "psr"),
    "3520": ("per", "ev_to_ebitda", "fcf_to_ev_yield", "psr"),
    "4010": ("pbr", "per", "pcr"),
    "4020": ("pbr", "per", "pcr"),
    "4030": ("pbr", "per", "pcr"),
    "4510": ("psr", "fcf_to_ev_yield", "ev_to_nopat", "rpr"),
    "4520": ("ev_to_nopat", "ev_to_ebitda", "fcf_to_ev_yield", "fcfpr", "pcr", "per"),
    "4530": ("ev_to_nopat", "ev_to_ebitda", "fcfpr", "fcf_to_ev_yield", "pcr"),
    "5010": ("ev_to_ebitda", "fcf_to_ev_yield", "per", "pbr"),
    "5020": ("psr", "ev_to_ebitda", "fcf_to_ev_yield", "per", "rpr"),
    "6010": ("pbr", "per"),
    "6020": ("pbr", "per"),
}


class MultipleValuationNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class FactorValue:
    factor_id: str
    value: float | None
    trade_date: date | None = None


@dataclass(frozen=True)
class FactorStats:
    avg_value: float | None = None
    median_value: float | None = None
    p25_value: float | None = None
    p75_value: float | None = None
    n: int = 0


@dataclass(frozen=True)
class PriceSnapshot:
    trade_date: date | None
    close: float | None
    currency: str | None = None


class MultipleValuationService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst

    def get_multiple_valuation(
        self,
        stock_code: str,
        *,
        as_of_date: date | None = None,
        factor_ids: list[str] | None = None,
        financial_basis: str = DEFAULT_FINANCIAL_BASIS,
        lookback_years: int = DEFAULT_LOOKBACK_YEARS,
        buy_margin_pct: float = DEFAULT_BUY_MARGIN_PCT,
        sell_margin_pct: float = DEFAULT_SELL_MARGIN_PCT,
        band_basis: str = DEFAULT_BAND_BASIS,
        market: str = DEFAULT_MARKET,
        include_history: bool = True,
    ) -> MultipleValuationResponse:
        normalized_market = _normalize_market(market)
        normalized_stock_code = _normalize_stock_code(stock_code, normalized_market)
        security_id = _security_id(normalized_stock_code, normalized_market)
        as_of = as_of_date or self._today_factory()
        normalized_factor_ids = _normalize_factor_ids(factor_ids)
        normalized_basis = _normalize_financial_basis(financial_basis)
        normalized_lookback = _normalize_lookback_years(lookback_years)
        normalized_buy_margin = _normalize_margin(buy_margin_pct, "buy_margin_pct")
        normalized_sell_margin = _normalize_margin(sell_margin_pct, "sell_margin_pct")
        normalized_band_basis = _normalize_band_basis(band_basis)

        client = self._client_factory()
        try:
            metadata, price = _load_stock_context(
                client,
                stock_code=normalized_stock_code,
                security_id=security_id,
                as_of_date=as_of,
            )
            current_values, factor_source = _load_current_factor_values(
                client,
                security_id=security_id,
                as_of_date=as_of,
                factor_ids=normalized_factor_ids,
                financial_basis=normalized_basis,
            )
            history_stats, history_points = _load_historical_factor_bundle(
                client,
                security_id=security_id,
                as_of_date=as_of,
                factor_ids=normalized_factor_ids,
                financial_basis="annual",
                lookback_years=normalized_lookback,
                factor_source=factor_source,
                include_history=include_history,
            )
            industry_stats = _load_industry_factor_stats(
                client,
                as_of_date=as_of,
                factor_ids=normalized_factor_ids,
                financial_basis=normalized_basis,
                factor_source=factor_source,
                metadata=metadata,
            )
            market_stats = _load_market_factor_stats(
                client,
                as_of_date=as_of,
                factor_ids=normalized_factor_ids,
                financial_basis=normalized_basis,
                market=normalized_market,
                factor_source=factor_source,
            )
            listing_market_stats = _load_listing_market_factor_stats(
                client,
                as_of_date=as_of,
                factor_ids=normalized_factor_ids,
                financial_basis=normalized_basis,
                factor_source=factor_source,
                metadata=metadata,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        current_by_factor = {item.factor_id: item for item in current_values}
        if price.close is None and not current_by_factor:
            raise MultipleValuationNotFoundError(
                f"multiple valuation data not found for stock_code={stock_code}"
            )

        comparisons = _build_comparisons(
            factor_ids=normalized_factor_ids,
            current_by_factor=current_by_factor,
            history_stats=history_stats,
            market_stats=market_stats,
            industry_stats=industry_stats,
            listing_market_stats=listing_market_stats,
            metadata=metadata,
        )
        bands, band_warnings = _build_bands(
            factor_ids=normalized_factor_ids,
            current_by_factor=current_by_factor,
            current_price=price.close,
            history_stats=history_stats,
            market_stats=market_stats,
            industry_stats=industry_stats,
            listing_market_stats=listing_market_stats,
            buy_margin_pct=normalized_buy_margin,
            sell_margin_pct=normalized_sell_margin,
            band_basis=normalized_band_basis,
        )
        central_band = _build_central_band_summary(
            bands,
            metadata=metadata,
            buy_margin_pct=normalized_buy_margin,
            sell_margin_pct=normalized_sell_margin,
        )
        warnings = []
        if price.close is None:
            warnings.append("latest close price is missing; price bands are unavailable")
        warnings.extend(band_warnings)

        return MultipleValuationResponse(
            stock=metadata,
            as_of_date=as_of,
            price_date=price.trade_date,
            current_price=_metric(price.close, "price"),
            financial_basis=normalized_basis,
            lookback_years=normalized_lookback,
            buy_margin_pct=normalized_buy_margin,
            sell_margin_pct=normalized_sell_margin,
            band_basis=normalized_band_basis,
            factor_source=factor_source,
            factor_ids=normalized_factor_ids,
            comparisons=comparisons,
            bands=bands,
            central_band=central_band,
            history=history_points,
            warnings=warnings,
        )


def _load_metadata(
    client: Any,
    *,
    stock_code: str,
    security_id: str,
) -> ValuationStockMetadata:
    try:
        rows = _records(
            client.query_df(
                """
SELECT
    sm.security_id AS security_id,
    any(id.id_value) AS ticker,
    any(iss.legal_name_ko) AS stock_name,
    any(iss.legal_name_en) AS stock_name_en,
    any(sm.country) AS country,
    any(sm.currency) AS currency,
    any(sm.primary_market_mic) AS primary_market_mic,
    any(iss.industry_schema) AS industry_schema,
    any(iss.sector_code) AS sector_code,
    any(iss.industry_group_code) AS industry_group_code,
    any(iss.industry_group_name) AS industry_group_name
FROM security_master AS sm
LEFT JOIN identifiers AS id
    ON id.security_id = sm.security_id
    AND id.id_type = 'TICKER'
    AND id.is_primary
LEFT JOIN issuers AS iss
    ON iss.issuer_id = sm.issuer_id
WHERE sm.security_id = {security_id:String}
GROUP BY sm.security_id
""".strip(),
                parameters={"security_id": security_id},
            )
        )
    except Exception:
        rows = []

    row = rows[0] if rows else {}
    return ValuationStockMetadata(
        stock_code=_optional_str(row.get("ticker")) or stock_code,
        security_id=_optional_str(row.get("security_id")) or security_id,
        stock_name=_optional_str(row.get("stock_name")),
        stock_name_en=_optional_str(row.get("stock_name_en")),
        country=_optional_str(row.get("country")) or ("US" if security_id.startswith("SEC_US_") else "KR"),
        currency=_optional_str(row.get("currency")) or ("USD" if security_id.startswith("SEC_US_") else "KRW"),
        primary_market_mic=_optional_str(row.get("primary_market_mic")) or "",
        industry_schema=_optional_str(row.get("industry_schema")) or "",
        sector_code=_optional_str(row.get("sector_code")) or "",
        industry_group_code=_optional_str(row.get("industry_group_code")) or "",
        industry_group_name=_optional_str(row.get("industry_group_name")) or "",
    )


def _load_stock_context(
    client: Any,
    *,
    stock_code: str,
    security_id: str,
    as_of_date: date,
) -> tuple[ValuationStockMetadata, PriceSnapshot]:
    try:
        rows = _records(
            client.query_df(
                """
WITH latest_price AS (
    SELECT
        security_id,
        trade_date,
        toFloat64(close) AS close,
        currency
    FROM price_daily
    WHERE security_id = {security_id:String}
        AND trade_date <= {as_of_date:Date}
        AND close IS NOT NULL
    ORDER BY trade_date DESC
    LIMIT 1
)
SELECT
    sm.security_id AS security_id,
    any(id.id_value) AS ticker,
    any(iss.legal_name_ko) AS stock_name,
    any(iss.legal_name_en) AS stock_name_en,
    any(sm.country) AS country,
    any(sm.currency) AS security_currency,
    any(sm.primary_market_mic) AS primary_market_mic,
    any(iss.industry_schema) AS industry_schema,
    any(iss.sector_code) AS sector_code,
    any(iss.industry_group_code) AS industry_group_code,
    any(iss.industry_group_name) AS industry_group_name,
    any(lp.trade_date) AS price_trade_date,
    any(lp.close) AS close,
    any(lp.currency) AS price_currency
FROM security_master AS sm
LEFT JOIN identifiers AS id
    ON id.security_id = sm.security_id
    AND id.id_type = 'TICKER'
    AND id.is_primary
LEFT JOIN issuers AS iss
    ON iss.issuer_id = sm.issuer_id
LEFT JOIN latest_price AS lp
    ON lp.security_id = sm.security_id
WHERE sm.security_id = {security_id:String}
GROUP BY sm.security_id
""".strip(),
                parameters={"security_id": security_id, "as_of_date": as_of_date.isoformat()},
            )
        )
    except Exception:
        rows = []

    if not rows:
        return (
            _load_metadata(client, stock_code=stock_code, security_id=security_id),
            _load_price_snapshot(client, security_id=security_id, as_of_date=as_of_date),
        )

    row = rows[0]
    currency = (
        _optional_str(row.get("price_currency"))
        or _optional_str(row.get("security_currency"))
        or ("USD" if security_id.startswith("SEC_US_") else "KRW")
    )
    metadata = ValuationStockMetadata(
        stock_code=_optional_str(row.get("ticker")) or stock_code,
        security_id=_optional_str(row.get("security_id")) or security_id,
        stock_name=_optional_str(row.get("stock_name")),
        stock_name_en=_optional_str(row.get("stock_name_en")),
        country=_optional_str(row.get("country")) or ("US" if security_id.startswith("SEC_US_") else "KR"),
        currency=currency,
        primary_market_mic=_optional_str(row.get("primary_market_mic")) or "",
        industry_schema=_optional_str(row.get("industry_schema")) or "",
        sector_code=_optional_str(row.get("sector_code")) or "",
        industry_group_code=_optional_str(row.get("industry_group_code")) or "",
        industry_group_name=_optional_str(row.get("industry_group_name")) or "",
    )
    price = PriceSnapshot(
        trade_date=_as_date_or_none(row.get("price_trade_date")),
        close=_float_or_none(row.get("close")),
        currency=currency,
    )
    return metadata, price


def _load_price_snapshot(client: Any, *, security_id: str, as_of_date: date) -> PriceSnapshot:
    try:
        rows = _records(
            client.query_df(
                """
SELECT
    trade_date,
    toFloat64(close) AS close,
    currency
FROM price_daily
WHERE security_id = {security_id:String}
    AND trade_date <= {as_of_date:Date}
    AND close IS NOT NULL
ORDER BY trade_date DESC
LIMIT 1
""".strip(),
                parameters={"security_id": security_id, "as_of_date": as_of_date.isoformat()},
            )
        )
    except Exception:
        rows = []

    row = rows[0] if rows else {}
    return PriceSnapshot(
        trade_date=_as_date_or_none(row.get("trade_date")),
        close=_float_or_none(row.get("close")),
        currency=_optional_str(row.get("currency")),
    )


def _load_current_factor_values(
    client: Any,
    *,
    security_id: str,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
) -> tuple[list[FactorValue], str]:
    def build_query(table_name: str, value_column: str) -> str:
        return f"""
WITH latest_factor_dates AS (
    SELECT
        factor_id,
        max(trade_date) AS latest_trade_date
    FROM {table_name}
    PREWHERE
        security_id = {{security_id:String}}
        AND trade_date <= {{as_of_date:Date}}
        AND trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, factor_id)
    WHERE isFinite({value_column})
    GROUP BY factor_id
)
SELECT
    f.factor_id AS factor_id,
    argMax(f.{value_column}, f.updated_at) AS value,
    any(d.latest_trade_date) AS latest_trade_date
FROM {table_name} AS f
INNER JOIN latest_factor_dates AS d
    ON d.factor_id = f.factor_id
    AND d.latest_trade_date = f.trade_date
PREWHERE
    f.security_id = {{security_id:String}}
    AND f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
    AND f.financial_basis = {{financial_basis:String}}
    AND has({{factor_ids:Array(String)}}, f.factor_id)
WHERE isFinite(f.{value_column})
GROUP BY f.factor_id
""".strip()

    values_by_factor: dict[str, FactorValue] = {}
    factor_source = FACTOR_TABLES[0]
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, values_by_factor)
        if not missing_factor_ids:
            break
        for table_name, value_column in _factor_sources():
            try:
                rows = _records(
                    client.query_df(
                        build_query(table_name, value_column),
                        parameters={
                            "security_id": security_id,
                            "as_of_date": as_of_date.isoformat(),
                            "factor_ids": missing_factor_ids,
                            "financial_basis": basis,
                        },
                    )
                )
            except Exception:
                continue
            values = [_to_factor_value(row) for row in rows]
            if values:
                factor_source = table_name
            for value in values:
                if value.factor_id in missing_factor_ids and value.factor_id not in values_by_factor:
                    values_by_factor[value.factor_id] = value
            if not _missing_factor_ids(factor_ids, values_by_factor):
                break
    return [values_by_factor[factor_id] for factor_id in factor_ids if factor_id in values_by_factor], factor_source


def _load_historical_factor_stats(
    client: Any,
    *,
    security_id: str,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    lookback_years: int,
    factor_source: str,
) -> dict[str, FactorStats]:
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    start_date = _shift_years(as_of_date, -lookback_years)
    stats_by_factor: dict[str, FactorStats] = {}
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, stats_by_factor)
        if not missing_factor_ids:
            break
        for table_name in source_order:
            for value_column in FACTOR_VALUE_COLUMNS:
                try:
                    rows = _records(
                        client.query_df(
                            _build_historical_stats_query(table_name, value_column),
                            parameters={
                                "security_id": security_id,
                                "start_date": start_date.isoformat(),
                                "as_of_date": as_of_date.isoformat(),
                                "factor_ids": missing_factor_ids,
                                "financial_basis": basis,
                            },
                        )
                    )
                except Exception:
                    continue
                result = _stats_by_factor(rows)
                stats_by_factor.update(
                    {
                        factor_id: stats
                        for factor_id, stats in result.items()
                        if factor_id in missing_factor_ids
                        and factor_id not in stats_by_factor
                        and not _should_fallback_historical_stats(factor_id, stats, basis)
                    }
                )
                if not _missing_factor_ids(factor_ids, stats_by_factor):
                    break
            if not _missing_factor_ids(factor_ids, stats_by_factor):
                break
    return stats_by_factor


def _load_historical_factor_bundle(
    client: Any,
    *,
    security_id: str,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    lookback_years: int,
    factor_source: str,
    include_history: bool,
) -> tuple[dict[str, FactorStats], list[ValuationHistoryPoint]]:
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    start_date = _shift_years(as_of_date, -lookback_years)
    stats_by_factor: dict[str, FactorStats] = {}
    history_points: list[ValuationHistoryPoint] = []
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, stats_by_factor)
        if not missing_factor_ids:
            break
        for table_name in source_order:
            for value_column in FACTOR_VALUE_COLUMNS:
                parameters = {
                    "security_id": security_id,
                    "start_date": start_date.isoformat(),
                    "as_of_date": as_of_date.isoformat(),
                    "factor_ids": missing_factor_ids,
                    "financial_basis": basis,
                }
                try:
                    rows = _records(
                        client.query_df(
                            _build_historical_bundle_query(
                                table_name,
                                value_column,
                                include_history=include_history,
                            ),
                            parameters=parameters,
                        )
                    )
                except Exception:
                    rows = []
                if not rows:
                    try:
                        rows = _records(
                            client.query_df(
                                _build_historical_bundle_plain_query(
                                    table_name,
                                    value_column,
                                    include_history=include_history,
                                ),
                                parameters=parameters,
                            )
                        )
                    except Exception:
                        rows = []
                if not rows:
                    continue
                stats = _stats_by_factor(rows)
                added_factor_ids = {
                    factor_id
                    for factor_id, stat in stats.items()
                    if factor_id in missing_factor_ids
                    and factor_id not in stats_by_factor
                    and not _should_fallback_historical_stats(factor_id, stat, basis)
                }
                stats_by_factor.update(
                    {
                        factor_id: stats[factor_id]
                        for factor_id in added_factor_ids
                    }
                )
                if include_history and added_factor_ids:
                    history_points.extend(
                        point
                        for point in _history_points_from_bundle_rows(rows)
                        if point.factor_id in added_factor_ids
                    )
                if not _missing_factor_ids(factor_ids, stats_by_factor):
                    break
            if not _missing_factor_ids(factor_ids, stats_by_factor):
                break
    return stats_by_factor, sorted(history_points, key=lambda item: (item.period, item.factor_id))


def _build_historical_bundle_query(
    table_name: str,
    value_column: str,
    *,
    include_history: bool,
) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    history_expr = (
        ",\n    arraySort(groupArray(tuple(mv.period, mv.value))) AS history_points"
        if include_history
        else ""
    )
    return f"""
WITH monthly_values AS (
    SELECT
        factor_id,
        toDate(toStartOfMonth(trade_date)) AS period,
        argMax({value_column}, tuple(trade_date, updated_at)) AS value
    FROM {table_name}
    PREWHERE
        security_id = {{security_id:String}}
        AND trade_date >= {{start_date:Date}}
        AND trade_date <= {{as_of_date:Date}}
        AND financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, factor_id)
    WHERE isFinite({value_column})
    GROUP BY
        factor_id,
        period
),
bounds AS (
    SELECT
        factor_id,
        quantileExact(0.10)(value) AS p10_value,
        quantileExact(0.90)(value) AS p90_value
    FROM monthly_values
    GROUP BY factor_id
)
SELECT
    mv.factor_id AS factor_id,
    avg(if(mv.value < b.p10_value, b.p10_value, if(mv.value > b.p90_value, b.p90_value, mv.value))) AS avg_value,
    quantileExact(0.5)(mv.value) AS median_value,
    quantileExact(0.25)(mv.value) AS p25_value,
    quantileExact(0.75)(mv.value) AS p75_value,
    count() AS n{history_expr}
FROM monthly_values AS mv
INNER JOIN bounds AS b
    ON b.factor_id = mv.factor_id
GROUP BY mv.factor_id
""".strip()


def _build_historical_bundle_plain_query(
    table_name: str,
    value_column: str,
    *,
    include_history: bool,
) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    history_expr = (
        ",\n    arraySort(groupArray(tuple(period, value))) AS history_points"
        if include_history
        else ""
    )
    return f"""
WITH monthly_values AS (
    SELECT
        factor_id,
        toDate(toStartOfMonth(trade_date)) AS period,
        argMax({value_column}, tuple(trade_date, updated_at)) AS value
    FROM {table_name}
    PREWHERE
        security_id = {{security_id:String}}
        AND trade_date >= {{start_date:Date}}
        AND trade_date <= {{as_of_date:Date}}
        AND financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, factor_id)
    WHERE isFinite({value_column})
    GROUP BY
        factor_id,
        period
)
SELECT
    factor_id,
    avg(value) AS avg_value,
    quantileExact(0.5)(value) AS median_value,
    quantileExact(0.25)(value) AS p25_value,
    quantileExact(0.75)(value) AS p75_value,
    count() AS n{history_expr}
FROM monthly_values
GROUP BY factor_id
""".strip()


def _build_historical_stats_query(table_name: str, value_column: str) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    return f"""
WITH raw_values AS (
    SELECT
        factor_id,
        {value_column} AS value
    FROM {table_name}
    WHERE security_id = {{security_id:String}}
        AND trade_date >= {{start_date:Date}}
        AND trade_date <= {{as_of_date:Date}}
        AND financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, factor_id)
        AND isFinite({value_column})
),
bounds AS (
    SELECT
        factor_id,
        quantileExact(0.10)(value) AS p10_value,
        quantileExact(0.90)(value) AS p90_value
    FROM raw_values
    GROUP BY factor_id
)
SELECT
    rv.factor_id AS factor_id,
    avg(if(rv.value < b.p10_value, b.p10_value, if(rv.value > b.p90_value, b.p90_value, rv.value))) AS avg_value,
    quantileExact(0.5)(rv.value) AS median_value,
    quantileExact(0.25)(rv.value) AS p25_value,
    quantileExact(0.75)(rv.value) AS p75_value,
    count() AS n
FROM raw_values AS rv
INNER JOIN bounds AS b
    ON b.factor_id = rv.factor_id
GROUP BY rv.factor_id
""".strip()


def _load_market_factor_stats(
    client: Any,
    *,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    market: str,
    factor_source: str,
) -> dict[str, FactorStats]:
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    stats_by_factor: dict[str, FactorStats] = {}
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, stats_by_factor)
        if not missing_factor_ids:
            break
        for table_name in source_order:
            for value_column in FACTOR_VALUE_COLUMNS:
                try:
                    rows = _records(
                        client.query_df(
                            _build_market_stats_query(table_name, value_column),
                            parameters={
                                "as_of_date": as_of_date.isoformat(),
                                "factor_ids": missing_factor_ids,
                                "financial_basis": basis,
                                "market_country": market.upper(),
                            },
                        )
                    )
                except Exception:
                    continue
                result = _stats_by_factor(rows)
                stats_by_factor.update(
                    {
                        factor_id: stats
                        for factor_id, stats in result.items()
                        if factor_id in missing_factor_ids and factor_id not in stats_by_factor
                    }
                )
                if not _missing_factor_ids(factor_ids, stats_by_factor):
                    break
            if not _missing_factor_ids(factor_ids, stats_by_factor):
                break
    return stats_by_factor


def _load_listing_market_factor_stats(
    client: Any,
    *,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    factor_source: str,
    metadata: ValuationStockMetadata,
) -> dict[str, FactorStats]:
    if not metadata.primary_market_mic:
        return {}
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    stats_by_factor: dict[str, FactorStats] = {}
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, stats_by_factor)
        if not missing_factor_ids:
            break
        for table_name in source_order:
            for value_column in FACTOR_VALUE_COLUMNS:
                try:
                    rows = _records(
                        client.query_df(
                            _build_listing_market_stats_query(table_name, value_column),
                            parameters={
                                "as_of_date": as_of_date.isoformat(),
                                "factor_ids": missing_factor_ids,
                                "financial_basis": basis,
                                "market_country": metadata.country or "",
                                "primary_market_mic": metadata.primary_market_mic,
                            },
                        )
                    )
                except Exception:
                    continue
                result = _stats_by_factor(rows)
                stats_by_factor.update(
                    {
                        factor_id: stats
                        for factor_id, stats in result.items()
                        if factor_id in missing_factor_ids and factor_id not in stats_by_factor
                    }
                )
                if not _missing_factor_ids(factor_ids, stats_by_factor):
                    break
            if not _missing_factor_ids(factor_ids, stats_by_factor):
                break
    return stats_by_factor


def _build_market_stats_query(table_name: str, value_column: str) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    return f"""
WITH latest_factor_dates AS (
    SELECT
        f.factor_id AS factor_id,
        max(f.trade_date) AS latest_trade_date
    FROM {table_name} AS f
    INNER JOIN security_master AS sm
        ON sm.security_id = f.security_id
    PREWHERE
        f.trade_date <= {{as_of_date:Date}}
        AND f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE sm.country = {{market_country:String}}
        AND sm.is_active
        AND isFinite(f.{value_column})
    GROUP BY f.factor_id
),
latest_factors AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        argMax(f.{value_column}, f.updated_at) AS value
    FROM {table_name} AS f
    INNER JOIN latest_factor_dates AS d
        ON d.factor_id = f.factor_id
        AND d.latest_trade_date = f.trade_date
    INNER JOIN security_master AS sm
        ON sm.security_id = f.security_id
    PREWHERE
        f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE sm.country = {{market_country:String}}
        AND sm.is_active
        AND isFinite(f.{value_column})
    GROUP BY
        f.security_id,
        f.factor_id
),
bounds AS (
    SELECT
        factor_id,
        quantileExact(0.10)(value) AS p10_value,
        quantileExact(0.90)(value) AS p90_value
    FROM latest_factors
    GROUP BY factor_id
)
SELECT
    lf.factor_id AS factor_id,
    avg(if(lf.value < b.p10_value, b.p10_value, if(lf.value > b.p90_value, b.p90_value, lf.value))) AS avg_value,
    quantileExact(0.5)(lf.value) AS median_value,
    quantileExact(0.25)(lf.value) AS p25_value,
    quantileExact(0.75)(lf.value) AS p75_value,
    count() AS n
FROM latest_factors AS lf
INNER JOIN bounds AS b
    ON b.factor_id = lf.factor_id
GROUP BY lf.factor_id
SETTINGS max_execution_time = {MARKET_STATS_MAX_EXECUTION_SECONDS}
""".strip()


def _build_listing_market_stats_query(table_name: str, value_column: str) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    return f"""
WITH latest_factor_dates AS (
    SELECT
        f.factor_id AS factor_id,
        max(f.trade_date) AS latest_trade_date
    FROM {table_name} AS f
    INNER JOIN security_master AS sm
        ON sm.security_id = f.security_id
    PREWHERE
        f.trade_date <= {{as_of_date:Date}}
        AND f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE sm.country = {{market_country:String}}
        AND sm.primary_market_mic = {{primary_market_mic:String}}
        AND sm.is_active
        AND isFinite(f.{value_column})
    GROUP BY f.factor_id
),
latest_factors AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        argMax(f.{value_column}, f.updated_at) AS value
    FROM {table_name} AS f
    INNER JOIN latest_factor_dates AS d
        ON d.factor_id = f.factor_id
        AND d.latest_trade_date = f.trade_date
    INNER JOIN security_master AS sm
        ON sm.security_id = f.security_id
    PREWHERE
        f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE sm.country = {{market_country:String}}
        AND sm.primary_market_mic = {{primary_market_mic:String}}
        AND sm.is_active
        AND isFinite(f.{value_column})
    GROUP BY
        f.security_id,
        f.factor_id
),
bounds AS (
    SELECT
        factor_id,
        quantileExact(0.10)(value) AS p10_value,
        quantileExact(0.90)(value) AS p90_value
    FROM latest_factors
    GROUP BY factor_id
)
SELECT
    lf.factor_id AS factor_id,
    avg(if(lf.value < b.p10_value, b.p10_value, if(lf.value > b.p90_value, b.p90_value, lf.value))) AS avg_value,
    quantileExact(0.5)(lf.value) AS median_value,
    quantileExact(0.25)(lf.value) AS p25_value,
    quantileExact(0.75)(lf.value) AS p75_value,
    count() AS n
FROM latest_factors AS lf
INNER JOIN bounds AS b
    ON b.factor_id = lf.factor_id
GROUP BY lf.factor_id
SETTINGS max_execution_time = {MARKET_STATS_MAX_EXECUTION_SECONDS}
""".strip()


def _load_industry_factor_stats(
    client: Any,
    *,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    factor_source: str,
    metadata: ValuationStockMetadata,
) -> dict[str, FactorStats]:
    if not metadata.industry_group_code:
        return {}
    cross_section_stats = _load_industry_cross_section_factor_stats(
        client,
        as_of_date=as_of_date,
        factor_ids=factor_ids,
        financial_basis=financial_basis,
        factor_source=factor_source,
        metadata=metadata,
    )
    if cross_section_stats and not _missing_factor_ids(factor_ids, cross_section_stats):
        return cross_section_stats

    rows = []
    for level in ("industry_group", "INDUSTRY_GROUP"):
        try:
            rows = _records(
                client.query_df(
                    """
SELECT
    factor_id,
    argMax(coalesce(winsor_avg_value, avg_value), tuple(trade_date, updated_at)) AS avg_value,
    argMax(median_value, tuple(trade_date, updated_at)) AS median_value,
    argMax(p25_value, tuple(trade_date, updated_at)) AS p25_value,
    argMax(p75_value, tuple(trade_date, updated_at)) AS p75_value,
    argMax(n_companies, tuple(trade_date, updated_at)) AS n
FROM industry_factor_daily_snapshot
WHERE trade_date <= {as_of_date:Date}
    AND industry_schema = {industry_schema:String}
    AND industry_level = {industry_level:String}
    AND industry_code = {industry_code:String}
    AND has({factor_ids:Array(String)}, factor_id)
GROUP BY factor_id
""".strip(),
                    parameters={
                        "as_of_date": as_of_date.isoformat(),
                        "industry_schema": metadata.industry_schema or "GICS",
                        "industry_level": level,
                        "industry_code": metadata.industry_group_code,
                        "factor_ids": factor_ids,
                    },
                )
            )
        except Exception:
            rows = []
        if rows:
            break
    snapshot_stats = _stats_by_factor(rows)
    missing_factor_ids = [
        factor_id for factor_id in factor_ids if factor_id not in snapshot_stats and factor_id not in cross_section_stats
    ]
    if snapshot_stats and not missing_factor_ids:
        return {**snapshot_stats, **cross_section_stats}

    result = _load_industry_cross_section_factor_stats(
        client,
        as_of_date=as_of_date,
        factor_ids=missing_factor_ids or factor_ids,
        financial_basis=financial_basis,
        factor_source=factor_source,
        metadata=metadata,
    )
    return {**result, **snapshot_stats, **cross_section_stats}


def _load_industry_cross_section_factor_stats(
    client: Any,
    *,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    factor_source: str,
    metadata: ValuationStockMetadata,
) -> dict[str, FactorStats]:
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    stats_by_factor: dict[str, FactorStats] = {}
    for basis in _financial_basis_order(financial_basis):
        missing_factor_ids = _missing_factor_ids(factor_ids, stats_by_factor)
        if not missing_factor_ids:
            break
        for table_name in source_order:
            for value_column in FACTOR_VALUE_COLUMNS:
                try:
                    rows = _records(
                        client.query_df(
                            _build_industry_cross_section_stats_query(table_name, value_column),
                            parameters={
                                "as_of_date": as_of_date.isoformat(),
                                "factor_ids": missing_factor_ids,
                                "financial_basis": basis,
                                "industry_schema": metadata.industry_schema or "GICS",
                                "industry_group_code": metadata.industry_group_code,
                                "market_country": metadata.country or "",
                            },
                        )
                    )
                except Exception:
                    continue
                result = _stats_by_factor(rows)
                stats_by_factor.update(
                    {
                        factor_id: stats
                        for factor_id, stats in result.items()
                        if factor_id in missing_factor_ids and factor_id not in stats_by_factor
                    }
                )
                if not _missing_factor_ids(factor_ids, stats_by_factor):
                    break
            if not _missing_factor_ids(factor_ids, stats_by_factor):
                break
    return stats_by_factor


def _build_industry_cross_section_stats_query(table_name: str, value_column: str) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    return f"""
WITH industry_universe AS (
    SELECT sm.security_id AS security_id
    FROM security_master AS sm
    INNER JOIN issuers AS iss
        ON iss.issuer_id = sm.issuer_id
    WHERE sm.is_active
        AND sm.country = {{market_country:String}}
        AND iss.is_active
        AND iss.industry_schema = {{industry_schema:String}}
        AND iss.industry_group_code = {{industry_group_code:String}}
),
latest_factor_dates AS (
    SELECT
        f.factor_id AS factor_id,
        max(f.trade_date) AS latest_trade_date
    FROM {table_name} AS f
    INNER JOIN industry_universe AS u
        ON u.security_id = f.security_id
    PREWHERE
        f.trade_date <= {{as_of_date:Date}}
        AND f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE isFinite(f.{value_column})
    GROUP BY f.factor_id
),
latest_factors AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        argMax(f.{value_column}, f.updated_at) AS value
    FROM {table_name} AS f
    INNER JOIN latest_factor_dates AS d
        ON d.factor_id = f.factor_id
        AND d.latest_trade_date = f.trade_date
    INNER JOIN industry_universe AS u
        ON u.security_id = f.security_id
    PREWHERE
        f.trade_date >= {{as_of_date:Date}} - INTERVAL {CROSS_SECTION_LOOKBACK_DAYS} DAY
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
    WHERE isFinite(f.{value_column})
    GROUP BY
        f.security_id,
        f.factor_id
),
bounds AS (
    SELECT
        factor_id,
        quantileExact(0.10)(value) AS p10_value,
        quantileExact(0.90)(value) AS p90_value
    FROM latest_factors
    GROUP BY factor_id
)
SELECT
    lf.factor_id AS factor_id,
    avg(if(lf.value < b.p10_value, b.p10_value, if(lf.value > b.p90_value, b.p90_value, lf.value))) AS avg_value,
    quantileExact(0.5)(lf.value) AS median_value,
    quantileExact(0.25)(lf.value) AS p25_value,
    quantileExact(0.75)(lf.value) AS p75_value,
    count() AS n
FROM latest_factors AS lf
INNER JOIN bounds AS b
    ON b.factor_id = lf.factor_id
GROUP BY lf.factor_id
SETTINGS max_execution_time = {MARKET_STATS_MAX_EXECUTION_SECONDS}
""".strip()


def _load_history_points(
    client: Any,
    *,
    security_id: str,
    as_of_date: date,
    factor_ids: list[str],
    financial_basis: str,
    lookback_years: int,
    factor_source: str,
) -> list[ValuationHistoryPoint]:
    start_date = _shift_years(as_of_date, -lookback_years)
    source_order = [factor_source, *[table for table in FACTOR_TABLES if table != factor_source]]
    for table_name in source_order:
        for value_column in FACTOR_VALUE_COLUMNS:
            try:
                rows = _records(
                    client.query_df(
                        _build_history_points_query(table_name, value_column),
                        parameters={
                            "security_id": security_id,
                            "start_date": start_date.isoformat(),
                            "as_of_date": as_of_date.isoformat(),
                            "factor_ids": factor_ids,
                            "financial_basis": financial_basis,
                        },
                    )
                )
            except Exception:
                continue
            points = [
                ValuationHistoryPoint(
                    factor_id=str(row["factor_id"]),
                    period=_as_date(row["period"]),
                    value=_float_or_none(row.get("value")),
                    display_value=_format_factor_value(
                        _float_or_none(row.get("value")),
                        str(row["factor_id"]),
                    ),
                )
                for row in rows
                if row.get("factor_id") is not None and row.get("period") is not None
            ]
            if points:
                return points
    return []


def _build_history_points_query(table_name: str, value_column: str) -> str:
    _validate_factor_table(table_name)
    _validate_value_column(value_column)
    return f"""
SELECT
    factor_id,
    toDate(toStartOfMonth(trade_date)) AS period,
    argMax({value_column}, tuple(trade_date, updated_at)) AS value
FROM {table_name}
WHERE security_id = {{security_id:String}}
    AND trade_date >= {{start_date:Date}}
    AND trade_date <= {{as_of_date:Date}}
    AND financial_basis = {{financial_basis:String}}
    AND has({{factor_ids:Array(String)}}, factor_id)
    AND isFinite({value_column})
GROUP BY
    factor_id,
    period
ORDER BY
    period ASC,
    factor_id ASC
""".strip()


def _build_comparisons(
    *,
    factor_ids: list[str],
    current_by_factor: dict[str, FactorValue],
    history_stats: dict[str, FactorStats],
    market_stats: dict[str, FactorStats],
    industry_stats: dict[str, FactorStats],
    listing_market_stats: dict[str, FactorStats],
    metadata: ValuationStockMetadata,
) -> list[ValuationFactorComparison]:
    result = []
    for factor_id in factor_ids:
        current_value = current_by_factor.get(factor_id, FactorValue(factor_id, None)).value
        benchmarks = [
            ("historical_median", "Historical Median", history_stats.get(factor_id)),
            ("historical_avg", "Historical Avg", history_stats.get(factor_id)),
            ("market_avg", "Market Avg", market_stats.get(factor_id)),
            (
                "listing_market_avg",
                f"{_listing_market_label(metadata)} Avg",
                listing_market_stats.get(factor_id),
            ),
            ("industry_avg", "Industry Avg", industry_stats.get(factor_id)),
        ]
        comparisons = []
        for key, name, stats in benchmarks:
            benchmark_value = None
            if stats is not None:
                benchmark_value = _display_stat_value(
                    stats,
                    "avg"
                    if key in {"historical_avg", "market_avg", "listing_market_avg", "industry_avg"}
                    else "median",
                )
            comparisons.append(
                _benchmark_comparison(
                    factor_id=factor_id,
                    benchmark_key=key,
                    benchmark_name=name,
                    current_value=current_value,
                    benchmark_value=benchmark_value,
                )
            )
        result.append(
            ValuationFactorComparison(
                factor_id=factor_id,
                factor_name=_factor_name(factor_id),
                unit=_factor_unit(factor_id),
                direction=_factor_direction(factor_id),
                current=_metric(current_value, _factor_unit(factor_id)),
                comparisons=comparisons,
            )
        )
    return result


def _benchmark_comparison(
    *,
    factor_id: str,
    benchmark_key: str,
    benchmark_name: str,
    current_value: float | None,
    benchmark_value: float | None,
) -> ValuationBenchmarkComparison:
    diff_pct = _difference_pct(current_value, benchmark_value)
    signal, signal_label = _valuation_signal(factor_id, current_value, benchmark_value)
    return ValuationBenchmarkComparison(
        benchmark_key=benchmark_key,
        benchmark_name=benchmark_name,
        value=_metric(benchmark_value, _factor_unit(factor_id)),
        difference_pct=diff_pct,
        signal=signal,
        signal_label=signal_label,
    )


def _build_bands(
    *,
    factor_ids: list[str],
    current_by_factor: dict[str, FactorValue],
    current_price: float | None,
    history_stats: dict[str, FactorStats],
    market_stats: dict[str, FactorStats],
    industry_stats: dict[str, FactorStats],
    listing_market_stats: dict[str, FactorStats],
    buy_margin_pct: float,
    sell_margin_pct: float,
    band_basis: str,
) -> tuple[list[ValuationBand], list[str]]:
    bands = []
    warnings = []
    for factor_id in factor_ids:
        current_value = current_by_factor.get(factor_id, FactorValue(factor_id, None)).value
        target_value, target_source = _target_multiple(
            factor_id=factor_id,
            band_basis=band_basis,
            history_stats=history_stats,
            market_stats=market_stats,
            industry_stats=industry_stats,
            listing_market_stats=listing_market_stats,
        )
        fair_price = _fair_price_from_multiple(
            factor_id=factor_id,
            current_price=current_price,
            current_value=current_value,
            target_value=target_value,
        )
        warning = None
        if fair_price is None:
            warning = _band_warning(factor_id, current_price, current_value, target_value)
            if warning:
                warnings.append(warning)
        buy_price = fair_price * (1 - buy_margin_pct / 100) if fair_price is not None else None
        sell_price = fair_price * (1 + sell_margin_pct / 100) if fair_price is not None else None
        upside_pct = _difference_pct(fair_price, current_price)
        signal, signal_label = _valuation_signal(factor_id, current_value, target_value)
        bands.append(
            ValuationBand(
                factor_id=factor_id,
                factor_name=_factor_name(factor_id),
                current_multiple=_metric(current_value, _factor_unit(factor_id)),
                target_multiple=_metric(target_value, _factor_unit(factor_id)),
                target_source=target_source,
                fair_price=_metric(fair_price, "price"),
                buy_below_price=_metric(buy_price, "price"),
                sell_above_price=_metric(sell_price, "price"),
                upside_pct=upside_pct,
                signal=signal,
                signal_label=signal_label,
                warning=warning,
            )
        )
    return bands, list(dict.fromkeys(warnings))


def _build_central_band_summary(
    bands: list[ValuationBand],
    *,
    metadata: ValuationStockMetadata,
    buy_margin_pct: float,
    sell_margin_pct: float,
) -> ValuationBandSummary:
    profile_factor_ids = _central_band_factor_ids(metadata)
    valid_profile_bands = [
        band
        for band in bands
        if band.factor_id in profile_factor_ids and _float_or_none(band.fair_price.value) is not None
    ]
    if len(valid_profile_bands) < MIN_CENTRAL_BAND_FACTORS:
        fallback_factor_ids = tuple(
            factor_id for factor_id in DEFAULT_CENTRAL_BAND_FACTORS if factor_id not in {"peg", "eps_yoy_pct"}
        )
        valid_profile_bands = [
            band
            for band in bands
            if band.factor_id in fallback_factor_ids and _float_or_none(band.fair_price.value) is not None
        ]
        profile_factor_ids = fallback_factor_ids

    valid_prices = [float(band.fair_price.value) for band in valid_profile_bands]
    excluded_factor_ids = [
        band.factor_id
        for band in bands
        if band.factor_id not in profile_factor_ids or _float_or_none(band.fair_price.value) is None
    ]
    central_price = median(valid_prices) if valid_prices else None
    buy_price = (
        central_price * (1 - buy_margin_pct / 100)
        if central_price is not None
        else None
    )
    sell_price = (
        central_price * (1 + sell_margin_pct / 100)
        if central_price is not None
        else None
    )
    return ValuationBandSummary(
        fair_price=_metric(central_price, "price"),
        buy_below_price=_metric(buy_price, "price"),
        sell_above_price=_metric(sell_price, "price"),
        valid_factor_count=len(valid_prices),
        excluded_factor_ids=excluded_factor_ids,
    )


def _central_band_factor_ids(metadata: ValuationStockMetadata) -> tuple[str, ...]:
    industry_group_code = str(metadata.industry_group_code or "")
    if industry_group_code in INDUSTRY_GROUP_CENTRAL_BAND_FACTORS:
        return INDUSTRY_GROUP_CENTRAL_BAND_FACTORS[industry_group_code]
    sector_code = str(metadata.sector_code or "")
    if sector_code in SECTOR_CENTRAL_BAND_FACTORS:
        return SECTOR_CENTRAL_BAND_FACTORS[sector_code]
    return DEFAULT_CENTRAL_BAND_FACTORS


def _target_multiple(
    *,
    factor_id: str,
    band_basis: str,
    history_stats: dict[str, FactorStats],
    market_stats: dict[str, FactorStats],
    industry_stats: dict[str, FactorStats],
    listing_market_stats: dict[str, FactorStats],
) -> tuple[float | None, str]:
    candidates: list[tuple[float | None, str]] = [
        (_target_stat_value(history_stats.get(factor_id)), "historical"),
        (_target_stat_value(industry_stats.get(factor_id), prefer_avg=True), "industry"),
        (_target_stat_value(listing_market_stats.get(factor_id), prefer_avg=True), "listing_market"),
        (_target_stat_value(market_stats.get(factor_id), prefer_avg=True), "market"),
    ]
    if band_basis == "historical":
        return candidates[0]
    if band_basis == "industry":
        return candidates[1]
    if band_basis == "market":
        return candidates[3]
    if band_basis == "listing_market":
        return candidates[2]

    values = [value for value, _ in candidates if value is not None]
    if not values:
        return None, "blend"
    return median(values), "blend"


def _display_stat_value(stats: FactorStats | None, kind: str) -> float | None:
    if stats is None:
        return None
    if kind == "median":
        return _first_finite(stats.median_value, stats.avg_value, stats.p75_value, stats.p25_value)
    return _first_finite(stats.avg_value, stats.median_value, stats.p75_value, stats.p25_value)


def _target_stat_value(stats: FactorStats | None, *, prefer_avg: bool = False) -> float | None:
    if stats is None:
        return None
    if prefer_avg:
        return _first_positive(stats.avg_value, stats.median_value, stats.p75_value, stats.p25_value)
    return _first_positive(stats.median_value, stats.avg_value, stats.p75_value, stats.p25_value)


def _should_fallback_historical_stats(
    factor_id: str,
    stats: FactorStats,
    financial_basis: str,
) -> bool:
    if financial_basis not in {"ttm", "forward"}:
        return False
    if stats.n < MIN_HISTORICAL_STATS_POINTS:
        return True
    if factor_id not in PRICE_PROPORTIONAL_FACTORS and factor_id not in PRICE_INVERSE_FACTORS:
        return False
    median_value = _float_or_none(stats.median_value)
    if median_value is None or median_value <= 0:
        return True
    return _target_stat_value(stats) is None


def _first_finite(*values: float | None) -> float | None:
    for value in values:
        number = _float_or_none(value)
        if number is not None:
            return number
    return None


def _first_positive(*values: float | None) -> float | None:
    for value in values:
        number = _float_or_none(value)
        if number is not None and number > 0:
            return number
    return None


def _fair_price_from_multiple(
    *,
    factor_id: str,
    current_price: float | None,
    current_value: float | None,
    target_value: float | None,
) -> float | None:
    current_price = _float_or_none(current_price)
    current_value = _float_or_none(current_value)
    target_value = _float_or_none(target_value)
    if current_price is None or current_price <= 0:
        return None
    if current_value is None or target_value is None:
        return None
    if current_value <= 0 or target_value <= 0:
        return None
    if factor_id in PRICE_PROPORTIONAL_FACTORS:
        return current_price * target_value / current_value
    if factor_id in PRICE_INVERSE_FACTORS:
        return current_price * current_value / target_value
    return None


def _band_warning(
    factor_id: str,
    current_price: float | None,
    current_value: float | None,
    target_value: float | None,
) -> str | None:
    if current_price is None:
        return f"{_factor_name(factor_id)} band unavailable: missing current price"
    if factor_id not in PRICE_PROPORTIONAL_FACTORS and factor_id not in PRICE_INVERSE_FACTORS:
        return f"{_factor_name(factor_id)} band unavailable: factor is not price-derivable"
    if current_value is None:
        return f"{_factor_name(factor_id)} band unavailable: missing current multiple"
    if target_value is None:
        return f"{_factor_name(factor_id)} band unavailable: missing benchmark multiple"
    return f"{_factor_name(factor_id)} band unavailable: non-positive multiple"


def _valuation_signal(
    factor_id: str,
    current_value: float | None,
    benchmark_value: float | None,
) -> tuple[str, str]:
    diff_pct = _difference_pct(current_value, benchmark_value)
    if diff_pct is None or abs(diff_pct) < 5:
        return "neutral", "Neutral"
    if factor_id in LOWER_IS_BETTER_FACTORS:
        return ("discount", "Discount") if diff_pct < 0 else ("premium", "Premium")
    if factor_id in HIGHER_IS_BETTER_FACTORS:
        return ("discount", "Discount") if diff_pct > 0 else ("premium", "Premium")
    return "neutral", "Neutral"


def _difference_pct(value: float | None, base: float | None) -> float | None:
    value = _float_or_none(value)
    base = _float_or_none(base)
    if value is None or base is None or base == 0:
        return None
    return (value - base) / abs(base) * 100


def _to_factor_value(row: dict[str, Any]) -> FactorValue:
    return FactorValue(
        factor_id=str(row["factor_id"]),
        value=_float_or_none(row.get("value")),
        trade_date=_as_date_or_none(row.get("latest_trade_date") or row.get("trade_date")),
    )


def _stats_by_factor(rows: list[dict[str, Any]]) -> dict[str, FactorStats]:
    result = {}
    for row in rows:
        if row.get("factor_id") is None:
            continue
        result[str(row["factor_id"])] = FactorStats(
            avg_value=_float_or_none(row.get("avg_value")),
            median_value=_float_or_none(row.get("median_value")),
            p25_value=_float_or_none(row.get("p25_value")),
            p75_value=_float_or_none(row.get("p75_value")),
            n=int(_float_or_none(row.get("n")) or 0),
        )
    return result


def _history_points_from_bundle_rows(rows: list[dict[str, Any]]) -> list[ValuationHistoryPoint]:
    points: list[ValuationHistoryPoint] = []
    for row in rows:
        factor_id = row.get("factor_id")
        if factor_id is None:
            continue
        for point in row.get("history_points") or []:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            period = _as_date_or_none(point[0])
            if period is None:
                continue
            value = _float_or_none(point[1])
            points.append(
                ValuationHistoryPoint(
                    factor_id=str(factor_id),
                    period=period,
                    value=value,
                    display_value=_format_factor_value(value, str(factor_id)),
                )
            )
    return sorted(points, key=lambda item: (item.period, item.factor_id))


def _factor_sources() -> list[tuple[str, str]]:
    return [(table_name, value_column) for table_name in FACTOR_TABLES for value_column in FACTOR_VALUE_COLUMNS]


def _financial_basis_order(financial_basis: str) -> list[str]:
    if financial_basis == "forward":
        return ["forward", "ttm", "annual"]
    if financial_basis == "ttm":
        return ["ttm", "annual"]
    return [financial_basis]


def _missing_factor_ids(factor_ids: list[str], values_by_factor: dict[str, Any]) -> list[str]:
    return [factor_id for factor_id in factor_ids if factor_id not in values_by_factor]


def _validate_factor_table(table_name: str) -> None:
    if table_name not in FACTOR_TABLES:
        raise ValueError(f"unsupported factor table: {table_name}")


def _validate_value_column(value_column: str) -> None:
    if value_column not in FACTOR_VALUE_COLUMNS:
        raise ValueError(f"unsupported factor value column: {value_column}")


def _normalize_market(value: str) -> str:
    normalized = str(value or DEFAULT_MARKET).strip().lower()
    if normalized not in MARKETS:
        allowed = ", ".join(sorted(MARKETS))
        raise ValueError(f"market must be one of: {allowed}")
    return normalized


def _normalize_stock_code(stock_code: str, market: str) -> str:
    normalized = str(stock_code).strip().upper()
    if not _SYMBOL_RE.match(normalized):
        raise ValueError("stock_code must contain only letters, digits, dot, dash, or underscore")
    if market == "kr":
        return normalized.zfill(6)
    return normalized


def _security_id(stock_code: str, market: str) -> str:
    return f"SEC_{market.upper()}_{stock_code}"


def _normalize_factor_ids(factor_ids: list[str] | None) -> list[str]:
    if factor_ids is None:
        return list(SUPPORTED_MULTIPLE_FACTORS)
    normalized = []
    for factor_id in factor_ids:
        for raw_part in str(factor_id).split(","):
            raw_item = raw_part.strip().lower()
            item = FACTOR_ALIASES.get(raw_item, raw_item)
            if not item:
                continue
            if item not in SUPPORTED_MULTIPLE_FACTORS:
                allowed = ", ".join(SUPPORTED_MULTIPLE_FACTORS)
                raise ValueError(f"unsupported factor_id={raw_part}; allowed: {allowed}")
            normalized.append(item)
    if not normalized:
        raise ValueError("factor_ids must include at least one supported factor")
    return list(dict.fromkeys(normalized))


def _normalize_financial_basis(value: str) -> str:
    normalized = str(value or DEFAULT_FINANCIAL_BASIS).strip().lower()
    aliases = {"quarter": "quarterly", "ntm": "forward", "next_twelve_months": "forward"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_FINANCIAL_BASES:
        raise ValueError("financial_basis must be one of: annual, quarterly, ttm, forward")
    return normalized


def _normalize_lookback_years(value: int) -> int:
    normalized = int(value)
    if normalized < 1 or normalized > 10:
        raise ValueError("lookback_years must be between 1 and 10")
    return normalized


def _normalize_margin(value: float, field_name: str) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0 or normalized >= 100:
        raise ValueError(f"{field_name} must be greater than or equal to 0 and less than 100")
    return normalized


def _normalize_band_basis(value: str) -> str:
    normalized = str(value or DEFAULT_BAND_BASIS).strip().lower()
    if normalized not in BAND_BASES:
        allowed = ", ".join(sorted(BAND_BASES))
        raise ValueError(f"band_basis must be one of: {allowed}")
    return normalized


def _factor_name(factor_id: str) -> str:
    return FACTOR_LABELS.get(factor_id, factor_id.replace("_", " ").upper())


def _factor_unit(factor_id: str) -> str:
    return "percent" if factor_id in PERCENT_FACTORS else "times"


def _factor_direction(factor_id: str) -> str:
    if factor_id in LOWER_IS_BETTER_FACTORS:
        return "LOWER_BETTER"
    if factor_id in HIGHER_IS_BETTER_FACTORS:
        return "HIGHER_BETTER"
    return "NEUTRAL"


def _listing_market_label(metadata: ValuationStockMetadata) -> str:
    raw = str(metadata.primary_market_mic or "").strip()
    if not raw:
        return "Listing Market"
    normalized = raw.upper()
    aliases = {
        "STK": "KOSPI",
        "KS": "KOSPI",
        "KOSPI": "KOSPI",
        "KOSDAQ": "KOSDAQ",
        "KQ": "KOSDAQ",
        "KN": "KONEX",
        "KONEX": "KONEX",
        "XKRX": "KOSPI",
        "XKOS": "KOSDAQ",
    }
    return aliases.get(normalized, raw)


def _metric(value: float | None, unit: str) -> ValuationMetric:
    number = _float_or_none(value)
    return ValuationMetric(
        value=number,
        display_value=_format_value(number, unit),
    )


def _format_factor_value(value: float | None, factor_id: str) -> str:
    return _format_value(value, _factor_unit(factor_id))


def _format_value(value: float | None, unit: str) -> str:
    number = _float_or_none(value)
    if number is None:
        return "N/A"
    if unit == "percent":
        return f"{number:.2f}%"
    if unit == "times":
        return f"{number:.2f}x"
    if unit == "price":
        return f"{number:,.2f}"
    return f"{number:,.2f}"


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if value == "":
        return None
    return str(value)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return _as_date(value)
    except (TypeError, ValueError):
        return None


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)
