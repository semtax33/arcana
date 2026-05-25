from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.model.financials import (
    FinancialAccountDetailResponse,
    FinancialAccountRow,
    FinancialAccountStatistics,
    FinancialChartPoint,
    FinancialPeriodColumn,
    FinancialStatementCell,
    FinancialStatementMetadata,
    FinancialStatementSection,
    FinancialStatementsResponse,
)
from engine.core.paths import DATA_LAKE
from engine.transformers._internal.statement_files import read_statement_period_frames


DEFAULT_CANONICAL_ACCOUNTS_PATH = DATA_LAKE.canonical_accounts()
DEFAULT_NORMALIZED_STATEMENT_DIR = DATA_LAKE.silver("dart", "normalized")
FACT_TABLE = "fact_canonical_statements"
STATEMENT_TYPES = ("IS", "BS", "CF")
STATEMENT_LABELS = {
    "IS": ("손익계산서", "Income Statement"),
    "BS": ("재무상태표", "Balance Sheet"),
    "CF": ("현금흐름표", "Cash Flow"),
}
PERIOD_LIMITS = {
    "annual": 10,
    "quarter": 20,
    "ttm": 40,
}
FLOW_STATEMENT_TYPES = {"IS", "CIS", "CF"}
BALANCE_STATEMENT_TYPES = {"BS"}
DERIVED_FCF_ID = "FCF"
PER_SHARE_ACCOUNT_IDS = {"BASIC_EPS", "DILUTED_EPS"}
MONETARY_DISPLAY_SCALE = 1_000_000
UNIT_SCALE_OUTLIER_FACTORS = (1_000, 1_000_000)
UNIT_SCALE_OUTLIER_MIN_SUPPORT = 3
UNIT_SCALE_OUTLIER_MULTIPLE = 100
UNIT_SCALE_REPAIRED_MULTIPLE = 10

PREFERRED_ACCOUNT_ORDER = {
    "IS": [
        "REVENUE",
        "COGS",
        "GROSS_PROFIT",
        "RND",
        "SGNA",
        "OPERATING_EXPENSES_TOTAL",
        "OPERATING_INCOME",
        "PBT",
        "TAX_EXPENSE",
        "NET_INCOME",
        "NET_INCOME_PARENT",
        "BASIC_EPS",
        "DILUTED_EPS",
    ],
    "BS": [
        "TOTAL_ASSETS",
        "CASH_AND_EQUIVALENTS",
        "INVENTORIES",
        "TRADE_RECEIVABLES",
        "CURRENT_ASSETS",
        "NON_CURRENT_ASSETS",
        "PPE",
        "INTANGIBLE_ASSETS",
        "TOTAL_LIABILITIES",
        "CURRENT_LIABILITIES",
        "NON_CURRENT_LIABILITIES",
        "SHORT_TERM_DEBT",
        "LONG_TERM_DEBT",
        "TOTAL_EQUITY",
        "EAOP",
        "RETAINED_EARNINGS",
    ],
    "CF": [
        "CFO",
        "CFI",
        "CFF",
        DERIVED_FCF_ID,
        "CAPEX_PPE",
        "CAPEX_INTANG",
        "DIV_PAID",
        "DEBT_ISSUE",
        "DEBT_REPAY",
        "CF_CASH_CHANGE",
        "CF_CASH_BEGIN",
        "CF_CASH_END",
    ],
}

_STOCK_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,12}$")
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z_]+$")


class FinancialStatementsNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalAccount:
    canonical_id: str
    account_name: str
    statement_type: str
    is_derived: bool = False
    formula: str | None = None
    description: str | None = None
    order: int = 9999


@dataclass(frozen=True)
class RawStatementRow:
    canonical_id: str
    account_name: str
    statement_type: str
    fiscal_year: int
    fiscal_month: int
    period_end_date: date
    value: float | None
    currency: str | None = None


@dataclass(frozen=True)
class PeriodValue:
    canonical_id: str
    statement_type: str
    fiscal_year: int
    fiscal_month: int
    period_end_date: date
    value: float | None
    currency: str | None = None


