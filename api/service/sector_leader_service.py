from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from api.config.clickhouse import get_clickhouse_client
from api.model.sector_leader import (
    SectorLeaderMetric,
    SectorLeaderResponse,
    SectorLeaderRow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GICS_RULES_PATH = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "gics_rules.yaml"
FACTOR_TABLES = ("fact_daily_factor", "fact_daily_factors")
FACTOR_VALUE_COLUMNS = ("factor_value", "value")
DEFAULT_FINANCIAL_BASIS = "annual"
DEFAULT_NEAR_HIGH_PCT = 3.0
DEFAULT_SORT_BY = "strong_stock_ratio"
DEFAULT_LEVEL = "industry_group"
LEVELS = {"sector", "industry_group"}
SORTABLE_METRICS = {
    "strong_stock_ratio",
    "eps_expected_growth",
    "return_1d",
    "return_1w",
    "roe",
    "per",
    "pbr",
}
LOWER_BETTER_METRICS = {"per", "pbr"}
EPS_EXPECTED_GROWTH_CANDIDATES = [
    "eps_expected_growth",
    "expected_eps_growth",
    "eps_est_growth",
    "eps_estimate_growth",
    "eps_growth_estimate",
    "forward_eps_growth",
    "eps_forward_growth",
    "eps_fwd_growth",
    "consensus_eps_growth",
]
EPS_GROWTH_FALLBACK = "eps_yoy_pct"


class SectorLeaderService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
        gics_rules_path: Path = DEFAULT_GICS_RULES_PATH,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst
        self._gics_rules_path = gics_rules_path

    def get_sector_leaders(
        self,
        *,
        as_of_date: date | None = None,
        sort_by: str = DEFAULT_SORT_BY,
        direction: str | None = None,
        limit: int | None = None,
        near_high_pct: float = DEFAULT_NEAR_HIGH_PCT,
        financial_basis: str = DEFAULT_FINANCIAL_BASIS,
        level: str = DEFAULT_LEVEL,
    ) -> SectorLeaderResponse:
        as_of = as_of_date or self._today_factory()
        normalized_level = _normalize_level(level)
        normalized_sort_by = _normalize_sort_by(sort_by)
        normalized_direction = _normalize_direction(direction, normalized_sort_by)
        normalized_limit = _normalize_limit(limit)
        normalized_near_high_pct = _normalize_near_high_pct(near_high_pct)
        normalized_financial_basis = str(financial_basis or DEFAULT_FINANCIAL_BASIS).strip()
        if not normalized_financial_basis:
            normalized_financial_basis = DEFAULT_FINANCIAL_BASIS

        sector_names = self._load_classification_names(normalized_level)
        client = self._client_factory()
        try:
            eps_factor_id = _select_eps_growth_factor_id(client)
            rows, factor_source = _load_metric_rows(
                client,
                as_of_date=as_of,
                near_high_pct=normalized_near_high_pct,
                financial_basis=normalized_financial_basis,
                eps_factor_id=eps_factor_id,
                level=normalized_level,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        leader_rows = _build_rows(rows, sector_names=sector_names)
        leader_rows = _sort_rows(leader_rows, normalized_sort_by, normalized_direction)
        if normalized_limit is not None:
            leader_rows = leader_rows[:normalized_limit]
        leader_rows = [
            _with_rank(row, rank=index)
            for index, row in enumerate(leader_rows, start=1)
        ]

        return SectorLeaderResponse(
            as_of_date=as_of,
            level=normalized_level,
            sort_by=normalized_sort_by,
            direction=normalized_direction,
            near_high_pct=normalized_near_high_pct,
            financial_basis=normalized_financial_basis,
            factor_source=factor_source,
            eps_growth_factor_id=eps_factor_id,
            rows=leader_rows,
        )

    def _load_classification_names(self, level: str) -> dict[str, str]:
        with self._gics_rules_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        key = "industry_groups" if level == "industry_group" else "sectors"
        names = config.get(key, {})
        return {str(code): str(name) for code, name in names.items()}


def _select_eps_growth_factor_id(client: Any) -> str:
    try:
        rows = _records(
            client.query_df(
                """
SELECT factor_id
FROM factor_catalog
WHERE is_active
    AND has({candidate_factor_ids:Array(String)}, factor_id)
""".strip(),
                parameters={"candidate_factor_ids": EPS_EXPECTED_GROWTH_CANDIDATES},
            )
        )
    except Exception:
        rows = []

    available = {str(row["factor_id"]) for row in rows if row.get("factor_id") is not None}
    for factor_id in EPS_EXPECTED_GROWTH_CANDIDATES:
        if factor_id in available:
            return factor_id
    return EPS_GROWTH_FALLBACK


def _load_metric_rows(
    client: Any,
    *,
    as_of_date: date,
    near_high_pct: float,
    financial_basis: str,
    eps_factor_id: str,
    level: str,
) -> tuple[list[dict[str, Any]], str]:
    factor_ids = ["roe", "per", "pbr", eps_factor_id]
    last_source = FACTOR_TABLES[0]
    last_error: Exception | None = None
    fallback_rows: list[dict[str, Any]] | None = None
    fallback_source = last_source
    for table_name in FACTOR_TABLES:
        last_source = table_name
        for value_column in FACTOR_VALUE_COLUMNS:
            try:
                rows = _records(
                    client.query_df(
                        _build_sector_leader_query(
                            factor_table=table_name,
                            value_column=value_column,
                            level=level,
                        ),
                        parameters={
                            "as_of_date": as_of_date.isoformat(),
                            "near_high_ratio": 1 - (near_high_pct / 100),
                            "financial_basis": financial_basis,
                            "factor_ids": factor_ids,
                            "eps_factor_id": eps_factor_id,
                        },
                    )
                )
            except Exception as exc:
                last_error = exc
                continue
            if rows:
                if _has_any_factor_metric(rows):
                    return rows, table_name
                if fallback_rows is None:
                    fallback_rows = rows
                    fallback_source = table_name
        continue

    if fallback_rows is not None:
        return fallback_rows, fallback_source
    if last_error is not None:
        raise last_error
    return [], last_source


def _has_any_factor_metric(rows: list[dict[str, Any]]) -> bool:
    return any(
        _float_or_none(row.get(metric_id)) is not None
        for row in rows
        for metric_id in ("roe", "per", "pbr", "eps_expected_growth")
    )


def _build_sector_leader_query(
    *,
    factor_table: str,
    value_column: str,
    level: str = DEFAULT_LEVEL,
) -> str:
    if factor_table not in FACTOR_TABLES:
        raise ValueError(f"unsupported factor table: {factor_table}")
    if value_column not in FACTOR_VALUE_COLUMNS:
        raise ValueError(f"unsupported factor value column: {value_column}")
    normalized_level = _normalize_level(level)
    classification_column = (
        "iss.industry_group_code" if normalized_level == "industry_group" else "iss.sector_code"
    )

    return f"""
WITH
latest_market_date AS (
    SELECT max(trade_date) AS latest_trade_date
    FROM price_daily
    WHERE trade_date <= {{as_of_date:Date}}
),
universe AS (
    SELECT
        sm.security_id AS security_id,
        any({classification_column}) AS sector_code
    FROM security_master AS sm
    INNER JOIN issuers AS iss
        ON iss.issuer_id = sm.issuer_id
    WHERE sm.is_active
        AND iss.is_active
        AND iss.industry_schema = 'GICS'
        AND {classification_column} != ''
        AND {classification_column} != 'UNMAPPED'
        AND lowerUTF8(coalesce(iss.legal_name_en, '')) NOT LIKE '%special purpose acquisition%'
        AND lowerUTF8(coalesce(iss.legal_name_en, '')) NOT LIKE '%spac%'
    GROUP BY sm.security_id
),
ranked_price AS (
    SELECT
        security_id,
        trade_date,
        high,
        close,
        row_number() OVER (PARTITION BY security_id ORDER BY trade_date DESC) AS rn
    FROM price_daily
    WHERE trade_date < (SELECT latest_trade_date FROM latest_market_date)
),
latest_price AS (
    SELECT
        security_id,
        trade_date AS latest_trade_date,
        high AS latest_high,
        close AS latest_close
    FROM price_daily
    WHERE trade_date = (SELECT latest_trade_date FROM latest_market_date)
),
previous_price AS (
    SELECT
        security_id,
        close AS previous_close
    FROM ranked_price
    WHERE rn = 2
),
high_52w AS (
    SELECT
        security_id,
        max(high) AS prior_high_52w
    FROM price_daily
    WHERE trade_date >= (SELECT latest_trade_date FROM latest_market_date) - INTERVAL 365 DAY
        AND trade_date < (SELECT latest_trade_date FROM latest_market_date)
    GROUP BY security_id
),
week_price AS (
    SELECT
        security_id,
        argMax(close, trade_date) AS week_close
    FROM price_daily
    WHERE trade_date <= (SELECT latest_trade_date FROM latest_market_date) - INTERVAL 7 DAY
    GROUP BY security_id
),
stock_price_metrics AS (
    SELECT
        u.sector_code AS sector_code,
        u.security_id AS security_id,
        if(
            h.prior_high_52w IS NOT NULL
            AND h.prior_high_52w > 0
            AND lp.latest_close IS NOT NULL
            AND (
                lp.latest_high > h.prior_high_52w
                OR lp.latest_close >= h.prior_high_52w * {{near_high_ratio:Float64}}
            ),
            1,
            0
        ) AS strong_flag,
        if(
            pp.previous_close IS NULL OR pp.previous_close = 0,
            NULL,
            (lp.latest_close - pp.previous_close) / abs(pp.previous_close) * 100
        ) AS return_1d,
        if(
            wp.week_close IS NULL OR wp.week_close = 0,
            NULL,
            (lp.latest_close - wp.week_close) / abs(wp.week_close) * 100
        ) AS return_1w
    FROM universe AS u
    INNER JOIN latest_price AS lp
        ON lp.security_id = u.security_id
    LEFT JOIN previous_price AS pp
        ON pp.security_id = u.security_id
    LEFT JOIN high_52w AS h
        ON h.security_id = u.security_id
    LEFT JOIN week_price AS wp
        ON wp.security_id = u.security_id
),
price_metrics AS (
    SELECT
        sector_code,
        count() AS stock_count,
        countIf(strong_flag = 1) AS strong_stock_count,
        if(count() = 0, NULL, countIf(strong_flag = 1) / count() * 100) AS strong_stock_ratio,
        avg(return_1d) AS return_1d,
        avg(return_1w) AS return_1w
    FROM stock_price_metrics
    GROUP BY sector_code
),
latest_factors AS (
    SELECT
        f.security_id AS security_id,
        f.factor_id AS factor_id,
        argMax(f.{value_column}, tuple(f.trade_date, f.updated_at)) AS factor_value
    FROM {factor_table} AS f
    INNER JOIN universe AS u
        ON u.security_id = f.security_id
    WHERE f.trade_date <= {{as_of_date:Date}}
        AND f.financial_basis = {{financial_basis:String}}
        AND has({{factor_ids:Array(String)}}, f.factor_id)
        AND isFinite(f.{value_column})
    GROUP BY
        f.security_id,
        f.factor_id
),
factor_metrics AS (
    SELECT
        u.sector_code AS sector_code,
        avgIf(lf.factor_value, lf.factor_id = 'roe') AS roe,
        avgIf(lf.factor_value, lf.factor_id = 'per') AS per,
        avgIf(lf.factor_value, lf.factor_id = 'pbr') AS pbr,
        avgIf(lf.factor_value, lf.factor_id = {{eps_factor_id:String}}) AS eps_expected_growth
    FROM universe AS u
    LEFT JOIN latest_factors AS lf
        ON lf.security_id = u.security_id
    GROUP BY u.sector_code
)
SELECT
    coalesce(pm.sector_code, fm.sector_code) AS sector_code,
    any(pm.stock_count) AS stock_count,
    any(pm.strong_stock_count) AS strong_stock_count,
    any(pm.strong_stock_ratio) AS strong_stock_ratio,
    any(pm.return_1d) AS return_1d,
    any(pm.return_1w) AS return_1w,
    any(fm.roe) AS roe,
    any(fm.per) AS per,
    any(fm.pbr) AS pbr,
    any(fm.eps_expected_growth) AS eps_expected_growth
FROM price_metrics AS pm
FULL OUTER JOIN factor_metrics AS fm
    ON fm.sector_code = pm.sector_code
GROUP BY sector_code
""".strip()


def _build_rows(
    rows: list[dict[str, Any]],
    *,
    sector_names: dict[str, str],
) -> list[SectorLeaderRow]:
    by_sector = {
        str(row["sector_code"]): row
        for row in rows
        if row.get("sector_code") is not None and str(row.get("sector_code")).strip()
    }
    sector_codes = list(dict.fromkeys([*sector_names.keys(), *by_sector.keys()]))
    return [
        _to_row(
            sector_code=sector_code,
            sector_name=sector_names.get(sector_code, sector_code),
            row=by_sector.get(sector_code, {}),
        )
        for sector_code in sector_codes
    ]


def _to_row(
    *,
    sector_code: str,
    sector_name: str,
    row: dict[str, Any],
) -> SectorLeaderRow:
    return SectorLeaderRow(
        rank=0,
        sector_code=sector_code,
        sector_name=sector_name,
        stock_count=int(_float_or_none(row.get("stock_count")) or 0),
        strong_stock_count=int(_float_or_none(row.get("strong_stock_count")) or 0),
        strong_stock_ratio=_metric(row.get("strong_stock_ratio"), "percent"),
        eps_expected_growth=_metric(row.get("eps_expected_growth"), "percent"),
        return_1d=_metric(row.get("return_1d"), "percent"),
        return_1w=_metric(row.get("return_1w"), "percent"),
        roe=_metric(row.get("roe"), "percent"),
        per=_metric(row.get("per"), "times"),
        pbr=_metric(row.get("pbr"), "times"),
    )


def _with_rank(row: SectorLeaderRow, *, rank: int) -> SectorLeaderRow:
    return SectorLeaderRow(
        rank=rank,
        sector_code=row.sector_code,
        sector_name=row.sector_name,
        stock_count=row.stock_count,
        strong_stock_count=row.strong_stock_count,
        strong_stock_ratio=row.strong_stock_ratio,
        eps_expected_growth=row.eps_expected_growth,
        return_1d=row.return_1d,
        return_1w=row.return_1w,
        roe=row.roe,
        per=row.per,
        pbr=row.pbr,
    )


def _sort_rows(
    rows: list[SectorLeaderRow],
    sort_by: str,
    direction: str,
) -> list[SectorLeaderRow]:
    def metric_value(row: SectorLeaderRow) -> float | None:
        metric = getattr(row, sort_by)
        return metric.value if isinstance(metric, SectorLeaderMetric) else None

    valid_rows = [row for row in rows if metric_value(row) is not None]
    missing_rows = [row for row in rows if metric_value(row) is None]
    if direction == "desc":
        valid_rows.sort(key=lambda row: (-(metric_value(row) or 0), row.sector_name))
    else:
        valid_rows.sort(key=lambda row: (metric_value(row) or 0, row.sector_name))
    missing_rows.sort(key=lambda row: row.sector_name)
    return [*valid_rows, *missing_rows]


def _normalize_level(value: str) -> str:
    normalized = str(value or DEFAULT_LEVEL).strip()
    if normalized not in LEVELS:
        allowed = ", ".join(sorted(LEVELS))
        raise ValueError(f"level must be one of: {allowed}")
    return normalized


def _normalize_sort_by(value: str) -> str:
    normalized = str(value or DEFAULT_SORT_BY).strip()
    if normalized not in SORTABLE_METRICS:
        allowed = ", ".join(sorted(SORTABLE_METRICS))
        raise ValueError(f"sort_by must be one of: {allowed}")
    return normalized


def _normalize_direction(value: str | None, sort_by: str) -> str:
    if value is None:
        return "asc" if sort_by in LOWER_BETTER_METRICS else "desc"
    normalized = str(value).strip().lower()
    if normalized not in {"asc", "desc"}:
        raise ValueError("direction must be one of: asc, desc")
    return normalized


def _normalize_limit(value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if normalized <= 0:
        raise ValueError("limit must be a positive integer")
    return normalized


def _normalize_near_high_pct(value: float) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0 or normalized >= 100:
        raise ValueError("near_high_pct must be greater than or equal to 0 and less than 100")
    return normalized


def _metric(value: Any, unit: str) -> SectorLeaderMetric:
    number = _float_or_none(value)
    return SectorLeaderMetric(
        value=number,
        display_value=_format_value(number, unit),
    )


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "N/A"
    if unit == "percent":
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}%"
    if unit == "times":
        return f"{value:.2f}x"
    return f"{value:,.2f}"


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


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()
