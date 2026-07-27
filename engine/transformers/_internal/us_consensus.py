from __future__ import annotations

"""Normalize vendor-isolated US consensus bronze payloads and calculate raw factors."""

from datetime import date
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.identifiers import security_id_of
from engine.markets.registry import market_config
from engine.core.paths import DATA_LAKE
from engine.extractors._internal.us_consensus import BRONZE_US_CONSENSUS_DIR


SILVER_US_CONSENSUS_DIR = DATA_LAKE.silver("consensus", "us")
US_OBSERVATIONS_NAME = "us_consensus_observations.csv"
US_EVENTS_NAME = "us_consensus_events.csv"
US_FACTORS_NAME = "us_consensus_factors.csv"

US_OBSERVATION_COLUMNS = [
    "symbol", "security_id", "provider", "dataset", "source_regime", "snapshot_date",
    "availability_date", "horizon", "period_type", "fiscal_period_end", "forecast_slot",
    "metric", "statistic", "lookback_days", "value", "currency", "analyst_count", "raw_path",
]
US_EVENT_COLUMNS = [
    "symbol", "security_id", "provider", "source_regime", "event_type", "event_date",
    "fiscal_period_end", "reported_eps", "estimated_eps", "surprise_pct", "availability_date",
    "snapshot_date", "raw_path",
]
US_FACTOR_COLUMNS = [
    "symbol", "security_id", "factor_date", "provider", "source_regime", "horizon", "analyst_count",
    "us_eps_consensus", "us_revenue_consensus", "us_operating_income_consensus", "us_target_price", "us_eps_revision_7d_pct", "us_eps_revision_30d_pct",
    "us_eps_revision_60d_pct", "us_eps_revision_90d_pct", "us_eps_revision_breadth_30d_pct",
    "us_eps_revision_acceleration_30d_pct", "us_eps_dispersion_pct", "us_revenue_dispersion_pct",
    "us_eps_surprise_pct", "currency", "raw_path",
]


def normalize_us_consensus(
    *,
    bronze_dir: str | Path = BRONZE_US_CONSENSUS_DIR,
    output_dir: str | Path = SILVER_US_CONSENSUS_DIR,
) -> dict[str, Path | int]:
    observations, events, factors = build_us_consensus_frames(bronze_dir)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "observations_path": output / US_OBSERVATIONS_NAME,
        "events_path": output / US_EVENTS_NAME,
        "factors_path": output / US_FACTORS_NAME,
    }
    _write_csv(paths["observations_path"], observations, US_OBSERVATION_COLUMNS)
    _write_csv(paths["events_path"], events, US_EVENT_COLUMNS)
    _write_csv(paths["factors_path"], factors, US_FACTOR_COLUMNS)
    print(
        "[DONE] us consensus normalize "
        f"observations={len(observations):,}, events={len(events):,}, factors={len(factors):,}",
        flush=True,
    )
    return {**paths, "observations": len(observations), "events": len(events), "factors": len(factors)}


