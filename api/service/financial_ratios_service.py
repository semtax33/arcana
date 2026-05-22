from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.financial_ratios import (
    FinancialRatioGroup,
    FinancialRatioRow,
    FinancialRatiosResponse,
    FinancialRatioSection,
)
from api.model.financials import (
    FinancialAccountStatistics,
    FinancialChartPoint,
    FinancialPeriodColumn,
    FinancialStatementCell,
    FinancialStatementMetadata,
)


FACTOR_TABLES = ("fact_daily_factor", "fact_daily_factors")
SOURCE_TABLES = ["fact_daily_factor", "stock_dividends", "stock_shares", "price_daily"]
PERIOD_LIMITS = {
    "annual": 10,
    "quarter": 20,
}
FINANCIAL_BASIS_BY_PERIOD = {
    "annual": ["annual"],
    "quarter": ["quarterly", "quarter"],
}

STATEMENT_LABELS = {
    "IS": ("\uc190\uc775\uacc4\uc0b0\uc11c", "Income Statement"),
    "BS": ("\uc7ac\ubb34\uc0c1\ud0dc\ud45c", "Balance Sheet"),
    "CF": ("\ud604\uae08\ud750\ub984\ud45c", "Cash Flow Statement"),
}

RATIO_GROUP_DEFINITIONS = {
    "IS": [
        (
            "profitability",
            "\uc218\uc775\uc131 \ube44\uc728",
            "Profitability Ratios",
            [
                "gpm",
                "opm",
                "ebitda_margin",
                "npm",
                "tax_rate",
                "roe",
                "roa",
                "iroe",
                "roic_financial",
                "roic_operational",
                "roce",
            ],
        ),
        (
            "growth",
            "\uc131\uc7a5\uc131 \ube44\uc728",
            "Growth Ratios",
            [
                "sales_yoy_pct",
                "op_yoy_pct",
                "eps_yoy_pct",
                "sales_change_mil",
                "op_change_mil",
                "rdsr_pct",
            ],
        ),
        (
            "per_share_valuation",
            "\uc8fc\ub2f9/\uac00\uce58 \uc9c0\ud45c",
            "Per Share and Valuation",
            [
                "eps",
                "sps",
                "per",
                "psr",
                "epr",
            ],
        ),
    ],
    "BS": [
        (
            "liquidity",
            "\uc720\ub3d9\uc131 \ube44\uc728",
            "Liquidity Ratios",
            [
                "current_ratio",
                "cash_to_debt",
                "working_capital_turnover",
                "wc_to_sales_pct",
            ],
        ),
        (
            "leverage",
            "\ub808\ubc84\ub9ac\uc9c0 \ube44\uc728",
            "Leverage Ratios",
            [
                "debt_to_equity",
                "debt_ratio",
                "net_debt_to_ebitda",
                "net_debt_to_ocf",
                "icr_times",
                "interest_coverage",
                "total_interest_coverage",
                "altman_z_score",
                "beneish_m_score",
                "f_score",
            ],
        ),
        (
            "activity",
            "\ud65c\ub3d9\uc131 \ube44\uc728",
            "Activity Ratios",
            [
                "asset_turnover",
                "receivables_turnover",
                "inventory_turnover",
                "inv_days",
                "ar_days",
                "ap_days",
                "ccc",
                "asset_yoy_pct",
            ],
        ),
        (
            "book_valuation",
            "\uc790\ubcf8/\uc2dc\uc7a5\uac00\uce58 \uc9c0\ud45c",
            "Book and Market Value",
            [
                "bps",
                "pbr",
                "bpr",
                "mcap_mil",
            ],
        ),
    ],
    "CF": [
        (
            "cash_flow",
            "\ud604\uae08\ud750\ub984 \ube44\uc728",
            "Cash Flow Ratios",
            [
                "cfo_yoy_pct",
                "fcf_yoy_pct",
                "ffo_yoy_pct",
                "fc_to_ndr",
                "pcr",
                "cpr",
                "fcfpr",
            ],
        ),
        (
            "shareholder",
            "\ubc30\ub2f9/\uc8fc\uc8fc\ud658\uc6d0 \ube44\uc728",
            "Dividend and Shareholder Return",
            [
                "dividend_yield",
                "payout_ratio",
                "sharehold_div_yield",
                "sharehold_net_buyback_yield",
                "sharehold_return",
                "tdpr",
                "dvpsp",
                "dvpsx",
            ],
        ),
    ],
}

_STOCK_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,12}$")
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z_]+$")


class FinancialRatiosNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class FactorCatalogItem:
    factor_id: str
    factor_name: str
    unit: str | None = None
    value_direction: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class RawRatioRow:
    factor_id: str
    fiscal_year: int
    fiscal_month: int
    period_end_date: date
    value: float | None
    currency: str | None = None


class FinancialRatiosService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst

    def get_ratios(self, stock_code: str, period: str = "annual") -> FinancialRatiosResponse:
        normalized_period = _normalize_period(period)
        normalized_stock_code = _normalize_stock_code(stock_code)
        security_id = f"SEC_KR_{normalized_stock_code}"
        as_of_date = self._today_factory()
        start_year = as_of_date.year - (12 if normalized_period == "annual" else 7)
        factor_ids = _all_ratio_factor_ids()

        client = self._client_factory()
        try:
            rows, source = self._load_ratio_rows(
                client,
                stock_code=normalized_stock_code,
                security_id=security_id,
                as_of_date=as_of_date,
                start_year=start_year,
                period=normalized_period,
                factor_ids=factor_ids,
            )
            metadata = _load_metadata(client, normalized_stock_code, security_id)
            catalog = _load_factor_catalog(client, factor_ids)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        if not rows:
            raise FinancialRatiosNotFoundError(
                f"financial ratio data not found for stock_code={stock_code}"
            )

        columns = _select_visible_columns(rows, normalized_period)
        sections = _build_sections(rows=rows, columns=columns, catalog=catalog)
        sections = [section for section in sections if any(group.ratios for group in section.groups)]
        if not columns or not sections:
            raise FinancialRatiosNotFoundError(
                f"financial ratio data not found for stock_code={stock_code}"
            )

        return FinancialRatiosResponse(
            stock=metadata,
            period=normalized_period,
            financial_basis=FINANCIAL_BASIS_BY_PERIOD[normalized_period][0],
            columns=columns,
            sections=sections,
            source=source,
            auxiliary_sources=SOURCE_TABLES[1:],
        )

    def _load_ratio_rows(
        self,
        client: Any,
        *,
        stock_code: str,
        security_id: str,
        as_of_date: date,
        start_year: int,
        period: str,
        factor_ids: list[str],
    ) -> tuple[list[RawRatioRow], str]:
        last_source = FACTOR_TABLES[0]
        for table_name in FACTOR_TABLES:
            last_source = table_name
            try:
                columns = _load_table_columns(client, table_name)
                query = _build_ratio_query(columns, table_name=table_name)
                rows = _records(
                    client.query_df(
                        query,
                        parameters={
                            "stock_code": stock_code,
                            "security_id": security_id,
                            "as_of_date": as_of_date.isoformat(),
                            "start_year": start_year,
                            "factor_ids": factor_ids,
                            "financial_basis": FINANCIAL_BASIS_BY_PERIOD[period],
                        },
                    )
                )
            except Exception:
                continue

            result = [_to_raw_ratio_row(row) for row in rows]
            result = [row for row in result if row.value is not None]
            if result:
                return result, table_name

        return [], last_source


def _build_ratio_query(columns: set[str], *, table_name: str) -> str:
    if table_name not in FACTOR_TABLES:
        raise ValueError(f"unsupported factor table: {table_name}")

    column_map = _ratio_column_map(columns)
    required_keys = ["factor_id", "trade_date", "value"]
    missing = [key for key in required_keys if column_map[key] is None]
    if missing:
        raise ValueError(f"{table_name} missing required columns: {', '.join(missing)}")

    factor_column = _q(column_map["factor_id"])
    trade_date_column = _q(column_map["trade_date"])
    value_column = _q(column_map["value"])
    period_expr = (
        f"toDate({_q(column_map['financial_period'])})"
        if column_map["financial_period"] is not None
        else f"toDate({trade_date_column})"
    )
    year_expr = (
        f"toInt32({_q(column_map['fiscal_year'])})"
        if column_map["fiscal_year"] is not None
        else f"toInt32(toYear({period_expr}))"
    )
    month_expr = f"toInt32(toMonth({period_expr}))"
    currency_expr = (
        f"argMax({_q(column_map['currency'])}, {trade_date_column})"
        if column_map["currency"] is not None
        else "'KRW'"
    )
    order_expr = (
        f"tuple({trade_date_column}, {_q(column_map['updated_at'])})"
        if column_map["updated_at"] is not None
        else trade_date_column
    )

    filters = [
        f"has({{factor_ids:Array(String)}}, {factor_column})",
        f"{trade_date_column} <= {{as_of_date:Date}}",
        f"{year_expr} >= {{start_year:Int32}}",
        f"isFinite(toFloat64({value_column}))",
    ]
    stock_filter = _stock_filter(column_map)
    if stock_filter:
        filters.append(stock_filter)
    if column_map["financial_basis"] is not None:
        filters.append(f"has({{financial_basis:Array(String)}}, {_q(column_map['financial_basis'])})")

    return f"""
SELECT
    {factor_column} AS factor_id,
    {year_expr} AS fiscal_year,
    {month_expr} AS fiscal_month,
    {period_expr} AS period_end_date,
    toFloat64(argMax({value_column}, {order_expr})) AS value,
    {currency_expr} AS currency
FROM {table_name}
WHERE {' AND '.join(filters)}
GROUP BY
    factor_id,
    fiscal_year,
    fiscal_month,
    period_end_date
ORDER BY period_end_date ASC, factor_id ASC
""".strip()


