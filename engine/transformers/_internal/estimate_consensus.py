from __future__ import annotations

import math
from typing import Any

import pandas as pd


CONSENSUS_COLUMNS = [
    "security_id",
    "stock_code",
    "target_period",
    "metric_id",
    "scenario",
    "consensus_mean",
    "consensus_median",
    "consensus_low",
    "consensus_high",
    "model_count",
    "confidence",
    "dispersion",
    "currency",
    "as_of_date",
]


def build_consensus(component_df: pd.DataFrame) -> pd.DataFrame:
    if component_df.empty:
        return pd.DataFrame(columns=CONSENSUS_COLUMNS)

    rows: list[dict[str, Any]] = []
    group_columns = ["security_id", "stock_code", "target_period", "metric_id", "scenario"]
    for key, group in component_df.groupby(group_columns, dropna=False):
        values = pd.to_numeric(group["estimate_value"], errors="coerce").dropna()
        if values.empty:
            continue
        mean_value = float(values.mean())
        dispersion = float(values.std(ddof=0) / abs(mean_value)) if len(values) > 1 and mean_value else 0.0
        confidence = pd.to_numeric(group.get("confidence"), errors="coerce").dropna()
        rows.append(
            {
                "security_id": key[0],
                "stock_code": key[1],
                "target_period": key[2],
                "metric_id": key[3],
                "scenario": key[4],
                "consensus_mean": mean_value,
                "consensus_median": float(values.median()),
                "consensus_low": float(values.quantile(0.25)),
                "consensus_high": float(values.quantile(0.75)),
                "model_count": int(len(values)),
                "confidence": _clean_float(confidence.mean() if not confidence.empty else None),
                "dispersion": dispersion,
                "currency": _first_text(group.get("currency")) or "KRW",
                "as_of_date": _first_text(group.get("as_of_date")) or "",
            }
        )
    return pd.DataFrame(rows, columns=CONSENSUS_COLUMNS)


def _first_text(series: Any) -> str | None:
    if series is None:
        return None
    for value in series:
        if value is not None and str(value).strip():
            return str(value)
    return None


def _clean_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result
