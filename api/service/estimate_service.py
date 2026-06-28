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

    def get_consensus_history(
        self,
        stock_code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        metric_id: str | None = None,
        target_period: str | None = None,
    ) -> EstimateResponse:
        stock_code = normalize_stock_code(stock_code)
        frame, source, warnings = self._load_history_frame(
            stock_code,
            start_date=start_date,
            end_date=end_date,
            metric_id=metric_id,
            target_period=target_period,
        )
        if frame.empty:
            raise EstimatesNotFoundError(f"estimate consensus history not found for stock_code={stock_code}")
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

    def _load_history_frame(
        self,
        stock_code: str,
        *,
        start_date: date | None,
        end_date: date | None,
        metric_id: str | None,
        target_period: str | None,
    ) -> tuple[pd.DataFrame, str, list[str]]:
        warnings: list[str] = []
        client = None
        try:
            client = self._client_factory()
            where = ["stock_code = {stock_code:String}"]
            parameters: dict[str, Any] = {"stock_code": stock_code}
            if start_date is not None:
                where.append("as_of_date >= {start_date:Date}")
                parameters["start_date"] = start_date.isoformat()
            if end_date is not None:
                where.append("as_of_date <= {end_date:Date}")
                parameters["end_date"] = end_date.isoformat()
            if metric_id:
                where.append("metric_id = {metric_id:String}")
                parameters["metric_id"] = metric_id
            if target_period:
                where.append("target_period = {target_period:String}")
                parameters["target_period"] = target_period

            frame = client.query_df(
                "SELECT * FROM arcana_estimate_consensus_history "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY as_of_date, target_period, metric_id",
                parameters=parameters,
            )
            if frame is not None and not frame.empty:
                return frame, "arcana_estimate_consensus_history", warnings
        except Exception as exc:
            warnings.append(f"clickhouse_fallback:{type(exc).__name__}")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        frame = self._load_gold_history_frame(stock_code)
        frame = _filter_history_frame(
            frame,
            start_date=start_date,
            end_date=end_date,
            metric_id=metric_id,
            target_period=target_period,
        )
        return frame, "gold_csv_history", warnings

    def _load_gold_history_frame(self, stock_code: str) -> pd.DataFrame:
        history_dir = self._gold_root / stock_code / "history"
        if not history_dir.exists():
            return pd.DataFrame()
        frames = []
        for path in sorted(history_dir.glob("arcana_estimate_consensus_*.csv")):
            try:
                frames.append(pd.read_csv(path, dtype={"stock_code": str, "target_period": str}))
            except Exception:
                continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)


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


def _filter_history_frame(
    frame: pd.DataFrame,
    *,
    start_date: date | None,
    end_date: date | None,
    metric_id: str | None,
    target_period: str | None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    if "as_of_date" in result.columns:
        dates = pd.to_datetime(result["as_of_date"], errors="coerce").dt.date
        if start_date is not None:
            result = result[dates >= start_date]
            dates = pd.to_datetime(result["as_of_date"], errors="coerce").dt.date
        if end_date is not None:
            result = result[dates <= end_date]
    if metric_id and "metric_id" in result.columns:
        result = result[result["metric_id"].astype(str) == str(metric_id)]
    if target_period and "target_period" in result.columns:
        result = result[result["target_period"].astype(str) == str(target_period)]
    if {"as_of_date", "target_period", "metric_id"}.issubset(result.columns):
        result = result.sort_values(["as_of_date", "target_period", "metric_id"])
    return result


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