class FinancialStatementsService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
        canonical_accounts_path: Path = DEFAULT_CANONICAL_ACCOUNTS_PATH,
        normalized_statement_dir: Path = DEFAULT_NORMALIZED_STATEMENT_DIR,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst
        self._canonical_accounts_path = canonical_accounts_path
        self._normalized_statement_dir = normalized_statement_dir

    def get_statements(
        self,
        stock_code: str,
        period: str = "annual",
        statement: str = "all",
    ) -> FinancialStatementsResponse:
        normalized_period = _normalize_period(period)
        statement_filter = _normalize_statement_filter(statement)
        normalized_stock_code = _normalize_stock_code(stock_code)
        security_id = f"SEC_KR_{normalized_stock_code}"
        as_of_date = self._today_factory()
        start_year = as_of_date.year - 12
        catalog = _load_canonical_accounts(self._canonical_accounts_path)

        rows: list[RawStatementRow] = []
        metadata = FinancialStatementMetadata(
            stock_code=normalized_stock_code,
            security_id=security_id,
            currency="KRW",
        )
        client = None
        try:
            client = self._client_factory()
            try:
                fact_columns = _load_table_columns(client, FACT_TABLE)
                rows = _load_statement_rows(
                    client,
                    columns=fact_columns,
                    stock_code=normalized_stock_code,
                    security_id=security_id,
                    start_year=start_year,
                    statement_filter=statement_filter,
                )
            except Exception:
                rows = []

            if not rows:
                rows = _load_normalized_statement_rows(
                    stock_code=normalized_stock_code,
                    start_year=start_year,
                    statement_filter=statement_filter,
                    normalized_statement_dir=self._normalized_statement_dir,
                )
            metadata = _load_metadata(client, normalized_stock_code, security_id, rows)
        except Exception:
            if not rows:
                rows = _load_normalized_statement_rows(
                    stock_code=normalized_stock_code,
                    start_year=start_year,
                    statement_filter=statement_filter,
                    normalized_statement_dir=self._normalized_statement_dir,
                )
            metadata = FinancialStatementMetadata(
                stock_code=normalized_stock_code,
                security_id=security_id,
                currency=_first_present_currency(rows) or "KRW",
            )
        finally:
            close = getattr(client, "close", None) if client is not None else None
            if callable(close):
                close()

        if not rows:
            raise FinancialStatementsNotFoundError(
                f"financial statement data not found for stock_code={stock_code}"
            )

        return self._build_response(
            stock=metadata,
            period=normalized_period,
            statement=statement_filter,
            catalog=catalog,
            rows=rows,
        )

    def get_account_detail(
        self,
        stock_code: str,
        canonical_id: str,
        period: str = "annual",
    ) -> FinancialAccountDetailResponse:
        response = self.get_statements(stock_code, period=period, statement="all")
        target_id = canonical_id.strip().upper()
        for section in response.sections:
            for account in section.accounts:
                if account.canonical_id == target_id:
                    return FinancialAccountDetailResponse(
                        stock=response.stock,
                        period=response.period,
                        statement_type=section.statement_type,
                        account=account,
                        columns=response.columns,
                        source=response.source,
                    )

        raise FinancialStatementsNotFoundError(
            f"financial account data not found for stock_code={stock_code}, canonical_id={canonical_id}"
        )

    def _build_response(
        self,
        *,
        stock: FinancialStatementMetadata,
        period: str,
        statement: str,
        catalog: dict[str, CanonicalAccount],
        rows: list[RawStatementRow],
    ) -> FinancialStatementsResponse:
        rows = _repair_unit_scale_outliers(rows)
        periodized_values = _periodize_rows(rows, period)
        periodized_values = _add_derived_fcf(periodized_values)
        visible_columns = _select_visible_columns(periodized_values, period)
        sections = _build_sections(
            period=period,
            statement_filter=statement,
            catalog=catalog,
            period_values=periodized_values,
            columns=visible_columns,
            default_currency=stock.currency,
        )

        if not visible_columns or not any(section.accounts for section in sections):
            raise FinancialStatementsNotFoundError(
                f"financial statement data not found for stock_code={stock.stock_code}"
            )

        return FinancialStatementsResponse(
            stock=stock,
            period=period,
            statement=statement,
            columns=visible_columns,
            sections=sections,
            source=FACT_TABLE,
        )


def _load_statement_rows(
    client: Any,
    *,
    columns: set[str],
    stock_code: str,
    security_id: str,
    start_year: int,
    statement_filter: str,
) -> list[RawStatementRow]:
    column_map = _statement_column_map(columns)
    query = _build_statement_query(column_map, statement_filter=statement_filter)
    params = {
        "stock_code": stock_code,
        "security_id": security_id,
        "start_year": start_year,
        "statement_types": list(STATEMENT_TYPES) + ["CIS"],
    }
    if statement_filter != "all":
        params["statement_filter_types"] = ["IS", "CIS"] if statement_filter == "IS" else [statement_filter]
    rows = _records(client.query_df(query, parameters=params))
    return [_to_raw_statement_row(row) for row in rows if _float_or_none(row.get("value")) is not None]


