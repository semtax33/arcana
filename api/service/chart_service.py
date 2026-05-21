from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.chart import (
    RecentStockChartRow,
    StockChartMetadata,
    StockChartPoint,
    StockChartResponse,
)
from api.service.dto import ChartRange


PRICE_TABLE = "price_daily"
FACTOR_TABLE = "fact_daily_factors"
RECENT_ROW_COUNT = 30
TECHNICAL_LOOKBACK_DAYS = 280

RANGE_MONTHS: dict[ChartRange, int | None] = {
    "1M": 1,
    "3M": 3,
    "6M": 6,
    "1Y": 12,
    "5Y": 60,
    "MAX": None,
}

FACTOR_ID_GROUPS = {
    "moving_average": ["na_5", "na_20", "na_50", "na_150", "na_200"],
    "monthly_return": ["ret_1m"],
    "rsi": ["rsi", "rsi_14"],
    "bollinger_band": [
        "bollinger_band",
        "bollinger_signal",
        "bb_signal",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "bollinger_upper",
        "bollinger_middle",
        "bollinger_lower",
    ],
    "trend": ["trend", "trend_signal", "price_trend", "ma_trend"],
    "macd": ["macd", "macd_signal", "macd_hist"],
}

_STOCK_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,12}$")


class StockChartNotFoundError(ValueError):
    pass


