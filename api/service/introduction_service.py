from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import re
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.introduction import (
    BusinessAreaBadge,
    CompanyIntroduction,
    StockIntroductionMetadata,
    StockIntroductionMetrics,
    StockIntroductionResponse,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GICS_RULES_PATH = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "gics_rules.yaml"
PRICE_TABLE = "price_daily"
FACTOR_TABLE = "fact_daily_factor"
FACTOR_TABLES = ["fact_daily_factor", "fact_daily_factors"]
METRIC_FACTOR_IDS = [
    "per",
    "PER",
    "mcap_mil",
    "MCAP_MIL",
    "market_cap",
    "MARKET_CAP",
    "dividend_yield",
    "DIVIDEND_YIELD",
    "DIV_YIELD",
    "sharehold_div_yield",
    "SHAREHOLD_DIV_YIELD",
]
INTRODUCTION_COLUMN_CANDIDATES = [
    "company_description",
    "description",
    "introduction",
    "intro",
    "overview",
    "business_summary",
    "summary",
    "profile",
]

_STOCK_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,12}$")


class StockIntroductionNotFoundError(ValueError):
    pass


class IntroductionService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
        gics_rules_path: Path = DEFAULT_GICS_RULES_PATH,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst
        self._gics_rules_path = gics_rules_path

    def get_introduction(self, stock_code: str) -> StockIntroductionResponse:
        normalized_stock_code = _normalize_stock_code(stock_code)
        security_id = f"SEC_KR_{normalized_stock_code}"
        as_of_date = self._today_factory()
        start_date = as_of_date - timedelta(weeks=52)

        client = self._client_factory()
        try:
            metadata_row = self._load_metadata(client, security_id)
            metric_factor_rows, factor_source = self._load_metric_factor_rows(
                client,
                security_id=security_id,
                as_of_date=as_of_date,
            )
            price_row = self._load_52_week_price_row(
                client,
                security_id=security_id,
                as_of_date=as_of_date,
                start_date=start_date,
            )
            company_description = self._load_company_description(
                client,
                _optional_str(metadata_row.get("issuer_id")),
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        if (
            not metadata_row
            and not metric_factor_rows
            and not price_row
        ):
            raise StockIntroductionNotFoundError(
                f"introduction data not found for stock_code={stock_code}"
            )

        sector_names = self._load_sector_names()
        metadata = _to_metadata(
            metadata_row,
            stock_code=normalized_stock_code,
            security_id=security_id,
            currency=(
                _optional_str(price_row.get("currency"))
                or _factor_currency(metric_factor_rows)
                or "KRW"
            ),
        )
        return StockIntroductionResponse(
            stock=metadata,
            metrics=_to_metrics(
                metric_factor_rows=metric_factor_rows,
                price_row=price_row,
            ),
            company=CompanyIntroduction(description=company_description),
            business_areas=_to_business_areas(metadata_row, sector_names),
            factor_source=factor_source,
        )

    def _load_metadata(self, client: Any, security_id: str) -> dict[str, Any]:
        rows = _records(
            client.query_df(
                """
SELECT
    sm.security_id AS security_id,
    any(sm.issuer_id) AS issuer_id,
    any(id.id_value) AS ticker,
    any(iss.legal_name_ko) AS stock_name,
    any(iss.legal_name_en) AS stock_name_en,
    any(iss.domicile_country) AS country,
    any(iss.industry_schema) AS sector_schema,
    any(iss.industry_code) AS sector_code
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
        return rows[0] if rows else {}

    def _load_metric_factor_rows(
        self,
        client: Any,
        *,
        security_id: str,
        as_of_date: date,
    ) -> tuple[list[dict[str, Any]], str]:
        last_source = FACTOR_TABLE
        for table_name in FACTOR_TABLES:
            last_source = table_name
            rows = self._query_metric_factor_rows(
                client,
                security_id=security_id,
                as_of_date=as_of_date,
                table_name=table_name,
            )
            if rows:
                return rows, table_name

        return [], last_source

    def _query_metric_factor_rows(
        self,
        client: Any,
        *,
        security_id: str,
        as_of_date: date,
        table_name: str,
    ) -> list[dict[str, Any]]:
        for value_column in ["factor_value", "value"]:
            try:
                rows = _records(
                    client.query_df(
                        _build_metric_factor_query(table_name, value_column=value_column),
                        parameters={
                            "security_id": security_id,
                            "as_of_date": as_of_date.isoformat(),
                            "factor_ids": METRIC_FACTOR_IDS,
                        },
                    )
                )
            except Exception:
                continue

            rows = [row for row in rows if _float_or_none(row.get("factor_value")) is not None]
            if rows:
                return rows
        return []

    def _load_52_week_price_row(
        self,
        client: Any,
        *,
        security_id: str,
        as_of_date: date,
        start_date: date,
    ) -> dict[str, Any]:
        rows = _records(
            client.query_df(
                """
SELECT
    count() AS row_count,
    max(high) AS high_52w,
    min(low) AS low_52w,
    argMax(close, trade_date) AS latest_close,
    max(trade_date) AS latest_trade_date,
    any(currency) AS currency
FROM price_daily
WHERE security_id = {security_id:String}
    AND trade_date >= {start_date:Date}
    AND trade_date <= {as_of_date:Date}
""".strip(),
                parameters={
                    "security_id": security_id,
                    "start_date": start_date.isoformat(),
                    "as_of_date": as_of_date.isoformat(),
                },
            )
        )
        row = rows[0] if rows else {}
        row_count = _float_or_none(row.get("row_count"))
        if row_count is not None and row_count <= 0:
            return {}
        return row if _float_or_none(row.get("high_52w")) is not None else {}

    def _load_company_description(self, client: Any, issuer_id: str | None) -> str:
        if not issuer_id:
            return ""
        column_name = self._find_introduction_column(client)
        if column_name is None:
            return ""
        rows = _records(
            client.query_df(
                f"""
SELECT
    any({column_name}) AS description
FROM issuers
WHERE issuer_id = {{issuer_id:String}}
""".strip(),
                parameters={"issuer_id": issuer_id},
            )
        )
        if not rows:
            return ""
        return _optional_str(rows[0].get("description")) or ""

    def _find_introduction_column(self, client: Any) -> str | None:
        try:
            rows = _records(
                client.query_df(
                    """
SELECT name
FROM system.columns
WHERE database = currentDatabase()
    AND table = 'issuers'
    AND has({columns:Array(String)}, name)
""".strip(),
                    parameters={"columns": INTRODUCTION_COLUMN_CANDIDATES},
                )
            )
        except Exception:
            return None

        existing_columns = {str(row["name"]) for row in rows if row.get("name") is not None}
        for column_name in INTRODUCTION_COLUMN_CANDIDATES:
            if column_name in existing_columns:
                return column_name
        return None

    def _load_sector_names(self) -> dict[str, str]:
        try:
            with self._gics_rules_path.open("r", encoding="utf-8") as file:
                import yaml

                config = yaml.safe_load(file) or {}
        except ModuleNotFoundError:
            return _load_sector_names_without_yaml(self._gics_rules_path)
        except FileNotFoundError:
            return {}
        sectors = config.get("sectors", {})
        return {str(code): str(name) for code, name in sectors.items()}


def _build_metric_factor_query(table_name: str, *, value_column: str = "factor_value") -> str:
    if table_name not in FACTOR_TABLES:
        raise ValueError(f"unsupported factor table: {table_name}")
    if value_column not in {"factor_value", "value"}:
        raise ValueError(f"unsupported factor value column: {value_column}")

    return f"""
SELECT
    factor_id,
    argMax({value_column}, tuple(trade_date, updated_at)) AS factor_value,
    max(trade_date) AS trade_date,
    argMax(currency, tuple(trade_date, updated_at)) AS currency
FROM {table_name}
WHERE security_id = {{security_id:String}}
    AND trade_date <= {{as_of_date:Date}}
    AND has({{factor_ids:Array(String)}}, factor_id)
    AND isFinite({value_column})
GROUP BY factor_id
""".strip()


def _to_metadata(
    row: dict[str, Any],
    *,
    stock_code: str,
    security_id: str,
    currency: str | None,
) -> StockIntroductionMetadata:
    return StockIntroductionMetadata(
        stock_code=_optional_str(row.get("ticker")) or stock_code,
        security_id=security_id,
        stock_name=_optional_str(row.get("stock_name")),
        stock_name_en=_optional_str(row.get("stock_name_en")),
        country=_optional_str(row.get("country")) or "KR",
        currency=currency or "KRW",
    )


def _to_metrics(
    *,
    metric_factor_rows: list[dict[str, Any]],
    price_row: dict[str, Any],
) -> StockIntroductionMetrics:
    factors = _factor_values(metric_factor_rows)
    high_52w = _float_or_none(price_row.get("high_52w"))
    low_52w = _float_or_none(price_row.get("low_52w"))
    range_pct = None
    if high_52w is not None and low_52w is not None and low_52w > 0:
        range_pct = (high_52w - low_52w) / low_52w * 100

    return StockIntroductionMetrics(
        market_cap=_market_cap_from_factors(factors),
        trailing_per=factors.get("per"),
        dividend_yield=_first_factor_value(factors, ["dividend_yield", "sharehold_div_yield"]),
        fifty_two_week_range_pct=range_pct,
        fifty_two_week_high=high_52w,
        fifty_two_week_low=low_52w,
        latest_close=_float_or_none(price_row.get("latest_close")),
        latest_trade_date=_as_date_or_none(price_row.get("latest_trade_date")),
    )


def _to_business_areas(
    row: dict[str, Any],
    sector_names: dict[str, str],
) -> list[BusinessAreaBadge]:
    sector_code = _optional_str(row.get("sector_code"))
    if not sector_code or sector_code == "UNMAPPED":
        return []
    sector_schema = _optional_str(row.get("sector_schema")) or "GICS"
    return [
        BusinessAreaBadge(
            sector_code=sector_code,
            sector_name=sector_names.get(sector_code, sector_code),
            schema=sector_schema,
        )
    ]


def _factor_values(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        factor_id = _canonical_metric_factor_id(_optional_str(row.get("factor_id")))
        value = _float_or_none(row.get("factor_value"))
        if factor_id and value is not None:
            result[factor_id] = value
    return result


def _canonical_metric_factor_id(factor_id: str | None) -> str | None:
    if factor_id is None:
        return None
    normalized = factor_id.strip().lower()
    aliases = {
        "per": "per",
        "mcap_mil": "mcap_mil",
        "market_cap": "market_cap",
        "div_yield": "dividend_yield",
        "dividend_yield": "dividend_yield",
        "sharehold_div_yield": "sharehold_div_yield",
    }
    return aliases.get(normalized, normalized)


def _market_cap_from_factors(factors: dict[str, float]) -> float | None:
    market_cap = factors.get("market_cap")
    if market_cap is not None:
        return market_cap

    mcap_mil = factors.get("mcap_mil")
    if mcap_mil is None:
        return None
    return mcap_mil * 1_000_000


def _first_factor_value(factors: dict[str, float], factor_ids: list[str]) -> float | None:
    for factor_id in factor_ids:
        if factor_id in factors:
            return factors[factor_id]
    return None


def _factor_currency(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        currency = _optional_str(row.get("currency"))
        if currency:
            return currency
    return None


def _load_sector_names_without_yaml(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}

    sectors: dict[str, str] = {}
    in_sectors = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "sectors:":
            in_sectors = True
            continue
        if in_sectors and not line.startswith((" ", "\t")):
            break
        if not in_sectors or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        sector_code = key.strip().strip("'\"")
        sector_name = value.strip().strip("'\"")
        if sector_code and sector_name:
            sectors[sector_code] = sector_name
    return sectors


def _normalize_stock_code(stock_code: str) -> str:
    normalized = str(stock_code).strip().upper()
    if not _STOCK_CODE_RE.match(normalized):
        raise ValueError("stock_code must contain only letters and digits")
    return normalized.zfill(6)


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


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


def _as_date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