def _load_normalized_statement_rows(
    *,
    stock_code: str,
    start_year: int,
    statement_filter: str,
    normalized_statement_dir: Path,
) -> list[RawStatementRow]:
    rows: list[RawStatementRow] = []
    if not normalized_statement_dir.exists():
        return rows

    for fiscal_year, fiscal_month, frame in read_statement_period_frames(
        stock_code,
        normalized_statement_dir,
        market="kr",
    ):
        if fiscal_year < start_year or fiscal_month not in {3, 6, 9, 12}:
            continue
        rows.extend(
            _load_normalized_frame_rows(
                frame=frame,
                fiscal_year=fiscal_year,
                fiscal_month=fiscal_month,
                statement_filter=statement_filter,
            )
        )

    return rows


def _load_normalized_file_rows(
    *,
    path: Path,
    fiscal_year: int,
    fiscal_month: int,
    statement_filter: str,
) -> list[RawStatementRow]:
    try:
        return _load_normalized_frame_rows(
            frame=pd.read_csv(path),
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            statement_filter=statement_filter,
        )
    except FileNotFoundError:
        return []


def _load_normalized_frame_rows(
    *,
    frame: Any,
    fiscal_year: int,
    fiscal_month: int,
    statement_filter: str,
) -> list[RawStatementRow]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
    for row in records:
        canonical_id = _optional_str(row.get("canonical_account_id"))
        if not canonical_id or canonical_id == "UNMAPPED":
            continue
        statement_type = _visible_statement_type(_optional_str(row.get("statement_type")) or "")
        if statement_type not in STATEMENT_TYPES:
            continue
        if statement_filter != "all" and statement_type != statement_filter:
            continue

        value = _float_or_none(row.get("normalized_amount") or row.get("amount"))
        if value is None:
            continue
        key = (statement_type, canonical_id)
        current = grouped.get(key)
        if current is None or abs(value) > abs(current["value"]):
            grouped[key] = {
                "statement_type": statement_type,
                "canonical_id": canonical_id,
                "account_name": _optional_str(row.get("canonical_account_name")) or canonical_id,
                "value": value,
            }

    period_end_date = _period_end_date(fiscal_year, fiscal_month)
    return [
        RawStatementRow(
            canonical_id=item["canonical_id"],
            account_name=item["account_name"],
            statement_type=item["statement_type"],
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            period_end_date=period_end_date,
            value=item["value"],
            currency="KRW",
        )
        for item in grouped.values()
    ]


def _statement_column_map(columns: set[str]) -> dict[str, str | None]:
    return {
        "security_id": _pick_column(columns, ["security_id", "sec_id"]),
        "stock_code": _pick_column(columns, ["stock_code", "ticker", "symbol", "corp_code"]),
        "statement_type": _pick_column(columns, ["statement_type", "fs_type"]),
        "canonical_id": _pick_column(
            columns,
            ["canonical_account_id", "canonical_id", "account_id"],
        ),
        "account_name": _pick_column(
            columns,
            ["canonical_account_name", "canonical_nm", "account_name"],
        ),
        "fiscal_year": _pick_column(columns, ["fiscal_year", "fs_year", "year"]),
        "fiscal_month": _pick_column(columns, ["fiscal_month", "month"]),
        "fiscal_quarter": _pick_column(columns, ["fiscal_quarter", "quarter"]),
        "period_end_date": _pick_column(
            columns,
            ["financial_period", "period_end_date", "period_date", "trade_date", "as_of_date"],
        ),
        "value": _pick_column(
            columns,
            ["normalized_amount", "value", "amount", "statement_value", "factor_value"],
        ),
        "currency": _pick_column(columns, ["currency"]),
        "updated_at": _pick_column(columns, ["updated_at", "created_at", "loaded_at"]),
    }