def _ratio_column_map(columns: set[str]) -> dict[str, str | None]:
    return {
        "security_id": _pick_column(columns, ["security_id", "sec_id"]),
        "stock_code": _pick_column(columns, ["stock_code", "ticker", "symbol", "corp_code"]),
        "trade_date": _pick_column(columns, ["trade_date", "date", "as_of_date"]),
        "factor_id": _pick_column(columns, ["factor_id", "factor", "item_id"]),
        "financial_basis": _pick_column(columns, ["financial_basis", "basis"]),
        "value": _pick_column(columns, ["factor_value", "value"]),
        "fiscal_year": _pick_column(columns, ["fiscal_year", "year"]),
        "financial_period": _pick_column(columns, ["financial_period", "period_end_date", "period_date"]),
        "currency": _pick_column(columns, ["currency"]),
        "updated_at": _pick_column(columns, ["updated_at", "created_at", "loaded_at"]),
    }


def _stock_filter(column_map: dict[str, str | None]) -> str:
    if column_map["security_id"] is not None:
        return f"{_q(column_map['security_id'])} = {{security_id:String}}"
    if column_map["stock_code"] is not None:
        return f"leftPad(toString({_q(column_map['stock_code'])}), 6, '0') = {{stock_code:String}}"
    return ""


def _load_factor_catalog(client: Any, factor_ids: list[str]) -> dict[str, FactorCatalogItem]:
    try:
        rows = _records(
            client.query_df(
                """
SELECT
    factor_id,
    any(factor_name) AS factor_name,
    any(unit) AS unit,
    any(value_direction) AS value_direction,
    any(description) AS description
FROM factor_catalog
WHERE has({factor_ids:Array(String)}, factor_id)
GROUP BY factor_id
""".strip(),
                parameters={"factor_ids": factor_ids},
            )
        )
    except Exception:
        rows = []

    catalog = {
        str(row["factor_id"]): FactorCatalogItem(
            factor_id=str(row["factor_id"]),
            factor_name=_optional_str(row.get("factor_name")) or _humanize_factor_id(str(row["factor_id"])),
            unit=_optional_str(row.get("unit")),
            value_direction=_optional_str(row.get("value_direction")),
            description=_optional_str(row.get("description")),
        )
        for row in rows
        if row.get("factor_id") is not None
    }
    for factor_id in factor_ids:
        catalog.setdefault(
            factor_id,
            FactorCatalogItem(
                factor_id=factor_id,
                factor_name=_humanize_factor_id(factor_id),
                unit=_infer_unit(factor_id),
                value_direction=None,
                description=None,
            ),
        )
    return catalog


def _load_metadata(client: Any, stock_code: str, security_id: str) -> FinancialStatementMetadata:
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
    return FinancialStatementMetadata(
        stock_code=_optional_str(row.get("ticker")) or stock_code,
        security_id=security_id,
        stock_name=_optional_str(row.get("stock_name")),
        country=_optional_str(row.get("country")) or "KR",
        currency="KRW",
    )


def _load_table_columns(client: Any, table_name: str) -> set[str]:
    rows = _records(
        client.query_df(
            """
SELECT name
FROM system.columns
WHERE database = currentDatabase()
    AND table = {table_name:String}
""".strip(),
            parameters={"table_name": table_name},
        )
    )
    return {str(row["name"]) for row in rows if row.get("name") is not None}


def _select_visible_columns(
    rows: list[RawRatioRow],
    period: str,
) -> list[FinancialPeriodColumn]:
    period_map: dict[tuple[int, int, date], date] = {}
    for row in rows:
        period_map[(row.fiscal_year, row.fiscal_month, row.period_end_date)] = row.period_end_date

    selected = sorted(period_map.items(), key=lambda item: item[0])
    selected = selected[-PERIOD_LIMITS[period]:]
    return [
        FinancialPeriodColumn(
            key=_period_key(period_end_date),
            label=period_end_date.isoformat(),
            fiscal_year=year,
            fiscal_month=month,
            period_end_date=period_end_date,
        )
        for (year, month, period_end_date), _ in selected
    ]