class ChartService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst

    def get_chart(self, stock_code: str, chart_range: ChartRange = "1Y") -> StockChartResponse:
        normalized_stock_code = _normalize_stock_code(stock_code)
        security_id = f"SEC_KR_{normalized_stock_code}"
        to_date = self._today_factory()
        from_date = _range_start_date(to_date, chart_range)
        query_start_date = (
            from_date - timedelta(days=TECHNICAL_LOOKBACK_DAYS) if from_date is not None else None
        )

        client = self._client_factory()
        try:
            price_rows = _records(
                client.query_df(
                    _build_price_query(query_start_date),
                    parameters={
                        "security_id": security_id,
                        "as_of_date": to_date.isoformat(),
                        **(
                            {"start_date": query_start_date.isoformat()}
                            if query_start_date is not None
                            else {}
                        ),
                    },
                )
            )
            if not price_rows:
                raise StockChartNotFoundError(f"price data not found for stock_code={stock_code}")

            metadata = self._load_metadata(client, normalized_stock_code, security_id, price_rows)
            factor_rows, factor_source = self._load_factor_rows(
                client,
                security_id=security_id,
                to_date=to_date,
                start_date=query_start_date,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        factor_by_date = _factor_by_date(factor_rows)
        enriched_rows = _enrich_price_rows(price_rows, factor_by_date)
        visible_rows = [
            row for row in enriched_rows if from_date is None or row["trade_date"] >= from_date
        ]
        if not visible_rows:
            raise StockChartNotFoundError(f"price data not found for stock_code={stock_code}")

        return StockChartResponse(
            stock=metadata,
            range=chart_range,
            from_date=visible_rows[0]["trade_date"],
            to_date=visible_rows[-1]["trade_date"],
            chart=[_to_chart_point(row) for row in visible_rows],
            recent=[_to_recent_row(row) for row in visible_rows[-RECENT_ROW_COUNT:]][::-1],
            factor_source=factor_source,
            factor_ids=FACTOR_ID_GROUPS,
        )

    def _load_metadata(
        self,
        client: Any,
        stock_code: str,
        security_id: str,
        price_rows: list[dict[str, Any]],
    ) -> StockChartMetadata:
        currency = _optional_str(price_rows[-1].get("currency")) or "KRW"
        try:
            rows = _records(
                client.query_df(
                    """
SELECT
    sm.security_id AS security_id,
    any(id.id_value) AS ticker,
    any(iss.legal_name_ko) AS stock_name,
    any(iss.domicile_country) AS country
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
        return StockChartMetadata(
            stock_code=_optional_str(row.get("ticker")) or stock_code,
            security_id=security_id,
            stock_name=_optional_str(row.get("stock_name")),
            country=_optional_str(row.get("country")) or "KR",
            currency=currency,
        )

    def _load_factor_rows(
        self,
        client: Any,
        *,
        security_id: str,
        to_date: date,
        start_date: date | None,
    ) -> tuple[list[dict[str, Any]], str]:
        parameters = {
            "security_id": security_id,
            "as_of_date": to_date.isoformat(),
            "factor_ids": _all_factor_ids(),
            **({"start_date": start_date.isoformat()} if start_date is not None else {}),
        }
        for table_name in [FACTOR_TABLE, "fact_daily_factor"]:
            try:
                return (
                    _records(
                        client.query_df(
                            _build_factor_query(start_date, factor_table=table_name),
                            parameters=parameters,
                        )
                    ),
                    table_name,
                )
            except Exception:
                continue
        return [], FACTOR_TABLE


def _build_price_query(start_date: date | None) -> str:
    start_filter = "\n    AND trade_date >= {start_date:Date}" if start_date is not None else ""
    return f"""
SELECT
    trade_date,
    open,
    high,
    low,
    close,
    volume,
    currency
FROM {PRICE_TABLE}
WHERE security_id = {{security_id:String}}
    AND trade_date <= {{as_of_date:Date}}{start_filter}
ORDER BY trade_date ASC
""".strip()


def _build_factor_query(start_date: date | None, *, factor_table: str = FACTOR_TABLE) -> str:
    start_filter = "\n    AND trade_date >= {start_date:Date}" if start_date is not None else ""
    return f"""
SELECT
    trade_date,
    factor_id,
    argMax(factor_value, updated_at) AS factor_value
FROM {factor_table}
WHERE security_id = {{security_id:String}}
    AND trade_date <= {{as_of_date:Date}}{start_filter}
    AND has({{factor_ids:Array(String)}}, factor_id)
    AND isFinite(factor_value)
GROUP BY trade_date, factor_id
ORDER BY trade_date ASC, factor_id ASC
""".strip()


def _enrich_price_rows(
    price_rows: list[dict[str, Any]],
    factor_by_date: dict[date, dict[str, float]],
) -> list[dict[str, Any]]:
    enriched = []
    previous_close: float | None = None
    closes: list[float] = []
    volumes: list[float] = []

    for row in sorted(price_rows, key=lambda item: _as_date(item["trade_date"])):
        trade_date = _as_date(row["trade_date"])
        close = _float_or_none(row.get("close"))
        volume = _float_or_none(row.get("volume"))
        factors = factor_by_date.get(trade_date, {})
        closes.append(close if close is not None else math.nan)
        volumes.append(volume if volume is not None else math.nan)

        enriched_row = {
            "trade_date": trade_date,
            "open": _required_float(row.get("open"), "open"),
            "high": _required_float(row.get("high"), "high"),
            "low": _required_float(row.get("low"), "low"),
            "close": _required_float(row.get("close"), "close"),
            "volume": _required_float(row.get("volume"), "volume"),
            "currency": row.get("currency"),
            "ma5": _factor_or_average(factors, "na_5", closes, 5),
            "ma20": _factor_or_average(factors, "na_20", closes, 20),
            "ma50": _factor_or_average(factors, "na_50", closes, 50),
            "ma150": _factor_or_average(factors, "na_150", closes, 150),
            "ma200": _factor_or_average(factors, "na_200", closes, 200),
            "monthly_return": _monthly_return(factors, closes),
            "continuity": _continuity(row.get("open"), previous_close),
            "volume_signal": _volume_signal(volumes),
            "rsi": _rsi_value(factors),
            "bollinger_band": _bollinger_value(factors, close),
            "trend": _first_present(factors, FACTOR_ID_GROUPS["trend"]),
            "macd": _macd_value(factors),
        }
        previous_close = close
        enriched.append(enriched_row)

    return enriched


def _to_chart_point(row: dict[str, Any]) -> StockChartPoint:
    return StockChartPoint(
        time=row["trade_date"].isoformat(),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        ma5=_clean_number(row.get("ma5")),
        ma20=_clean_number(row.get("ma20")),
        ma50=_clean_number(row.get("ma50")),
        ma150=_clean_number(row.get("ma150")),
        ma200=_clean_number(row.get("ma200")),
    )


def _to_recent_row(row: dict[str, Any]) -> RecentStockChartRow:
    return RecentStockChartRow(
        date=row["trade_date"].isoformat(),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        monthly_return=_clean_number(row.get("monthly_return")),
        continuity=row.get("continuity"),
        volume_signal=row.get("volume_signal"),
        rsi=_clean_nested(row.get("rsi")),
        bollinger_band=_clean_nested(row.get("bollinger_band")),
        trend=_clean_nested(row.get("trend")),
        macd=_clean_nested(row.get("macd")),
    )


def _factor_by_date(rows: list[dict[str, Any]]) -> dict[date, dict[str, float]]:
    result: dict[date, dict[str, float]] = defaultdict(dict)
    for row in rows:
        value = _float_or_none(row.get("factor_value"))
        if value is None:
            continue
        result[_as_date(row["trade_date"])][str(row["factor_id"])] = value
    return dict(result)


def _normalize_stock_code(stock_code: str) -> str:
    normalized = str(stock_code).strip().upper()
    if not _STOCK_CODE_RE.match(normalized):
        raise ValueError("stock_code must contain only letters and digits")
    return normalized.zfill(6)


def _range_start_date(to_date: date, chart_range: ChartRange) -> date | None:
    months = RANGE_MONTHS.get(chart_range)
    if chart_range not in RANGE_MONTHS:
        raise ValueError("range must be one of: 1M, 3M, 6M, 1Y, 5Y, MAX")
    if months is None:
        return None
    return _subtract_months(to_date, months)


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


def _all_factor_ids() -> list[str]:
    return sorted({factor_id for ids in FACTOR_ID_GROUPS.values() for factor_id in ids})


def _factor_or_average(
    factors: dict[str, float],
    factor_id: str,
    values: list[float],
    window: int,
) -> float | None:
    if factor_id in factors:
        return factors[factor_id]
    valid_values = [value for value in values[-window:] if math.isfinite(value)]
    if not valid_values:
        return None
    return sum(valid_values) / len(valid_values)


def _monthly_return(factors: dict[str, float], closes: list[float]) -> float | None:
    factor_value = factors.get("ret_1m")
    if factor_value is not None:
        return factor_value * 100 if abs(factor_value) <= 5 else factor_value
    if len(closes) <= 21:
        return None
    current = closes[-1]
    past = closes[-22]
    if not math.isfinite(current) or not math.isfinite(past) or past == 0:
        return None
    return (current / past - 1) * 100


def _continuity(open_value: Any, previous_close: float | None) -> str | None:
    open_number = _float_or_none(open_value)
    if open_number is None or previous_close is None or previous_close == 0:
        return None
    gap = open_number / previous_close - 1
    if abs(gap) < 0.03:
        return "Normal_Gap"
    if gap > 0:
        return "Up_Gap"
    return "Down_Gap"


def _volume_signal(volumes: list[float]) -> str | None:
    current = volumes[-1] if volumes else math.nan
    if not math.isfinite(current):
        return None
    history = [value for value in volumes[-20:] if math.isfinite(value)]
    if not history:
        return None
    average = sum(history) / len(history)
    if average == 0:
        return None
    ratio = current / average
    if ratio >= 2:
        return "High_Volume"
    if ratio >= 1.2:
        return "Above_Average_Volume"
    if ratio <= 0.8:
        return "Below_Average_Volume"
    return "Normal_Volume"


def _rsi_value(factors: dict[str, float]) -> str | float | None:
    value = _first_present(factors, FACTOR_ID_GROUPS["rsi"])
    if value is None:
        return None
    if value >= 70:
        return "Overbought"
    if value <= 30:
        return "Oversold"
    return "Neutral"


def _bollinger_value(factors: dict[str, float], close: float | None) -> float | dict[str, Any] | None:
    direct = _first_present(factors, ["bollinger_band", "bollinger_signal", "bb_signal"])
    if direct is not None:
        return direct

    upper = _first_present(factors, ["bb_upper", "bollinger_upper"])
    middle = _first_present(factors, ["bb_middle", "bollinger_middle"])
    lower = _first_present(factors, ["bb_lower", "bollinger_lower"])
    if upper is None and middle is None and lower is None:
        return None

    signal = None
    if close is not None:
        if upper is not None and close > upper:
            signal = "Above_Upper_Band"
        elif lower is not None and close < lower:
            signal = "Below_Lower_Band"
        elif upper is not None and close > upper * 0.98:
            signal = "Near_Upper_Band"
        elif lower is not None and close < lower * 1.02:
            signal = "Near_Lower_Band"
        else:
            signal = "Neutral"
    return {"upper": upper, "middle": middle, "lower": lower, "signal": signal}


def _macd_value(factors: dict[str, float]) -> float | dict[str, Any] | None:
    macd = factors.get("macd")
    signal = factors.get("macd_signal")
    hist = factors.get("macd_hist")
    if signal is None and hist is None:
        return macd
    return {"macd": macd, "signal": signal, "histogram": hist}


def _first_present(factors: dict[str, float], factor_ids: list[str]) -> float | None:
    for factor_id in factor_ids:
        if factor_id in factors:
            return factors[factor_id]
    return None


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _required_float(value: Any, name: str) -> float:
    result = _float_or_none(value)
    if result is None:
        raise ValueError(f"{name} contains a non-numeric value")
    return result


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


def _clean_number(value: Any) -> float | None:
    return _float_or_none(value)


def _clean_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean_nested(item) for key, item in value.items()}
    return _clean_number(value) if isinstance(value, (int, float)) else value