def _build_statement_query(column_map: dict[str, str | None], *, statement_filter: str) -> str:
    required_keys = ["statement_type", "canonical_id", "value"]
    missing = [key for key in required_keys if column_map[key] is None]
    if missing:
        raise ValueError(f"{FACT_TABLE} missing required columns: {', '.join(missing)}")
    if column_map["fiscal_year"] is None and column_map["period_end_date"] is None:
        raise ValueError(f"{FACT_TABLE} missing fiscal year/date column")
    if (
        column_map["fiscal_month"] is None
        and column_map["fiscal_quarter"] is None
        and column_map["period_end_date"] is None
    ):
        raise ValueError(f"{FACT_TABLE} missing fiscal month/quarter/date column")

    statement_column = _q(column_map["statement_type"])
    canonical_column = _q(column_map["canonical_id"])
    value_column = _q(column_map["value"])
    year_expr = (
        _q(column_map["fiscal_year"])
        if column_map["fiscal_year"] is not None
        else f"toYear({_q(column_map['period_end_date'])})"
    )
    if column_map["fiscal_month"] is not None:
        month_expr = _q(column_map["fiscal_month"])
    elif column_map["fiscal_quarter"] is not None:
        quarter_column = _q(column_map["fiscal_quarter"])
        month_expr = (
            f"multiIf("
            f"match(toString({quarter_column}), '1$'), 3, "
            f"match(toString({quarter_column}), '2$'), 6, "
            f"match(toString({quarter_column}), '3$'), 9, "
            f"match(toString({quarter_column}), '4$'), 12, "
            f"12)"
        )
    else:
        month_expr = f"toMonth({_q(column_map['period_end_date'])})"

    account_expr = (
        f"any({_q(column_map['account_name'])})"
        if column_map["account_name"] is not None
        else "''"
    )
    currency_expr = (
        f"any({_q(column_map['currency'])})"
        if column_map["currency"] is not None
        else "'KRW'"
    )
    value_expr = (
        f"argMax({value_column}, {_q(column_map['updated_at'])})"
        if column_map["updated_at"] is not None
        else f"any({value_column})"
    )

    filters = [
        f"{canonical_column} != 'UNMAPPED'",
        f"{statement_column} IN {{statement_types:Array(String)}}",
        f"toInt32({year_expr}) >= {{start_year:Int32}}",
        f"isFinite(toFloat64({value_column}))",
    ]
    if statement_filter != "all":
        filters.append(f"{statement_column} IN {{statement_filter_types:Array(String)}}")
    stock_filter = _stock_filter(column_map)
    if stock_filter:
        filters.append(stock_filter)

    params_filter = "\n    AND ".join(filters)
    period_date_expr = (
        f"max({_q(column_map['period_end_date'])})"
        if column_map["period_end_date"] is not None
        else "NULL"
    )
    return f"""
SELECT
    {statement_column} AS statement_type,
    {canonical_column} AS canonical_id,
    {account_expr} AS account_name,
    toInt32({year_expr}) AS fiscal_year,
    toInt32({month_expr}) AS fiscal_month,
    {period_date_expr} AS period_end_date,
    toFloat64({value_expr}) AS value,
    {currency_expr} AS currency
FROM {FACT_TABLE}
WHERE {params_filter}
GROUP BY
    statement_type,
    canonical_id,
    fiscal_year,
    fiscal_month
ORDER BY fiscal_year ASC, fiscal_month ASC, statement_type ASC, canonical_id ASC
""".strip()


def _stock_filter(column_map: dict[str, str | None]) -> str:
    if column_map["security_id"] is not None:
        return f"{_q(column_map['security_id'])} = {{security_id:String}}"
    if column_map["stock_code"] is not None:
        return f"leftPad(toString({_q(column_map['stock_code'])}), 6, '0') = {{stock_code:String}}"
    return ""


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


def _load_metadata(
    client: Any,
    stock_code: str,
    security_id: str,
    statement_rows: list[RawStatementRow],
) -> FinancialStatementMetadata:
    currency = _first_present_currency(statement_rows) or "KRW"
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
        currency=currency,
    )


def _periodize_rows(rows: list[RawStatementRow], period: str) -> list[PeriodValue]:
    by_account: dict[tuple[str, str], list[RawStatementRow]] = {}
    for row in rows:
        statement_type = _visible_statement_type(row.statement_type)
        by_account.setdefault((statement_type, row.canonical_id), []).append(
            RawStatementRow(
                canonical_id=row.canonical_id,
                account_name=row.account_name,
                statement_type=statement_type,
                fiscal_year=row.fiscal_year,
                fiscal_month=row.fiscal_month,
                period_end_date=row.period_end_date,
                value=row.value,
                currency=row.currency,
            )
        )

    result: list[PeriodValue] = []
    for (statement_type, canonical_id), account_rows in by_account.items():
        account_rows = sorted(account_rows, key=lambda item: (item.fiscal_year, item.fiscal_month))
        if period == "annual":
            result.extend(_annual_values(account_rows, statement_type, canonical_id))
        elif period == "quarter":
            result.extend(_quarter_values(account_rows, statement_type, canonical_id))
        else:
            result.extend(_ttm_values(account_rows, statement_type, canonical_id))
    return result


