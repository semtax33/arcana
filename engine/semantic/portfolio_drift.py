from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _mean(values: list[float]) -> float | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return float(sum(finite) / len(finite)) if finite else None


def _spearman(rows: pd.DataFrame, value_column: str) -> float | None:
    if (
        len(rows) < 3
        or rows[value_column].nunique(dropna=True) < 2
        or rows["forward_return"].nunique(dropna=True) < 2
    ):
        return None
    value = rows[value_column].corr(rows["forward_return"], method="spearman")
    return float(value) if pd.notna(value) and math.isfinite(float(value)) else None


def _membership_turnover(left: set[str], right: set[str]) -> float | None:
    target_size = max(len(left), len(right))
    if target_size == 0:
        return None
    return len(left.symmetric_difference(right)) / (2.0 * target_size)


def audit_portfolio_factor_drift(
    frame: pd.DataFrame,
    *,
    higher_is_better: bool = True,
) -> dict[str, Any]:
    """Compare old/new daily factor values through portfolio-facing outputs."""

    required = {
        "trade_date",
        "security_id",
        "old_value",
        "new_value",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"portfolio drift input missing columns: {missing}")
    rows = frame.copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"], errors="coerce")
    rows["old_value"] = pd.to_numeric(rows["old_value"], errors="coerce")
    rows["new_value"] = pd.to_numeric(rows["new_value"], errors="coerce")
    if "forward_return" in rows.columns:
        rows["forward_return"] = pd.to_numeric(rows["forward_return"], errors="coerce")
    rows = rows.dropna(subset=["trade_date", "security_id"])

    daily = []
    old_portfolios: list[tuple[pd.Timestamp, set[str]]] = []
    new_portfolios: list[tuple[pd.Timestamp, set[str]]] = []
    for trade_date, day in rows.groupby("trade_date", sort=True):
        day = day.copy()
        day["old_percentile"] = day["old_value"].rank(method="average", pct=True)
        day["new_percentile"] = day["new_value"].rank(method="average", pct=True)
        day["old_decile"] = (day["old_percentile"] * 10).apply(
            lambda value: min(10, max(1, int(-(-value // 1))))
            if pd.notna(value)
            else pd.NA
        ).astype("Int64")
        day["new_decile"] = (day["new_percentile"] * 10).apply(
            lambda value: min(10, max(1, int(-(-value // 1))))
            if pd.notna(value)
            else pd.NA
        ).astype("Int64")
        top_decile = 10 if higher_is_better else 1
        bottom_decile = 1 if higher_is_better else 10
        old_top = set(
            day.loc[
                day["old_decile"].eq(top_decile).fillna(False), "security_id"
            ].astype(str)
        )
        new_top = set(
            day.loc[
                day["new_decile"].eq(top_decile).fillna(False), "security_id"
            ].astype(str)
        )
        old_portfolios.append((trade_date, old_top))
        new_portfolios.append((trade_date, new_top))
        valid_pair = day["old_value"].notna() & day["new_value"].notna()
        value_changed = valid_pair & day["old_value"].ne(day["new_value"])
        percentile_changed = (
            day["old_percentile"].notna()
            & day["new_percentile"].notna()
            & day["old_percentile"].ne(day["new_percentile"])
        )
        valid_decile_pair = day["old_decile"].notna() & day["new_decile"].notna()
        decile_changed = pd.Series(False, index=day.index, dtype=bool)
        decile_changed.loc[valid_decile_pair] = day.loc[
            valid_decile_pair, "old_decile"
        ].astype("int64").ne(
            day.loc[valid_decile_pair, "new_decile"].astype("int64")
        )
        record: dict[str, Any] = {
            "trade_date": trade_date.strftime("%Y-%m-%d"),
            "security_count": int(day["security_id"].nunique()),
            "value_changed_cell_count": int(value_changed.sum()),
            "percentile_changed_cell_count": int(percentile_changed.sum()),
            "decile_changed_cell_count": int(decile_changed.sum()),
            "top_decile_membership_changed_cell_count": len(
                old_top.symmetric_difference(new_top)
            ),
            "cross_version_membership_turnover": _membership_turnover(
                old_top, new_top
            ),
            "old_top_count": len(old_top),
            "new_top_count": len(new_top),
        }
        if "forward_return" in day.columns:
            old_ic_rows = day.dropna(subset=["old_value", "forward_return"])
            new_ic_rows = day.dropna(subset=["new_value", "forward_return"])
            record["old_ic"] = _spearman(old_ic_rows, "old_value")
            record["new_ic"] = _spearman(new_ic_rows, "new_value")
            old_top_return = day.loc[
                day["old_decile"].eq(top_decile).fillna(False), "forward_return"
            ].mean()
            old_bottom_return = day.loc[
                day["old_decile"].eq(bottom_decile).fillna(False), "forward_return"
            ].mean()
            new_top_return = day.loc[
                day["new_decile"].eq(top_decile).fillna(False), "forward_return"
            ].mean()
            new_bottom_return = day.loc[
                day["new_decile"].eq(bottom_decile).fillna(False), "forward_return"
            ].mean()
            record["old_long_short_return"] = (
                float(old_top_return - old_bottom_return)
                if pd.notna(old_top_return) and pd.notna(old_bottom_return)
                else None
            )
            record["new_long_short_return"] = (
                float(new_top_return - new_bottom_return)
                if pd.notna(new_top_return) and pd.notna(new_bottom_return)
                else None
            )
        daily.append(record)

    def time_series_turnovers(portfolios):
        return [
            turnover
            for (_, previous), (_, current) in zip(portfolios, portfolios[1:])
            if (turnover := _membership_turnover(previous, current)) is not None
        ]

    old_ics = [row["old_ic"] for row in daily if row.get("old_ic") is not None]
    new_ics = [row["new_ic"] for row in daily if row.get("new_ic") is not None]
    old_spreads = [
        row["old_long_short_return"]
        for row in daily
        if row.get("old_long_short_return") is not None
    ]
    new_spreads = [
        row["new_long_short_return"]
        for row in daily
        if row.get("new_long_short_return") is not None
    ]
    cross_turnovers = [
        row["cross_version_membership_turnover"]
        for row in daily
        if row["cross_version_membership_turnover"] is not None
    ]
    old_turnovers = time_series_turnovers(old_portfolios)
    new_turnovers = time_series_turnovers(new_portfolios)
    old_ic_mean = _mean(old_ics)
    new_ic_mean = _mean(new_ics)
    old_spread_mean = _mean(old_spreads)
    new_spread_mean = _mean(new_spreads)
    return {
        "date_count": len(daily),
        "cell_count": int(len(rows)),
        "value_changed_cell_count": sum(
            row["value_changed_cell_count"] for row in daily
        ),
        "percentile_changed_cell_count": sum(
            row["percentile_changed_cell_count"] for row in daily
        ),
        "decile_changed_cell_count": sum(
            row["decile_changed_cell_count"] for row in daily
        ),
        "top_decile_membership_changed_cell_count": sum(
            row["top_decile_membership_changed_cell_count"] for row in daily
        ),
        "cross_version_membership_turnover_mean": _mean(cross_turnovers),
        "ic": {
            "old_mean": old_ic_mean,
            "new_mean": new_ic_mean,
            "delta": (
                new_ic_mean - old_ic_mean
                if old_ic_mean is not None and new_ic_mean is not None
                else None
            ),
        },
        "long_short_return": {
            "old_mean": old_spread_mean,
            "new_mean": new_spread_mean,
            "delta": (
                new_spread_mean - old_spread_mean
                if old_spread_mean is not None and new_spread_mean is not None
                else None
            ),
        },
        "time_series_turnover": {
            "old_mean": _mean(old_turnovers),
            "new_mean": _mean(new_turnovers),
        },
        "daily": daily,
    }
