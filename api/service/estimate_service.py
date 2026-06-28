from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.model.estimate import EstimateComponentRow, EstimateConsensusRow, EstimateResponse
from api.model.operating_metrics import OperatingMetricStock
from api.service.operating_metrics_service import normalize_stock_code
from engine.core.paths import DATA_LAKE


GOLD_ROOT = DATA_LAKE.root / "gold" / "estimates"


class EstimatesNotFoundError(ValueError):
    pass


class EstimateService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        gold_root: str | Path = GOLD_ROOT,
    ) -> None:
        self._client_factory = client_factory
        self._gold_root = Path(gold_root)

    def get_components(self, stock_code: str) -> EstimateResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_frame(stock_code, "arcana_estimate_component", "arcana_estimate_component.csv")
        if frame.empty:
            raise EstimatesNotFoundError(f"estimate components not found for stock_code={stock_code}")
        rows = [
            EstimateComponentRow(
                target_period=_text(row.get("target_period")),
                metric_id=_text(row.get("metric_id")),
                model_id=_text(row.get("model_id")),
                scenario=_text(row.get("scenario")),
                estimate_value=_float(row.get("estimate_value")),
                currency=_text(row.get("currency")) or "KRW",
                source_actual_period=_text(row.get("source_actual_period")),
                assumptions_json=_text(row.get("assumptions_json")),
                confidence=_float(row.get("confidence")),
                quality_flags=_text(row.get("quality_flags")),
                as_of_date=_date_or_text(row.get("as_of_date")),
            )
            for row in frame.to_dict("records")
        ]
        return EstimateResponse(_stock(stock_code), _as_of_date(frame), _target_period(frame), rows, source, warnings)

    def get_consensus(self, stock_code: str) -> EstimateResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_frame(stock_code, "arcana_estimate_consensus", "arcana_estimate_consensus.csv")
        if frame.empty:
            raise EstimatesNotFoundError(f"estimate consensus not found for stock_code={stock_code}")
        rows = [
            EstimateConsensusRow(
                target_period=_text(row.get("target_period")),
                metric_id=_text(row.get("metric_id")),
                scenario=_text(row.get("scenario")),
                consensus_mean=_float(row.get("consensus_mean")),
                consensus_median=_float(row.get("consensus_median")),
                consensus_low=_float(row.get("consensus_low")),
                consensus_high=_float(row.get("consensus_high")),
                model_count=int(_float(row.get("model_count")) or 0),
                confidence=_float(row.get("confidence")),
                dispersion=_float(row.get("dispersion")),
                currency=_text(row.get("currency")) or "KRW",
                as_of_date=_date_or_text(row.get("as_of_date")),
            )
            for row in frame.to_dict("records")
        ]
        return EstimateResponse(_stock(stock_code), _as_of_date(frame), _target_period(frame), rows, source, warnings)

    def _load_frame(self, stock_code: str, table_name: str, file_name: str) -> tuple[pd.DataFrame, str, list[str]]:
        warnings: list[str] = []
        client = None
        try:
            client = self._client_factory()
            frame = client.query_df(
                f"SELECT * FROM {table_name} WHERE stock_code = {{stock_code:String}} ORDER BY target_period, metric_id",
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


def _stock(stock_code: str) -> OperatingMetricStock:
    return OperatingMetricStock(stock_code=stock_code, security_id=f"SEC_KR_{stock_code}")


def _as_of_date(frame: pd.DataFrame) -> date | str:
    if "as_of_date" in frame:
        values = [_date_or_text(value) for value in frame["as_of_date"] if _text(value)]
        if values:
            return values[-1]
    return date.today()


def _target_period(frame: pd.DataFrame) -> str:
    if "target_period" not in frame or frame.empty:
        return ""
    return _text(frame.iloc[0].get("target_period"))


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