def _repair_unit_scale_outliers(rows: list[RawStatementRow]) -> list[RawStatementRow]:
    if len(rows) < 3:
        return rows

    comparable_counts: dict[tuple[int, int], int] = {}
    scale_support: dict[tuple[int, int, float], int] = {}
    by_account: dict[tuple[str, str], list[RawStatementRow]] = {}

    for row in rows:
        statement_type = _visible_statement_type(row.statement_type)
        canonical_id = row.canonical_id.strip().upper()
        if canonical_id in PER_SHARE_ACCOUNT_IDS:
            continue
        by_account.setdefault((statement_type, canonical_id), []).append(row)

    for account_rows in by_account.values():
        ordered = sorted(account_rows, key=lambda item: (item.fiscal_year, item.fiscal_month))
        for index, current in enumerate(ordered):
            if current.value is None:
                continue

            current_abs = abs(current.value)
            neighbor_values = []
            if index > 0 and ordered[index - 1].value is not None:
                neighbor_values.append(abs(ordered[index - 1].value))
            if index + 1 < len(ordered) and ordered[index + 1].value is not None:
                neighbor_values.append(abs(ordered[index + 1].value))
            if not neighbor_values:
                continue

            neighbor_abs = max(neighbor_values)
            if current_abs == 0 or neighbor_abs == 0:
                continue

            period_key = (current.fiscal_year, current.fiscal_month)
            comparable_counts[period_key] = comparable_counts.get(period_key, 0) + 1
            if current_abs <= neighbor_abs * UNIT_SCALE_OUTLIER_MULTIPLE:
                continue

            for scale_factor in UNIT_SCALE_OUTLIER_FACTORS:
                repaired_abs = current_abs / scale_factor
                if repaired_abs <= neighbor_abs * UNIT_SCALE_REPAIRED_MULTIPLE:
                    scale_key = (current.fiscal_year, current.fiscal_month, float(scale_factor))
                    scale_support[scale_key] = scale_support.get(scale_key, 0) + 1
                    break

    selected_scales: dict[tuple[int, int], float] = {}
    for (year, month, scale_factor), support_count in scale_support.items():
        period_key = (year, month)
        comparable_count = comparable_counts.get(period_key, 0)
        if support_count < UNIT_SCALE_OUTLIER_MIN_SUPPORT:
            continue
        if comparable_count and support_count / comparable_count < 0.25:
            continue
        current_scale = selected_scales.get(period_key)
        if current_scale is None or scale_factor < current_scale:
            selected_scales[period_key] = scale_factor

    if not selected_scales:
        return rows

    repaired_rows: list[RawStatementRow] = []
    for row in rows:
        scale_factor = selected_scales.get((row.fiscal_year, row.fiscal_month))
        canonical_id = row.canonical_id.strip().upper()
        if scale_factor is None or row.value is None or canonical_id in PER_SHARE_ACCOUNT_IDS:
            repaired_rows.append(row)
            continue
        repaired_rows.append(
            RawStatementRow(
                canonical_id=row.canonical_id,
                account_name=row.account_name,
                statement_type=row.statement_type,
                fiscal_year=row.fiscal_year,
                fiscal_month=row.fiscal_month,
                period_end_date=row.period_end_date,
                value=row.value / scale_factor,
                currency=row.currency,
            )
        )
    return repaired_rows


def _annual_values(
    rows: list[RawStatementRow],
    statement_type: str,
    canonical_id: str,
) -> list[PeriodValue]:
    by_year: dict[int, list[RawStatementRow]] = {}
    for row in rows:
        by_year.setdefault(row.fiscal_year, []).append(row)

    values: list[PeriodValue] = []
    for year, year_rows in sorted(by_year.items()):
        selected = next((row for row in year_rows if row.fiscal_month == 12), year_rows[-1])
        values.append(_period_value(selected, statement_type, canonical_id, selected.value))
    return values


