from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.model.operating_metrics import (
    OperatingMetricDriverRow,
    OperatingMetricResponse,
    OperatingMetricRow,
    OperatingMetricStock,
    UnitEconomicsRow,
)
from engine.core.paths import DATA_LAKE


GOLD_ROOT = DATA_LAKE.root / "gold" / "operating-metrics"


class OperatingMetricsNotFoundError(ValueError):
    pass


class OperatingMetricsService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        gold_root: str | Path = GOLD_ROOT,
    ) -> None:
        self._client_factory = client_factory
        self._gold_root = Path(gold_root)

    def get_metrics(self, stock_code: str) -> OperatingMetricResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_frame(stock_code, "business_operating_metric", "business_operating_metric.csv")
        if frame.empty:
            raise OperatingMetricsNotFoundError(f"operating metrics not found for stock_code={stock_code}")
        rows = [
            OperatingMetricRow(
                fiscal_year=int(row.get("fiscal_year") or 0),
                fiscal_month=int(row.get("fiscal_month") or 0),
                period_end_date=_date_or_text(row.get("period_end_date")),
                segment_id=_text(row.get("segment_id")),
                segment_name=_text(row.get("segment_name")),
                product_id=_text(row.get("product_id")),
                product_name=_text(row.get("product_name")),
                metric_id=_text(row.get("metric_id")),
                metric_name=_text(row.get("metric_name")),
                metric_value=_float(row.get("metric_value")),
                metric_unit=_text(row.get("metric_unit")),
                value_type=_text(row.get("value_type")),
                source_type=_text(row.get("source_type")),
                confidence=_float(row.get("confidence")),
                quality_flags=_text(row.get("quality_flags")),
            )
            for row in frame.to_dict("records")
        ]
        return OperatingMetricResponse(
            stock=_stock(stock_code),
            as_of_date=_as_of_date(frame),
            rows=rows,
            source=source,
            warnings=warnings,
        )

    def get_unit_economics(self, stock_code: str) -> OperatingMetricResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_frame(stock_code, "business_unit_economics", "business_unit_economics.csv")
        if frame.empty:
            raise OperatingMetricsNotFoundError(f"unit economics not found for stock_code={stock_code}")
        rows = [
            UnitEconomicsRow(
                fiscal_year=int(row.get("fiscal_year") or 0),
                fiscal_month=int(row.get("fiscal_month") or 0),
                period_end_date=_date_or_text(row.get("period_end_date")),
                segment_id=_text(row.get("segment_id")),
                segment_name=_text(row.get("segment_name")),
                product_id=_text(row.get("product_id")),
                product_name=_text(row.get("product_name")),
                revenue=_float(row.get("revenue")),
                quantity=_float(row.get("quantity")),
                quantity_unit=_text(row.get("quantity_unit")),
                p=_float(row.get("p")),
                asp=_float(row.get("asp")),
                c=_float(row.get("c")),
                gross_profit=_float(row.get("gross_profit")),
                gross_margin=_float(row.get("gross_margin")),
                revenue_coverage_ratio=_float(row.get("revenue_coverage_ratio")),
                confidence=_float(row.get("confidence")),
                quality_flags=_text(row.get("quality_flags")),
            )
            for row in frame.to_dict("records")
        ]
        return OperatingMetricResponse(_stock(stock_code), _as_of_date(frame), rows, source, warnings)

    def get_drivers(self, stock_code: str) -> OperatingMetricResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_frame(
            stock_code,
            "business_unit_economics_driver",
            "business_unit_economics_driver.csv",
        )
        if frame.empty:
            raise OperatingMetricsNotFoundError(f"operating metric drivers not found for stock_code={stock_code}")
        rows = [
            OperatingMetricDriverRow(
                fiscal_year=int(row.get("fiscal_year") or 0),
                fiscal_month=int(row.get("fiscal_month") or 0),
                period_end_date=_date_or_text(row.get("period_end_date")),
                segment_id=_text(row.get("segment_id")),
                segment_name=_text(row.get("segment_name")),
                product_id=_text(row.get("product_id")),
                product_name=_text(row.get("product_name")),
                q_yoy_pct=_float(row.get("q_yoy_pct")),
                asp_yoy_pct=_float(row.get("asp_yoy_pct")),
                unit_cost_yoy_pct=_float(row.get("unit_cost_yoy_pct")),
                revenue_yoy_pct=_float(row.get("revenue_yoy_pct")),
                gross_margin_change_pctp=_float(row.get("gross_margin_change_pctp")),
            )
            for row in frame.to_dict("records")
        ]
        return OperatingMetricResponse(_stock(stock_code), _as_of_date(frame), rows, source, warnings)

    def _load_frame(self, stock_code: str, table_name: str, file_name: str) -> tuple[pd.DataFrame, str, list[str]]:
        warnings: list[str] = []
        client = None
        try:
            client = self._client_factory()
            frame = client.query_df(
                f"SELECT * FROM {table_name} WHERE stock_code = {{stock_code:String}} ORDER BY fiscal_year, fiscal_month",
                parameters={"stock_code": stock_code},
            )
            if frame is not None and not frame.empty:
                return frame, table_name, warnings
        except Exception as exc:
            warnings.append(f"clickhouse_fallback:{type(exc).__name__}")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        path = self._gold_root / stock_code / file_name
        if not path.exists():
            return pd.DataFrame(), "gold_csv", warnings
        return pd.read_csv(path), "gold_csv", warnings


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _stock(stock_code: str) -> OperatingMetricStock:
    return OperatingMetricStock(stock_code=stock_code, security_id=f"SEC_KR_{stock_code}")


def _as_of_date(frame: pd.DataFrame) -> date:
    if "created_at" in frame:
        dates = pd.to_datetime(frame["created_at"], errors="coerce").dropna()
        if not dates.empty:
            return dates.max().date()
    return date.today()


def _date_or_text(value: Any) -> date | str:
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return _text(value)


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