def _build_sections(
    *,
    rows: list[RawRatioRow],
    columns: list[FinancialPeriodColumn],
    catalog: dict[str, FactorCatalogItem],
) -> list[FinancialRatioSection]:
    values_by_factor: dict[str, dict[str, RawRatioRow]] = {}
    for row in rows:
        values_by_factor.setdefault(row.factor_id, {})[_period_key(row.period_end_date)] = row

    sections: list[FinancialRatioSection] = []
    for statement_type, group_definitions in RATIO_GROUP_DEFINITIONS.items():
        title, title_en = STATEMENT_LABELS[statement_type]
        groups: list[FinancialRatioGroup] = []
        for group_key, group_title, group_title_en, factor_ids in group_definitions:
            ratios = [
                _build_ratio_row(
                    statement_type=statement_type,
                    group_key=group_key,
                    group_name=group_title,
                    factor_id=factor_id,
                    catalog=catalog,
                    factor_values=values_by_factor.get(factor_id, {}),
                    columns=columns,
                )
                for factor_id in factor_ids
            ]
            ratios = [ratio for ratio in ratios if _has_any_numeric_value(ratio)]
            groups.append(
                FinancialRatioGroup(
                    group_key=group_key,
                    title=group_title,
                    title_en=group_title_en,
                    ratios=ratios,
                )
            )
        sections.append(
            FinancialRatioSection(
                statement_type=statement_type,
                title=title,
                title_en=title_en,
                groups=groups,
            )
        )
    return sections


def _build_ratio_row(
    *,
    statement_type: str,
    group_key: str,
    group_name: str,
    factor_id: str,
    catalog: dict[str, FactorCatalogItem],
    factor_values: dict[str, RawRatioRow],
    columns: list[FinancialPeriodColumn],
) -> FinancialRatioRow:
    catalog_item = catalog[factor_id]
    numeric_by_key = {
        column.key: factor_values[column.key].value if column.key in factor_values else None
        for column in columns
    }
    growth_by_key = _growth_by_period_key(numeric_by_key, columns)

    cells = [
        FinancialStatementCell(
            period_key=column.key,
            value=_clean_number(numeric_by_key[column.key]),
            display_value=_format_factor_value(numeric_by_key[column.key], catalog_item.unit),
            growth_rate=_clean_number(growth_by_key[column.key]),
            display_growth_rate=_format_percent(growth_by_key[column.key]),
        )
        for column in columns
    ]
    trend = [
        FinancialChartPoint(
            period_key=column.key,
            label=column.label,
            value=_clean_number(numeric_by_key[column.key]),
        )
        for column in columns
    ]
    growth_chart = [
        FinancialChartPoint(
            period_key=column.key,
            label=column.label,
            value=_clean_number(growth_by_key[column.key]),
        )
        for column in columns
    ]

    return FinancialRatioRow(
        factor_id=factor_id,
        factor_name=catalog_item.factor_name,
        statement_type=statement_type,
        group_key=group_key,
        group_name=group_name,
        unit=catalog_item.unit,
        value_direction=catalog_item.value_direction,
        description=catalog_item.description,
        values=cells,
        trend=trend,
        growth_chart=growth_chart,
        statistics=_statistics(list(numeric_by_key.values())),
    )