def _quarter_values(
    rows: list[RawStatementRow],
    statement_type: str,
    canonical_id: str,
) -> list[PeriodValue]:
    values: list[PeriodValue] = []
    previous_by_year: dict[int, RawStatementRow] = {}

    for row in rows:
        if statement_type in BALANCE_STATEMENT_TYPES:
            value = row.value
        elif row.fiscal_month == 3:
            value = row.value
        else:
            previous = previous_by_year.get(row.fiscal_year)
            value = _subtract_or_none(row.value, previous.value if previous else None)
        values.append(_period_value(row, statement_type, canonical_id, value))
        previous_by_year[row.fiscal_year] = row

    return values


def _ttm_values(
    rows: list[RawStatementRow],
    statement_type: str,
    canonical_id: str,
) -> list[PeriodValue]:
    if statement_type in BALANCE_STATEMENT_TYPES:
        return [
            _period_value(row, statement_type, canonical_id, row.value)
            for row in rows
        ]

    quarters = _quarter_values(rows, statement_type, canonical_id)
    values: list[PeriodValue] = []
    for index, row in enumerate(quarters):
        window = quarters[index - 3:index + 1]
        value = None
        if len(window) == 4 and all(item.value is not None for item in window):
            value = sum(item.value or 0 for item in window)
        values.append(
            PeriodValue(
                canonical_id=canonical_id,
                statement_type=statement_type,
                fiscal_year=row.fiscal_year,
                fiscal_month=row.fiscal_month,
                period_end_date=row.period_end_date,
                value=value,
                currency=row.currency,
            )
        )
    return values


def _add_derived_fcf(values: list[PeriodValue]) -> list[PeriodValue]:
    by_period: dict[tuple[int, int], dict[str, PeriodValue]] = {}
    for value in values:
        if value.statement_type == "CF":
            by_period.setdefault((value.fiscal_year, value.fiscal_month), {})[value.canonical_id] = value

    derived: list[PeriodValue] = []
    for items in by_period.values():
        cfo = items.get("CFO")
        if cfo is None:
            continue
        capex_values = [
            items[item].value
            for item in ("CAPEX_PPE", "CAPEX_INTANG")
            if item in items and items[item].value is not None
        ]
        if not capex_values:
            continue
        capex = sum(capex_values)
        if cfo.value is None:
            fcf_value = None
        else:
            fcf_value = cfo.value + capex
        derived.append(
            PeriodValue(
                canonical_id=DERIVED_FCF_ID,
                statement_type="CF",
                fiscal_year=cfo.fiscal_year,
                fiscal_month=cfo.fiscal_month,
                period_end_date=cfo.period_end_date,
                value=fcf_value,
                currency=cfo.currency,
            )
        )

    return values + derived


def _select_visible_columns(
    period_values: list[PeriodValue],
    period: str,
) -> list[FinancialPeriodColumn]:
    period_map: dict[tuple[int, int], date] = {}
    for value in period_values:
        if period == "annual" and value.fiscal_month != 12:
            continue
        period_map[(value.fiscal_year, value.fiscal_month)] = value.period_end_date

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
        for (year, month), period_end_date in selected
    ]


def _build_sections(
    *,
    period: str,
    statement_filter: str,
    catalog: dict[str, CanonicalAccount],
    period_values: list[PeriodValue],
    columns: list[FinancialPeriodColumn],
    default_currency: str | None,
) -> list[FinancialStatementSection]:
    values_by_account: dict[tuple[str, str], dict[str, PeriodValue]] = {}
    for item in period_values:
        key = (item.statement_type, item.canonical_id)
        values_by_account.setdefault(key, {})[_period_key(item.period_end_date)] = item

    requested_statements = (
        [statement_filter]
        if statement_filter != "all"
        else list(STATEMENT_TYPES)
    )
    column_keys = [column.key for column in columns]
    sections: list[FinancialStatementSection] = []

    for statement_type in requested_statements:
        title, title_en = STATEMENT_LABELS[statement_type]
        account_ids = [
            canonical_id
            for key_statement_type, canonical_id in values_by_account
            if key_statement_type == statement_type
        ]
        account_ids = sorted(set(account_ids), key=lambda item: _account_sort_key(catalog, statement_type, item))
        accounts = [
            _build_account_row(
                statement_type=statement_type,
                canonical_id=canonical_id,
                catalog=catalog,
                account_values=values_by_account.get((statement_type, canonical_id), {}),
                columns=columns,
                column_keys=column_keys,
                period=period,
                default_currency=default_currency,
            )
            for canonical_id in account_ids
        ]
        accounts = [account for account in accounts if _has_any_numeric_value(account)]
        sections.append(
            FinancialStatementSection(
                statement_type=statement_type,
                title=title,
                title_en=title_en,
                accounts=accounts,
            )
        )

    return sections