def build_us_consensus_frames(bronze_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    root = Path(bronze_dir)
    split_map = _load_splits(root / "alpha-vantage" / "splits")
    observations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    alpha_root = root / "alpha-vantage"
    for path in sorted(alpha_root.glob("earnings/snapshot_date=*/ticker=*.json")):
        payload = _read_json(path)
        symbol, snapshot = _path_identity(path)
        if not symbol or not payload:
            continue
        event_rows = _alpha_events(payload, symbol=symbol, snapshot=snapshot, raw_path=str(path))
        events.extend(event_rows)

    anchors = {
        (row["symbol"], row["fiscal_period_end"]): row["event_date"]
        for row in events
        if row["provider"] == "ALPHA_VANTAGE" and row["fiscal_period_end"] and row["event_date"]
    }
    surprises = {
        (row["symbol"], row["fiscal_period_end"]): row["surprise_pct"]
        for row in events
        if row["provider"] == "ALPHA_VANTAGE" and row["fiscal_period_end"]
    }
    for path in sorted(alpha_root.glob("earnings-estimates/snapshot_date=*/ticker=*.json")):
        payload = _read_json(path)
        symbol, snapshot = _path_identity(path)
        if not symbol or not payload:
            continue
        obs, rows = _alpha_estimates(
            payload,
            symbol=symbol,
            snapshot=snapshot,
            anchors=anchors,
            surprises=surprises,
            splits=split_map.get(symbol, []),
            raw_path=str(path),
        )
        observations.extend(obs)
        factors.extend(rows)

    for path in sorted((root / "yahoo").glob("snapshot_date=*/ticker=*.json")):
        payload = _read_json(path)
        symbol, snapshot = _path_identity(path)
        if not symbol or not payload:
            continue
        obs, event_rows, rows = _yahoo_frames(payload, symbol=symbol, snapshot=snapshot, raw_path=str(path))
        observations.extend(obs)
        events.extend(event_rows)
        factors.extend(rows)

    observation_frame = pd.DataFrame(observations, columns=US_OBSERVATION_COLUMNS)
    event_frame = pd.DataFrame(events, columns=US_EVENT_COLUMNS)
    factor_frame = pd.DataFrame(factors, columns=US_FACTOR_COLUMNS)
    return (
        observation_frame.sort_values(["symbol", "availability_date", "metric"], na_position="last").reset_index(drop=True)
        if not observation_frame.empty else observation_frame,
        event_frame.sort_values(["symbol", "event_date"], na_position="last").reset_index(drop=True)
        if not event_frame.empty else event_frame,
        factor_frame.sort_values(["symbol", "factor_date", "provider", "horizon"], na_position="last").reset_index(drop=True)
        if not factor_frame.empty else factor_frame,
    )


def _alpha_events(payload: dict[str, Any], *, symbol: str, snapshot: str, raw_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entries in (payload.get("quarterlyEarnings", []), payload.get("annualEarnings", [])):
        for entry in entries if isinstance(entries, list) else []:
            fiscal_end = _date_text(_pick(entry, "fiscalDateEnding", "date"))
            event_date = _date_text(_pick(entry, "reportedDate", "reportDate"))
            if not event_date:
                continue
            rows.append({
                "symbol": symbol, "security_id": _security_id(symbol), "provider": "ALPHA_VANTAGE",
                "source_regime": "ALPHA_VANTAGE_HISTORICAL", "event_type": "EARNINGS_RELEASE",
                "event_date": event_date, "fiscal_period_end": fiscal_end,
                "reported_eps": _number(_pick(entry, "reportedEPS")),
                "estimated_eps": _number(_pick(entry, "estimatedEPS")),
                "surprise_pct": _number(_pick(entry, "surprisePercentage")),
                "availability_date": _next_us_trading_day(event_date), "snapshot_date": snapshot, "raw_path": raw_path,
            })
    return rows


def _alpha_estimates(
    payload: dict[str, Any], *, symbol: str, snapshot: str, anchors: dict[tuple[str, str], str],
    surprises: dict[tuple[str, str], float | None],
    splits: list[tuple[pd.Timestamp, float]], raw_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    for entries, period_type, horizon in _alpha_estimate_groups(payload):
        for entry in entries:
            fiscal_end = _date_text(_pick(entry, "date", "fiscalDateEnding"))
            # Alpha Vantage does not provide an independent estimate as-of date. Only
            # records linked to an already reported quarter can be event-relative PIT.
            anchor = anchors.get((symbol, fiscal_end))
            if not anchor:
                continue
            availability = _next_us_trading_day(anchor)
            currency = _text(_pick(entry, "currency"))
            analysts = _number(_pick(entry, "eps_estimate_analyst_count", "eps_estimate_number_of_analysts", "number_of_analysts", "numberOfAnalysts"))
            values = {}
            for label, aliases, lookback in (
                ("current", ("eps_estimate_average", "epsEstimateAverage", "eps_average"), 0),
                ("7d", ("eps_estimate_average_7_days_ago", "average_7_days_ago"), 7),
                ("30d", ("eps_estimate_average_30_days_ago", "average_30_days_ago"), 30),
                ("60d", ("eps_estimate_average_60_days_ago", "average_60_days_ago"), 60),
                ("90d", ("eps_estimate_average_90_days_ago", "average_90_days_ago"), 90),
            ):
                raw = _number(_pick(entry, *aliases))
                value = _split_adjust(raw, pd.Timestamp(anchor) - pd.Timedelta(days=lookback), pd.Timestamp(anchor), splits)
                values[label] = value
                if value is not None:
                    observations.append(_observation(symbol, "ALPHA_VANTAGE", "EARNINGS_ESTIMATES", "ALPHA_VANTAGE_HISTORICAL", snapshot, availability, horizon, period_type, fiscal_end, "", "eps", "average", lookback, value, currency, analysts, raw_path))
            eps_high = _split_adjust(_number(_pick(entry, "eps_estimate_high", "epsEstimateHigh")), pd.Timestamp(anchor), pd.Timestamp(anchor), splits)
            eps_low = _split_adjust(_number(_pick(entry, "eps_estimate_low", "epsEstimateLow")), pd.Timestamp(anchor), pd.Timestamp(anchor), splits)
            revenue_current = _number(_pick(entry, "revenue_estimate_average", "revenueEstimateAverage"))
            revenue_high = _number(_pick(entry, "revenue_estimate_high", "revenueEstimateHigh"))
            revenue_low = _number(_pick(entry, "revenue_estimate_low", "revenueEstimateLow"))
            up = _number(_pick(entry, "eps_estimate_revision_up_trailing_30_days", "eps_estimate_revision_up", "revision_up", "upLast30days"))
            down = _number(_pick(entry, "eps_estimate_revision_down_trailing_30_days", "eps_estimate_revision_down", "revision_down", "downLast30days"))
            for metric, statistic, value in (("eps", "high", eps_high), ("eps", "low", eps_low), ("revenue", "average", revenue_current), ("revenue", "high", revenue_high), ("revenue", "low", revenue_low), ("eps_revision", "up", up), ("eps_revision", "down", down)):
                if value is not None:
                    observations.append(_observation(symbol, "ALPHA_VANTAGE", "EARNINGS_ESTIMATES", "ALPHA_VANTAGE_HISTORICAL", snapshot, availability, horizon, period_type, fiscal_end, "", metric, statistic, 30 if metric == "eps_revision" else 0, value, currency, analysts, raw_path))
            operating_income_current = _number(
                _pick(
                    entry,
                    "operating_income_estimate_average",
                    "operatingIncomeEstimateAverage",
                )
            )
            factors.append(_factor_row(symbol, availability, "ALPHA_VANTAGE", "ALPHA_VANTAGE_HISTORICAL", horizon, analysts, values, eps_high, eps_low, revenue_current, revenue_high, revenue_low, operating_income_current, up, down, surprises.get((symbol, fiscal_end)), currency, raw_path))
    return observations, factors


def _alpha_estimate_groups(payload: dict[str, Any]) -> list[tuple[list[dict[str, Any]], str, str]]:
    """Return actual Alpha Vantage estimates plus the legacy fixture schema."""
    actual_groups: dict[str, list[dict[str, Any]]] = {
        "fiscal quarter": [],
        "fiscal year": [],
    }
    actual_entries = payload.get("estimates", [])
    if isinstance(actual_entries, list):
        for entry in actual_entries:
            if not isinstance(entry, dict):
                continue
            key = _text(_pick(entry, "horizon")).lower()
            if key in actual_groups:
                actual_groups[key].append(entry)
    if any(actual_groups.values()):
        return [
            (actual_groups["fiscal quarter"], "fiscal_quarter", "FQ1"),
            (actual_groups["fiscal year"], "fiscal_year", "FY1"),
        ]

    # Retain the earlier normalized fixture/provider shape for compatibility.
    return [
        (entries, period_type, horizon)
        for entries, period_type, horizon in (
            (payload.get("quarterlyEstimates", []), "fiscal_quarter", "FQ1"),
            (payload.get("annualEstimates", []), "fiscal_year", "FY1"),
        )
        if isinstance(entries, list)
    ]


def _yahoo_frames(payload: dict[str, Any], *, symbol: str, snapshot: str, raw_path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    earnings = _frame(data.get("earnings_estimate"))
    revenue = _frame(data.get("revenue_estimate"))
    operating_income = _frame(data.get("operating_income_estimate"))
    trend = _frame(data.get("eps_trend"))
    revisions = _frame(data.get("eps_revisions"))
    history = _frame(data.get("earnings_history"))
    target_price = _yahoo_target_price(data.get("analyst_price_targets"))
    snapshot_day = _date_text(snapshot)
    observations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    surprise = _latest_yahoo_surprise(history, snapshot_day)
    for _, row in history.iterrows():
        event_date = _date_text(_row_value(row, "index", "date", "Earnings Date"))
        if not event_date:
            continue
        raw_surprise = _number(_row_value(row, "surprisePercent", "surprise_pct"))
        surprise_pct = raw_surprise * 100.0 if raw_surprise is not None and abs(raw_surprise) <= 1 else raw_surprise
        events.append({"symbol": symbol, "security_id": _security_id(symbol), "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "event_type": "EARNINGS_RELEASE", "event_date": event_date, "fiscal_period_end": "", "reported_eps": _number(_row_value(row, "epsActual")), "estimated_eps": _number(_row_value(row, "epsEstimate")), "surprise_pct": surprise_pct, "availability_date": snapshot_day, "snapshot_date": snapshot_day, "raw_path": raw_path})
    for slot, horizon in (("0q", "FQ1"), ("+1q", "FQ2"), ("0y", "FY1"), ("+1y", "FY2")):
        estimate = _row_for_slot(earnings, slot)
        revenue_row = _row_for_slot(revenue, slot)
        operating_income_row = _row_for_slot(operating_income, slot)
        trend_row = _row_for_slot(trend, slot)
        revision_row = _row_for_slot(revisions, slot)
        analysts = _number(_row_value(estimate, "numberOfAnalysts", "number_of_analysts"))
        currency = _text(_row_value(estimate, "currency")) or _text(_row_value(trend_row, "currency"))
        eps_current = _number(_row_value(trend_row, "current"))
        values = {"current": eps_current, "7d": _number(_row_value(trend_row, "7daysAgo")), "30d": _number(_row_value(trend_row, "30daysAgo")), "60d": _number(_row_value(trend_row, "60daysAgo")), "90d": _number(_row_value(trend_row, "90daysAgo"))}
        eps_high, eps_low = _number(_row_value(estimate, "high")), _number(_row_value(estimate, "low"))
        revenue_current = _number(_row_value(revenue_row, "avg"))
        operating_income_current = _number(_row_value(operating_income_row, "avg"))
        revenue_high, revenue_low = _number(_row_value(revenue_row, "high")), _number(_row_value(revenue_row, "low"))
        up = _number(_row_value(revision_row, "upLast30days", "upLast30Days"))
        down = _number(_row_value(revision_row, "downLast30days", "downLast30Days"))
        for metric, statistic, lookback, value in (("eps", "average", 0, eps_current), ("eps", "average", 7, values["7d"]), ("eps", "average", 30, values["30d"]), ("eps", "average", 60, values["60d"]), ("eps", "average", 90, values["90d"]), ("eps", "high", 0, eps_high), ("eps", "low", 0, eps_low), ("revenue", "average", 0, revenue_current), ("revenue", "high", 0, revenue_high), ("revenue", "low", 0, revenue_low), ("eps_revision", "up", 30, up), ("eps_revision", "down", 30, down)):
            if value is not None:
                observations.append(_observation(symbol, "YAHOO_FINANCE", "YAHOO_ANALYSIS", "YAHOO_CURRENT", snapshot_day, snapshot_day, horizon, "forward", "", slot, metric, statistic, lookback, value, currency, analysts, raw_path))
        if horizon == "FY1" and target_price is not None:
            observations.append(
                _observation(
                    symbol,
                    "YAHOO_FINANCE",
                    "YAHOO_ANALYSIS",
                    "YAHOO_CURRENT",
                    snapshot_day,
                    snapshot_day,
                    horizon,
                    "forward",
                    "",
                    slot,
                    "target_price",
                    "mean",
                    0,
                    target_price,
                    currency,
                    analysts,
                    raw_path,
                )
            )
        factors.append(_factor_row(symbol, snapshot_day, "YAHOO_FINANCE", "YAHOO_CURRENT", horizon, analysts, values, eps_high, eps_low, revenue_current, revenue_high, revenue_low, operating_income_current, up, down, surprise, currency, raw_path, target_price=target_price if horizon == "FY1" else None))
    return observations, events, factors


def _factor_row(symbol: str, factor_date: str, provider: str, regime: str, horizon: str, analysts: float | None, values: dict[str, float | None], eps_high: float | None, eps_low: float | None, revenue_current: float | None, revenue_high: float | None, revenue_low: float | None, operating_income_current: float | None, up: float | None, down: float | None, surprise: float | None, currency: str, raw_path: str, *, target_price: float | None = None) -> dict[str, Any]:
    revision = {days: _revision(values.get("current"), values.get(f"{days}d")) for days in (7, 30, 60, 90)}
    breadth = None if up is None or down is None else (up - down) / max(up + down, 1.0)
    prior_monthly = _revision(values.get("30d"), values.get("90d"))
    acceleration = None if revision[30] is None or prior_monthly is None else revision[30] - prior_monthly / 2.0
    return {
        "symbol": symbol, "security_id": _security_id(symbol), "factor_date": factor_date, "provider": provider,
        "source_regime": regime, "horizon": horizon, "analyst_count": analysts,
        "us_eps_consensus": values.get("current"), "us_revenue_consensus": revenue_current,
        "us_operating_income_consensus": operating_income_current,
        "us_target_price": target_price,
        "us_eps_revision_7d_pct": revision[7], "us_eps_revision_30d_pct": revision[30],
        "us_eps_revision_60d_pct": revision[60], "us_eps_revision_90d_pct": revision[90],
        "us_eps_revision_breadth_30d_pct": breadth,
        "us_eps_revision_acceleration_30d_pct": acceleration,
        "us_eps_dispersion_pct": _dispersion(eps_high, eps_low, values.get("current"), 0.1),
        "us_revenue_dispersion_pct": _dispersion(revenue_high, revenue_low, revenue_current, 1.0),
        "us_eps_surprise_pct": surprise, "currency": currency, "raw_path": raw_path,
    }


def _yahoo_target_price(value: Any) -> float | None:
    """Return Yahoo's mean analyst target price, falling back to the median."""
    if not isinstance(value, dict):
        return None
    for key in ("mean", "targetMeanPrice", "median", "targetMedianPrice"):
        target = _number(_pick(value, key))
        if target is not None and target > 0:
            return target
    return None


def _load_splits(root: Path) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    result: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for path in root.glob("snapshot_date=*/ticker=*.json"):
        payload = _read_json(path)
        symbol, _ = _path_identity(path)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        pairs = []
        for row in rows if isinstance(rows, list) else []:
            effective = pd.to_datetime(_pick(row, "effective_date", "effectiveDate"), errors="coerce")
            factor = _number(_pick(row, "split_factor", "splitFactor"))
            if pd.notna(effective) and factor is not None and factor > 0:
                pairs.append((effective, factor))
        if pairs:
            result[symbol] = sorted(pairs)
    return result


def _split_adjust(value: float | None, vintage: pd.Timestamp, anchor: pd.Timestamp, splits: list[tuple[pd.Timestamp, float]]) -> float | None:
    if value is None:
        return None
    product = math.prod(factor for effective, factor in splits if vintage < effective <= anchor)
    return value / product if product else value


def _revision(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    return 100.0 * (current - prior) / max(abs(prior), 0.1)


def _dispersion(high: float | None, low: float | None, average: float | None, floor: float) -> float | None:
    if high is None or low is None or average is None:
        return None
    return (high - low) / max(abs(average), floor)


def _latest_yahoo_surprise(history: pd.DataFrame, snapshot: str) -> float | None:
    if history.empty:
        return None
    rows = history.copy()
    rows["_event_date"] = pd.to_datetime(rows.apply(lambda row: _row_value(row, "index", "date", "Earnings Date"), axis=1), errors="coerce")
    rows = rows.loc[rows["_event_date"].notna() & (rows["_event_date"] <= pd.Timestamp(snapshot))]
    if rows.empty:
        return None
    row = rows.sort_values("_event_date").iloc[-1]
    if (pd.Timestamp(snapshot) - row["_event_date"]).days > 120:
        return None
    value = _number(_row_value(row, "surprisePercent", "surprise_pct"))
    return value * 100.0 if value is not None and abs(value) <= 1 else value


def _observation(symbol: str, provider: str, dataset: str, regime: str, snapshot: str, availability: str, horizon: str, period_type: str, fiscal_end: str, slot: str, metric: str, statistic: str, lookback: int, value: float, currency: str, analysts: float | None, raw_path: str) -> dict[str, Any]:
    return {"symbol": symbol, "security_id": _security_id(symbol), "provider": provider, "dataset": dataset, "source_regime": regime, "snapshot_date": snapshot, "availability_date": availability, "horizon": horizon, "period_type": period_type, "fiscal_period_end": fiscal_end, "forecast_slot": slot, "metric": metric, "statistic": statistic, "lookback_days": lookback, "value": value, "currency": currency, "analyst_count": analysts, "raw_path": raw_path}


def _frame(value: Any) -> pd.DataFrame:
    if not isinstance(value, dict) or value.get("kind") != "dataframe":
        return pd.DataFrame()
    return pd.DataFrame(value.get("records", []))


def _row_for_slot(frame: pd.DataFrame, slot: str) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=object)
    for column in ("period", "index", "Period"):
        if column in frame.columns:
            matched = frame.loc[frame[column].astype(str) == slot]
            if not matched.empty:
                return matched.iloc[-1]
    return pd.Series(dtype=object)


def _row_value(row: pd.Series, *names: str) -> Any:
    if row is None or row.empty:
        return None
    normalized = {_normal_key(column): column for column in row.index}
    for name in names:
        key = normalized.get(_normal_key(name))
        if key is not None:
            return row.get(key)
    return None


def _pick(row: Any, *names: str) -> Any:
    if not isinstance(row, dict):
        return None
    normalized = {_normal_key(key): key for key in row}
    for name in names:
        key = normalized.get(_normal_key(name))
        if key is not None:
            return row.get(key)
    return None


def _normal_key(value: Any) -> str:
    return "".join(character.lower() for character in str(value) if character.isalnum())


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _date_text(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.date().isoformat() if pd.notna(parsed) else ""


def _next_us_trading_day(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    try:  # exchange_calendars is a yfinance dependency in normal deployments.
        import exchange_calendars as xcals
        calendar = xcals.get_calendar("XNYS")
        session = calendar.date_to_session(parsed.date(), direction="next")
        if pd.Timestamp(session).date() <= parsed.date():
            session = calendar.next_session(session)
        return pd.Timestamp(session).date().isoformat()
    except Exception:
        return (parsed + pd.offsets.BDay(1)).date().isoformat()


def _path_identity(path: Path) -> tuple[str, str]:
    symbol = path.stem.split("ticker=", 1)[-1].upper() if "ticker=" in path.stem else path.stem.upper()
    snapshot = next((part.split("=", 1)[1] for part in path.parts if part.startswith("snapshot_date=")), "")
    return symbol, _date_text(snapshot)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _security_id(symbol: str) -> str:
    return security_id_of(symbol, market_config("us"))


def _write_csv(path: Path, frame: pd.DataFrame, columns: list[str]) -> None:
    result = frame.copy()
    for column in columns:
        if column not in result.columns:
            result[column] = pd.NA
    path.parent.mkdir(parents=True, exist_ok=True)
    result[columns].to_csv(path, index=False, encoding="utf-8-sig")