def _growth_by_period_key(
    numeric_by_key: dict[str, float | None],
    columns: list[FinancialPeriodColumn],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for index, column in enumerate(columns):
        previous_index = index - 1
        current = numeric_by_key[column.key]
        previous = numeric_by_key[columns[previous_index].key] if previous_index >= 0 else None
        result[column.key] = _growth_rate(current, previous)
    return result


def _to_raw_ratio_row(row: dict[str, Any]) -> RawRatioRow:
    period_end_date = _as_date(row.get("period_end_date"))
    fiscal_year = int(_float_or_none(row.get("fiscal_year")) or period_end_date.year)
    fiscal_month = int(_float_or_none(row.get("fiscal_month")) or period_end_date.month)
    return RawRatioRow(
        factor_id=str(row["factor_id"]).strip(),
        fiscal_year=fiscal_year,
        fiscal_month=fiscal_month,
        period_end_date=period_end_date,
        value=_float_or_none(row.get("value")),
        currency=_optional_str(row.get("currency")),
    )


def _all_ratio_factor_ids() -> list[str]:
    result: list[str] = []
    for group_definitions in RATIO_GROUP_DEFINITIONS.values():
        for _, _, _, factor_ids in group_definitions:
            result.extend(factor_ids)
    return list(dict.fromkeys(result))


def _normalize_period(period: str) -> str:
    normalized = str(period).strip().lower()
    if normalized in {"quarterly", "q"}:
        normalized = "quarter"
    if normalized not in PERIOD_LIMITS:
        raise ValueError("period must be one of: annual, quarter")
    return normalized


def _normalize_stock_code(stock_code: str) -> str:
    normalized = str(stock_code).strip().upper()
    if not _STOCK_CODE_RE.match(normalized):
        raise ValueError("stock_code must contain only letters and digits")
    return normalized.zfill(6)


def _pick_column(columns: set[str], candidates: list[str]) -> str | None:
    lower_to_original = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in lower_to_original:
            return lower_to_original[candidate.lower()]
    return None


def _q(identifier: str | None) -> str:
    if identifier is None or not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"unsafe column identifier: {identifier}")
    return f"`{identifier}`"


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


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


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


def _clean_number(value: Any) -> float | None:
    return _float_or_none(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if value == "":
        return None
    return str(value)


def _period_key(value: date) -> str:
    return value.isoformat()


def _growth_rate(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100


def _statistics(values: list[float | None]) -> FinancialAccountStatistics:
    clean_values = [_clean_number(value) for value in values]
    clean_values = [value for value in clean_values if value is not None]
    if not clean_values:
        return FinancialAccountStatistics()
    return FinancialAccountStatistics(
        latest=clean_values[-1],
        maximum=max(clean_values),
        minimum=min(clean_values),
        average=sum(clean_values) / len(clean_values),
    )


def _has_any_numeric_value(ratio: FinancialRatioRow) -> bool:
    return any(cell.value is not None for cell in ratio.values)


def _format_factor_value(value: float | None, unit: str | None) -> str:
    number = _clean_number(value)
    if number is None:
        return "N/A"
    normalized_unit = (unit or "").strip().lower()
    if normalized_unit == "percent":
        return f"{number:.2f}%"
    if normalized_unit == "times":
        return f"{number:.2f}x"
    if normalized_unit == "days":
        return f"{number:.1f}d"
    if normalized_unit == "score":
        return f"{number:.2f}"
    if normalized_unit in {"krw", "shares"}:
        return _format_value(number)
    if abs(number) >= 100:
        return f"{number:,.2f}"
    return f"{number:.2f}"


def _format_percent(value: float | None) -> str:
    number = _clean_number(value)
    if number is None:
        return "N/A"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.0f}%"


def _format_value(value: float | None) -> str:
    number = _clean_number(value)
    if number is None:
        return "N/A"
    abs_value = abs(number)
    if abs_value >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.1f}T"
    if abs_value >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{number / 1_000:.1f}K"
    if float(number).is_integer():
        return str(int(number))
    return f"{number:.2f}"


def _humanize_factor_id(factor_id: str) -> str:
    return factor_id.replace("_", " ").upper()


def _infer_unit(factor_id: str) -> str | None:
    if factor_id.endswith("_pct") or factor_id in {
        "gpm",
        "opm",
        "ebitda_margin",
        "npm",
        "tax_rate",
        "roe",
        "roa",
        "iroe",
        "roic_financial",
        "roic_operational",
        "roce",
        "rdsr_pct",
        "dividend_yield",
        "payout_ratio",
        "sharehold_div_yield",
        "sharehold_net_buyback_yield",
        "sharehold_return",
        "tdpr",
    }:
        return "percent"
    if factor_id.endswith("_times") or factor_id in {
        "per",
        "pbr",
        "pcr",
        "psr",
        "peg",
        "current_ratio",
        "debt_to_equity",
        "debt_ratio",
        "cash_to_debt",
        "net_debt_to_ebitda",
        "net_debt_to_ocf",
        "fc_to_ndr",
        "interest_coverage",
        "total_interest_coverage",
        "asset_turnover",
        "receivables_turnover",
        "inventory_turnover",
        "working_capital_turnover",
    }:
        return "times"
    if factor_id in {"inv_days", "ar_days", "ap_days", "ccc"}:
        return "days"
    if factor_id.endswith("_score") or factor_id == "f_score":
        return "score"
    if factor_id in {
        "eps",
        "sps",
        "bps",
        "dvpsp",
        "dvpsx",
        "mcap_mil",
        "sales_change_mil",
        "op_change_mil",
    }:
        return "krw"
    return "ratio"