def _build_account_row(
    *,
    statement_type: str,
    canonical_id: str,
    catalog: dict[str, CanonicalAccount],
    account_values: dict[str, PeriodValue],
    columns: list[FinancialPeriodColumn],
    column_keys: list[str],
    period: str,
    default_currency: str | None,
) -> FinancialAccountRow:
    catalog_item = catalog.get(canonical_id) or _virtual_catalog_item(canonical_id, statement_type)
    numeric_by_key = {
        column.key: account_values[column.key].value if column.key in account_values else None
        for column in columns
    }
    growth_by_key = _growth_by_period_key(numeric_by_key, columns, period)
    currency = _first_value_currency(account_values, column_keys) or default_currency or "KRW"

    cells = [
        FinancialStatementCell(
            period_key=column.key,
            value=_clean_number(numeric_by_key[column.key]),
            display_value=_format_statement_value(numeric_by_key[column.key], canonical_id),
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

    return FinancialAccountRow(
        canonical_id=canonical_id,
        account_name=catalog_item.account_name,
        statement_type=statement_type,
        is_derived=catalog_item.is_derived,
        formula=catalog_item.formula,
        description=catalog_item.description,
        unit=_statement_unit(canonical_id),
        currency=currency,
        values=cells,
        trend=trend,
        growth_chart=growth_chart,
        statistics=_statistics(list(numeric_by_key.values())),
    )


def _growth_by_period_key(
    numeric_by_key: dict[str, float | None],
    columns: list[FinancialPeriodColumn],
    period: str,
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    value_by_year_month = {
        (column.fiscal_year, column.fiscal_month): numeric_by_key[column.key]
        for column in columns
    }

    for index, column in enumerate(columns):
        current = numeric_by_key[column.key]
        previous = None
        if period == "annual":
            previous = numeric_by_key[columns[index - 1].key] if index > 0 else None
        else:
            previous = value_by_year_month.get((column.fiscal_year - 1, column.fiscal_month))
        result[column.key] = _growth_rate(current, previous)

    return result


def _load_canonical_accounts(path: Path) -> dict[str, CanonicalAccount]:
    rows: dict[str, CanonicalAccount] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for order, row in enumerate(reader):
                canonical_id = _optional_str(row.get("canonical_id"))
                if not canonical_id:
                    continue
                statement_type = _visible_statement_type(_optional_str(row.get("fs_type")) or "")
                if statement_type not in STATEMENT_TYPES:
                    continue
                rows[canonical_id] = CanonicalAccount(
                    canonical_id=canonical_id,
                    account_name=_optional_str(row.get("canonical_nm")) or canonical_id,
                    statement_type=statement_type,
                    is_derived=_bool(row.get("is_derived")),
                    formula=_optional_str(row.get("formula")),
                    description=_optional_str(row.get("description")),
                    order=order,
                )
    except FileNotFoundError:
        pass

    rows.setdefault(
        DERIVED_FCF_ID,
        CanonicalAccount(
            canonical_id=DERIVED_FCF_ID,
            account_name="잉여현금흐름(FCF)",
            statement_type="CF",
            is_derived=True,
            formula="CFO + CAPEX_PPE + CAPEX_INTANG",
            description="영업활동현금흐름에서 유형/무형자산 취득 지출을 반영한 잉여현금흐름",
            order=10_000,
        ),
    )
    return rows


def _to_raw_statement_row(row: dict[str, Any]) -> RawStatementRow:
    fiscal_year = int(_float_or_none(row.get("fiscal_year")) or 0)
    fiscal_month = int(_float_or_none(row.get("fiscal_month")) or 0)
    if fiscal_month <= 0:
        fiscal_month = 12
    period_end_date = _as_date_or_none(row.get("period_end_date"))
    if period_end_date is None:
        period_end_date = _period_end_date(fiscal_year, fiscal_month)

    return RawStatementRow(
        canonical_id=str(row["canonical_id"]).strip().upper(),
        account_name=_optional_str(row.get("account_name")) or str(row["canonical_id"]).strip().upper(),
        statement_type=_visible_statement_type(str(row["statement_type"])),
        fiscal_year=fiscal_year,
        fiscal_month=fiscal_month,
        period_end_date=period_end_date,
        value=_float_or_none(row.get("value")),
        currency=_optional_str(row.get("currency")),
    )


def _period_value(
    row: RawStatementRow,
    statement_type: str,
    canonical_id: str,
    value: float | None,
) -> PeriodValue:
    return PeriodValue(
        canonical_id=canonical_id,
        statement_type=statement_type,
        fiscal_year=row.fiscal_year,
        fiscal_month=row.fiscal_month,
        period_end_date=row.period_end_date,
        value=value,
        currency=row.currency,
    )


def _normalize_period(period: str) -> str:
    normalized = str(period).strip().lower()
    if normalized not in PERIOD_LIMITS:
        raise ValueError("period must be one of: annual, quarter, ttm")
    return normalized


def _normalize_statement_filter(statement: str) -> str:
    normalized = str(statement).strip().upper()
    if normalized in {"", "ALL"}:
        return "all"
    if normalized not in STATEMENT_TYPES:
        raise ValueError("statement must be one of: all, IS, BS, CF")
    return normalized


def _normalize_stock_code(stock_code: str) -> str:
    normalized = str(stock_code).strip().upper()
    if not _STOCK_CODE_RE.match(normalized):
        raise ValueError("stock_code must contain only letters and digits")
    return normalized.zfill(6)


def _visible_statement_type(value: str) -> str:
    statement_type = str(value).strip().upper()
    if statement_type == "CIS":
        return "IS"
    return statement_type


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


def _subtract_or_none(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


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


def _account_sort_key(
    catalog: dict[str, CanonicalAccount],
    statement_type: str,
    canonical_id: str,
) -> tuple[int, int, str]:
    preferred = PREFERRED_ACCOUNT_ORDER.get(statement_type, [])
    if canonical_id in preferred:
        return (0, preferred.index(canonical_id), canonical_id)
    account = catalog.get(canonical_id)
    return (1, account.order if account else 9999, canonical_id)


def _virtual_catalog_item(canonical_id: str, statement_type: str) -> CanonicalAccount:
    if canonical_id == DERIVED_FCF_ID:
        return CanonicalAccount(
            canonical_id=DERIVED_FCF_ID,
            account_name="잉여현금흐름(FCF)",
            statement_type="CF",
            is_derived=True,
            formula="CFO + CAPEX_PPE + CAPEX_INTANG",
            description="영업활동현금흐름에서 유형/무형자산 취득 지출을 반영한 잉여현금흐름",
        )
    return CanonicalAccount(
        canonical_id=canonical_id,
        account_name=canonical_id,
        statement_type=statement_type,
    )


def _has_any_numeric_value(account: FinancialAccountRow) -> bool:
    return any(cell.value is not None for cell in account.values)


def _first_value_currency(
    account_values: dict[str, PeriodValue],
    column_keys: list[str],
) -> str | None:
    for key in reversed(column_keys):
        if key in account_values and account_values[key].currency:
            return account_values[key].currency
    return None


def _first_present_currency(rows: list[RawStatementRow]) -> str | None:
    for row in reversed(rows):
        if row.currency:
            return row.currency
    return None


def _period_key(value: date) -> str:
    return value.isoformat()


def _period_end_date(year: int, month: int) -> date:
    if month <= 0:
        month = 12
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return next_month - timedelta(days=1)


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


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


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


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


def _format_statement_value(value: float | None, canonical_id: str) -> str:
    number = _clean_number(value)
    if number is None:
        return "N/A"
    if _is_per_share_account(canonical_id):
        return _format_value(number)
    return _format_value(_monetary_display_value(number))


def _monetary_display_value(value: float) -> float:
    if abs(value) < MONETARY_DISPLAY_SCALE:
        return value
    return value / MONETARY_DISPLAY_SCALE


def _statement_unit(canonical_id: str) -> str:
    if _is_per_share_account(canonical_id):
        return "KRW_PER_SHARE"
    return "KRW_MILLION"


def _is_per_share_account(canonical_id: str) -> bool:
    return canonical_id.strip().upper() in PER_SHARE_ACCOUNT_IDS


def _format_percent(value: float | None) -> str:
    number = _clean_number(value)
    if number is None:
        return "N/A"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.0f}%"
