from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Any, Callable

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.model.consensus import RealConsensusReport, RealConsensusReportsResponse
from api.service.operating_metrics_service import normalize_stock_code


class ConsensusReportsNotFoundError(ValueError):
    pass


CONSENSUS_REPORT_LOOKBACK_DAYS = 120


class ConsensusService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def get_kr_reports(self, stock_code: str) -> RealConsensusReportsResponse:
        stock_code = normalize_stock_code(stock_code)
        client = self._client_factory()
        try:
            frame = client.query_df(
                """
SELECT
    report_idx,
    coalesce(report_date, file_register_date) AS report_date,
    office_name AS broker_name,
    report_writer AS analyst_name,
    report_title,
    grade_value,
    old_grade_value,
    target_stock_prices AS target_price,
    old_target_stock_prices AS old_target_price,
    change_stock_prices AS change_price
FROM real_consensus_reports FINAL
WHERE stock_code = {stock_code:String}
ORDER BY report_date DESC, file_register_date DESC, report_idx DESC
""".strip(),
                parameters={"stock_code": stock_code},
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        if frame is None or frame.empty:
            raise ConsensusReportsNotFoundError(
                f"real consensus reports not found for stock_code={stock_code}"
            )

        records = sorted(
            frame.to_dict("records"),
            key=lambda row: _date_value(row.get("report_date")) or date.min,
            reverse=True,
        )
        latest_report_date = _date_value(records[0].get("report_date"))
        if latest_report_date is not None:
            cutoff_date = latest_report_date - timedelta(days=CONSENSUS_REPORT_LOOKBACK_DAYS)
            records = [
                record
                for record in records
                if (_date_value(record.get("report_date")) or date.min) >= cutoff_date
            ]
        reports = [_report(record) for record in records]
        target_prices = _latest_target_prices_by_analyst(records)
        average_target_price = (
            sum(target_prices) / len(target_prices) if target_prices else None
        )

        return RealConsensusReportsResponse(
            stock_code=stock_code,
            as_of_date=reports[0].report_date if reports else date.today(),
            average_target_price=average_target_price,
            target_price_analyst_count=len(target_prices),
            currency="KRW",
            reports=reports,
        )


def _report(row: dict[str, Any]) -> RealConsensusReport:
    return RealConsensusReport(
        report_date=_date_or_text(row.get("report_date")),
        broker_name=_text(row.get("broker_name")) or "-",
        analyst_name=_text(row.get("analyst_name")),
        report_title=_text(row.get("report_title")),
        grade_value=_text(row.get("grade_value")),
        old_grade_value=_text(row.get("old_grade_value")),
        target_price=_positive_float_or_none(row.get("target_price")),
        old_target_price=_positive_float_or_none(row.get("old_target_price")),
        change_price=_float_or_none(row.get("change_price")),
    )


def _latest_target_prices_by_analyst(records: list[dict[str, Any]]) -> list[float]:
    seen_analysts: set[str] = set()
    target_prices: list[float] = []

    for row in records:
        target_price = _positive_float_or_none(row.get("target_price"))
        if target_price is None:
            continue

        broker_name = _text(row.get("broker_name")).strip().casefold()
        analyst_name = _text(row.get("analyst_name")).strip().casefold()
        analyst_key = f"{broker_name}|{analyst_name}"
        if not broker_name and not analyst_name:
            analyst_key = f"report:{_text(row.get('report_idx'))}"
        if analyst_key in seen_analysts:
            continue

        seen_analysts.add(analyst_key)
        target_prices.append(target_price)

    return target_prices


def _date_or_text(value: Any) -> date | str:
    parsed = _date_value(value)
    if parsed is not None:
        return parsed
    return _text(value)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _float_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None
