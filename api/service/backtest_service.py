from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import math
from typing import Any, Callable

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.model.backtest import (
    BacktestAnnualReturn,
    BacktestEquityCurvePoint,
    BacktestPosition,
    BacktestRebalance,
    BacktestSummary,
    FactorBacktestResult,
)
from api.repository.backtest_query import (
    build_benchmark_history_query,
    build_factor_snapshot_query,
    build_price_history_query,
    build_trading_days_query,
)
from api.repository.factor_screen_query import FactorCondition
from api.service.dto import FactorBacktestRequestDto, FactorConditionDto
from api.service.factor_identity import canonical_factor_id
from api.service.style_score_catalog import (
    DEFAULT_FACTOR_SCREEN_STYLE_PROFILE,
    DEFAULT_SCREEN_STYLE_PROFILE,
    is_style_score_factor,
)


SURVIVOR_BIAS_WARNING = (
    "Survivor bias is not fully eliminated because delisted security history is not available. "
    "The backtest does not filter by current security_master.is_active and only uses securities "
    "with point-in-time factor and price data."
)


class BacktestService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def run_factor_backtest(self, request: FactorBacktestRequestDto) -> FactorBacktestResult:
        _validate_request(request)
        conditions = [_to_repository_condition(condition) for condition in request.conditions]
        style_profile = _resolve_style_profile(request.style_profile, conditions)
        benchmark_ids = _normalize_benchmark_ids(request.benchmarks)
        warnings = [SURVIVOR_BIAS_WARNING]

        client = self._client_factory()
        try:
            trading_days = self._load_trading_days(client, request.start_date, request.end_date)
            visible_days = [
                day for day in trading_days if request.start_date <= day <= request.end_date
            ]
            if not visible_days:
                raise ValueError("price trading days were not found in the requested period")

            rebalance_dates = _rebalance_dates(
                trading_days,
                start_date=request.start_date,
                end_date=request.end_date,
                frequency=request.rebalance_frequency,
            )
            if not rebalance_dates:
                raise ValueError("rebalance dates were not found in the requested period")

            equity_points, rebalance_history = self._run_strategy(
                client,
                conditions=conditions,
                trading_days=trading_days,
                rebalance_dates=rebalance_dates,
                end_date=request.end_date,
                market=request.market,
                financial_basis=request.financial_basis or "annual",
                style_profile=style_profile,
                sector_codes=request.sector_codes,
                industry_group_codes=request.industry_group_codes,
                match_mode=request.match_mode,
                max_positions=request.max_positions,
                transaction_cost_bps=float(request.transaction_cost_bps),
                warnings=warnings,
            )
            if not equity_points:
                equity_points = _flat_cash_equity_points(visible_days)
                warnings.append(
                    "A flat cash equity curve was returned because the strategy produced no "
                    "investable return points."
                )

            benchmark_navs = self._load_benchmark_navs(
                client,
                benchmark_ids=benchmark_ids,
                start_date=equity_points[0].trade_date if equity_points else request.start_date,
                end_date=equity_points[-1].trade_date if equity_points else request.end_date,
                warnings=warnings,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        equity_points = [
            BacktestEquityCurvePoint(
                trade_date=point.trade_date,
                strategy_nav=point.strategy_nav,
                benchmark_navs={
                    benchmark_id: benchmark_navs.get(benchmark_id, {}).get(point.trade_date)
                    for benchmark_id in benchmark_ids
                },
            )
            for point in equity_points
        ]
        summary = _summary(
            equity_points,
            start_date=request.start_date,
            end_date=request.end_date,
            rebalance_frequency=request.rebalance_frequency,
            rebalance_count=len(rebalance_history),
        )
        annual_returns = _annual_returns(equity_points, benchmark_ids)

        return FactorBacktestResult(
            summary=summary,
            equity_curve=equity_points,
            rebalance_history=rebalance_history,
            annual_returns=annual_returns,
            warnings=warnings,
        )

    def _load_trading_days(self, client: Any, start_date: date, end_date: date) -> list[date]:
        query, params = build_trading_days_query(start_date=start_date, end_date=end_date)
        rows = _records(client.query_df(query, parameters=params))
        return sorted({_as_date(row["trade_date"]) for row in rows})

    def _run_strategy(
        self,
        client: Any,
        *,
        conditions: list[FactorCondition],
        trading_days: list[date],
        rebalance_dates: list[date],
        end_date: date,
        market: str | None,
        financial_basis: str,
        style_profile: str,
        sector_codes: list[str] | None,
        industry_group_codes: list[str] | None,
        match_mode: str,
        max_positions: int | None,
        transaction_cost_bps: float,
        warnings: list[str],
    ) -> tuple[list[BacktestEquityCurvePoint], list[BacktestRebalance]]:
        nav = 1.0
        equity_points: list[BacktestEquityCurvePoint] = []
        rebalance_history: list[BacktestRebalance] = []
        planned_segments: list[dict[str, Any]] = []
        selected_security_ids: set[str] = set()
        previous_positions_by_id: dict[str, BacktestPosition] = {}

        for index, rebalance_date in enumerate(rebalance_dates):
            signal_date = _previous_trading_day(trading_days, rebalance_date)
            if signal_date is None:
                warnings.append(f"Skipped rebalance {rebalance_date.isoformat()}: no prior signal date.")
                continue

            snapshot_rows = self._load_factor_snapshot(
                client,
                conditions=conditions,
                signal_date=signal_date,
                market=market,
                financial_basis=financial_basis,
                style_profile=style_profile,
                sector_codes=sector_codes,
                industry_group_codes=industry_group_codes,
            )
            candidates = _select_candidates(
                snapshot_rows,
                conditions=conditions,
                match_mode=match_mode,
            )
            if max_positions is not None:
                candidates = candidates[:max_positions]

            positions = _positions_from_candidates(candidates)
            current_positions_by_id = {position.security_id: position for position in positions}
            entered_positions = [
                position
                for position in positions
                if position.security_id not in previous_positions_by_id
            ]
            exited_positions = [
                position
                for security_id, position in previous_positions_by_id.items()
                if security_id not in current_positions_by_id
            ]
            previous_positions_by_id = current_positions_by_id
            rebalance_history.append(
                BacktestRebalance(
                    rebalance_date=rebalance_date,
                    signal_date=signal_date,
                    positions=positions,
                    entered_positions=entered_positions,
                    exited_positions=exited_positions,
                )
            )

            if not positions:
                warnings.append(
                    f"No positions selected for rebalance {rebalance_date.isoformat()}."
                )
                continue

            period_end = _segment_end_date(
                trading_days,
                rebalance_dates,
                current_index=index,
                final_end_date=end_date,
            )
            segment_security_ids = [position.security_id for position in positions]
            selected_security_ids.update(segment_security_ids)
            planned_segments.append(
                {
                    "security_ids": segment_security_ids,
                    "start_date": rebalance_date,
                    "end_date": period_end,
                    "transaction_cost_bps": transaction_cost_bps,
                }
            )

        if not planned_segments:
            warnings.append(
                _no_positions_error_message(
                    client,
                    conditions=conditions,
                    style_profile=style_profile,
                    rebalance_dates=rebalance_dates,
                    warnings=warnings,
                )
            )
            return [], rebalance_history

        price_returns = self._load_price_return_matrix(
            client,
            security_ids=sorted(selected_security_ids),
            start_date=min(segment["start_date"] for segment in planned_segments),
            end_date=max(segment["end_date"] for segment in planned_segments),
        )

        for segment in planned_segments:
            segment_returns = _segment_returns_from_matrix(
                price_returns,
                security_ids=segment["security_ids"],
                start_date=segment["start_date"],
                end_date=segment["end_date"],
                transaction_cost_bps=segment["transaction_cost_bps"],
            )
            for trade_date, daily_return in segment_returns:
                nav *= 1 + daily_return
                equity_points.append(
                    BacktestEquityCurvePoint(
                        trade_date=trade_date,
                        strategy_nav=nav,
                    )
                )

        if not equity_points:
            warnings.append(
                "backtest produced no equity curve points because selected securities had no "
                "usable price returns in the requested holding periods"
            )
            return [], rebalance_history

        return _deduplicate_equity_points(equity_points), rebalance_history

    def _load_factor_snapshot(
        self,
        client: Any,
        *,
        conditions: list[FactorCondition],
        signal_date: date,
        market: str | None,
        financial_basis: str,
        style_profile: str,
        sector_codes: list[str] | None,
        industry_group_codes: list[str] | None,
    ) -> list[dict[str, Any]]:
        query, params = build_factor_snapshot_query(
            conditions,
            signal_date=signal_date,
            market=market,
            financial_basis=financial_basis,
            style_profile=style_profile,
            sector_codes=sector_codes,
            industry_group_codes=industry_group_codes,
        )
        return _records(client.query_df(query, parameters=params))

    def _load_price_return_matrix(
        self,
        client: Any,
        *,
        security_ids: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        query, params = build_price_history_query(
            security_ids=security_ids,
            start_date=start_date,
            end_date=end_date,
        )
        rows = _records(client.query_df(query, parameters=params))
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        pivot = (
            frame.pivot_table(
                index="trade_date",
                columns="security_id",
                values="close",
                aggfunc="last",
            )
            .sort_index()
            .dropna(how="all")
        )
        if pivot.empty:
            return pd.DataFrame()
        return pivot.pct_change(fill_method=None)

    def _load_benchmark_navs(
        self,
        client: Any,
        *,
        benchmark_ids: list[str],
        start_date: date,
        end_date: date,
        warnings: list[str],
    ) -> dict[str, dict[date, float]]:
        if not benchmark_ids:
            return {}
        try:
            query, params = build_benchmark_history_query(
                benchmark_ids=benchmark_ids,
                start_date=start_date,
                end_date=end_date,
            )
            rows = _records(client.query_df(query, parameters=params))
        except Exception as exc:
            warnings.append(f"Benchmark data could not be loaded: {exc}")
            return {benchmark_id: {} for benchmark_id in benchmark_ids}

        result: dict[str, dict[date, float]] = {benchmark_id: {} for benchmark_id in benchmark_ids}
        if not rows:
            warnings.append("Benchmark data was not found for the requested period.")
            return result

        frame = pd.DataFrame(rows)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        frame["benchmark_id"] = frame["benchmark_id"].astype(str).str.upper()
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")

        for benchmark_id, rows_by_id in frame.groupby("benchmark_id", sort=False):
            prices = rows_by_id.sort_values("trade_date").dropna(subset=["close"])
            if prices.empty:
                continue
            base = float(prices["close"].iat[0])
            if base == 0:
                continue
            result[benchmark_id] = {
                _as_date(row.trade_date): float(row.close) / base
                for row in prices.itertuples(index=False)
            }

        missing = [benchmark_id for benchmark_id in benchmark_ids if not result.get(benchmark_id)]
        if missing:
            warnings.append(f"Benchmark data missing for: {', '.join(missing)}")
        return result


def _to_repository_condition(condition: FactorConditionDto) -> FactorCondition:
    data = condition.model_dump() if hasattr(condition, "model_dump") else condition.dict()
    data["factor_id"] = canonical_factor_id(str(data["factor_id"]))
    return FactorCondition(**data)


def _resolve_style_profile(
    requested_profile: str | None,
    conditions: list[FactorCondition],
) -> str | None:
    if not any(is_style_score_factor(condition.factor_id) for condition in conditions):
        return requested_profile

    profile = str(requested_profile or "").strip().upper()
    if not profile or profile == DEFAULT_SCREEN_STYLE_PROFILE:
        return DEFAULT_FACTOR_SCREEN_STYLE_PROFILE
    return profile


def _validate_request(request: FactorBacktestRequestDto) -> None:
    if request.start_date >= request.end_date:
        raise ValueError("start_date must be earlier than end_date")
    if request.match_mode not in {"all", "any"}:
        raise ValueError("match_mode must be 'all' or 'any'")
    if request.transaction_cost_bps < 0:
        raise ValueError("transaction_cost_bps must be greater than or equal to 0")


def _no_positions_error_message(
    client: Any,
    *,
    conditions: list[FactorCondition],
    style_profile: str,
    rebalance_dates: list[date],
    warnings: list[str],
) -> str:
    base = (
        "backtest selected no positions for any rebalance date; "
        "no equity curve can be produced"
    )
    if not any(is_style_score_factor(condition.factor_id) for condition in conditions):
        return base

    start, end, count = _load_style_score_date_range(client, style_profile)
    if start is None or end is None or count == 0:
        return (
            f"{base}. Style score data was not found for style_profile={style_profile}. "
            "Build style scores before running a style-score backtest."
        )

    first_rebalance = min(rebalance_dates) if rebalance_dates else None
    last_rebalance = max(rebalance_dates) if rebalance_dates else None
    warning_text = " ".join(warnings)
    return (
        f"{base}. Style score data for style_profile={style_profile} is available only from "
        f"{start.isoformat()} to {end.isoformat()} ({count} rows). "
        f"Requested rebalance dates run from "
        f"{first_rebalance.isoformat() if first_rebalance else 'N/A'} to "
        f"{last_rebalance.isoformat() if last_rebalance else 'N/A'}. "
        "Choose a backtest period with signal dates on or after the first style-score date, "
        "or build historical style scores for the requested period."
        + (f" Details: {warning_text}" if warning_text else "")
    )


def _load_style_score_date_range(
    client: Any,
    style_profile: str,
) -> tuple[date | None, date | None, int]:
    try:
        rows = _records(
            client.query_df(
                """
SELECT
    nullIf(min(trade_date), toDate(0)) AS min_trade_date,
    nullIf(max(trade_date), toDate(0)) AS max_trade_date,
    count() AS row_count
FROM arcana.fact_daily_style_score FINAL
WHERE style_profile = {style_profile:String}
""".strip(),
                parameters={"style_profile": style_profile},
            )
        )
    except Exception:
        return None, None, 0
    if not rows:
        return None, None, 0
    row = rows[0]
    row_count = int(_float_or_none(row.get("row_count")) or 0)
    start = _as_date(row["min_trade_date"]) if row.get("min_trade_date") is not None else None
    end = _as_date(row["max_trade_date"]) if row.get("max_trade_date") is not None else None
    return start, end, row_count


def _rebalance_dates(
    trading_days: list[date],
    *,
    start_date: date,
    end_date: date,
    frequency: str,
) -> list[date]:
    visible_days = [day for day in trading_days if start_date <= day <= end_date]
    if not visible_days:
        return []

    dates = [visible_days[0]]
    for boundary in _period_boundaries(start_date, end_date, frequency):
        execution_date = _first_trading_day_on_or_after(trading_days, boundary + timedelta(days=1))
        if execution_date is None or execution_date < start_date or execution_date > end_date:
            continue
        if execution_date not in dates:
            dates.append(execution_date)
    return sorted(dates)


def _period_boundaries(start_date: date, end_date: date, frequency: str) -> list[date]:
    months_by_frequency = {
        "quarterly": {3, 6, 9, 12},
        "semiannual": {6, 12},
        "annual": {12},
    }
    if frequency not in months_by_frequency:
        raise ValueError("rebalance_frequency must be one of: quarterly, semiannual, annual")

    boundaries: list[date] = []
    year = start_date.year
    while year <= end_date.year:
        for month in sorted(months_by_frequency[frequency]):
            boundary = _month_end(year, month)
            if start_date <= boundary <= end_date:
                boundaries.append(boundary)
        year += 1
    return boundaries


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _first_trading_day_on_or_after(trading_days: list[date], value: date) -> date | None:
    for trading_day in trading_days:
        if trading_day >= value:
            return trading_day
    return None


def _previous_trading_day(trading_days: list[date], value: date) -> date | None:
    previous = [trading_day for trading_day in trading_days if trading_day < value]
    return previous[-1] if previous else None


def _segment_end_date(
    trading_days: list[date],
    rebalance_dates: list[date],
    *,
    current_index: int,
    final_end_date: date,
) -> date:
    if current_index + 1 >= len(rebalance_dates):
        eligible = [day for day in trading_days if day <= final_end_date]
        return eligible[-1] if eligible else final_end_date

    next_rebalance_date = rebalance_dates[current_index + 1]
    eligible = [day for day in trading_days if day < next_rebalance_date]
    return eligible[-1] if eligible else next_rebalance_date


def _select_candidates(
    rows: list[dict[str, Any]],
    *,
    conditions: list[FactorCondition],
    match_mode: str,
) -> list[dict[str, Any]]:
    by_security: dict[str, dict[str, Any]] = {}
    for row in rows:
        security_id = str(row["security_id"])
        factor_id = str(row["factor_id"])
        item = by_security.setdefault(
            security_id,
            {
                "security_id": security_id,
                "ticker": _optional_str(row.get("ticker")),
                "stock_name": _optional_str(row.get("stock_name")),
                "factor_values": {},
                "factor_scores": {},
                "factor_rows": {},
            },
        )
        item["factor_values"][factor_id] = _float_or_none(row.get("factor_value"))
        item["factor_scores"][factor_id] = _float_or_none(row.get("percentile_score"))
        item["factor_rows"][factor_id] = row

    candidates = []
    required_count = len(conditions) if match_mode == "all" else 1
    for item in by_security.values():
        matched_count = 0
        for condition in conditions:
            row = item["factor_rows"].get(condition.factor_id)
            if row is not None and _condition_matches(condition, row):
                matched_count += 1

        if matched_count < required_count:
            continue

        scores = [
            item["factor_scores"].get(condition.factor_id)
            for condition in conditions
            if item["factor_scores"].get(condition.factor_id) is not None
        ]
        item["score"] = sum(scores) / len(scores) if scores else None
        item["matched_condition_count"] = matched_count
        candidates.append(item)

    return sorted(
        candidates,
        key=lambda item: (
            -(item["score"] if item["score"] is not None else -math.inf),
            item["security_id"],
        ),
    )


def _condition_matches(condition: FactorCondition, row: dict[str, Any]) -> bool:
    value = _float_or_none(row.get("factor_value"))
    if value is None:
        return False

    if condition.mode == "top_percent":
        top_percent = float(condition.top_percent or 0)
        if top_percent <= 0 or top_percent > 100:
            raise ValueError("top_percent must be > 0 and <= 100")
        factor_count = int(row.get("factor_count") or 0)
        if factor_count <= 0:
            return False
        top_n = max(1, math.ceil(factor_count * (top_percent / 100.0)))
        rank = _rank_for_condition(condition, row)
        return rank is not None and rank <= top_n

    if condition.mode == "threshold":
        operator = (condition.operator or "").lower()
        if operator == "between":
            if condition.min_value is None or condition.max_value is None:
                raise ValueError("between threshold requires min_value and max_value")
            return float(condition.min_value) <= value <= float(condition.max_value)

        if condition.value is None:
            raise ValueError("threshold condition requires value")
        threshold = float(condition.value)
        if operator in {">", "gt"}:
            return value > threshold
        if operator in {">=", "gte"}:
            return value >= threshold
        if operator in {"<", "lt"}:
            return value < threshold
        if operator in {"<=", "lte"}:
            return value <= threshold
        if operator in {"=", "==", "eq"}:
            return value == threshold
        if operator in {"!=", "<>", "ne"}:
            return value != threshold
        raise ValueError("unsupported threshold operator")

    raise ValueError("condition mode must be 'top_percent' or 'threshold'")


def _rank_for_condition(condition: FactorCondition, row: dict[str, Any]) -> int | None:
    side = condition.percentile_side
    if side not in {"top", "bottom"}:
        raise ValueError("percentile_side must be 'top' or 'bottom'")
    if condition.rank_direction == "higher":
        return _int_or_none(row.get("rank_high" if side == "top" else "rank_low"))
    if condition.rank_direction == "lower":
        return _int_or_none(row.get("rank_low" if side == "top" else "rank_high"))
    if condition.rank_direction == "catalog":
        if str(row.get("value_direction") or "") == "LOWER_BETTER":
            return _int_or_none(row.get("rank_low" if side == "top" else "rank_high"))
        return _int_or_none(row.get("rank_high" if side == "top" else "rank_low"))
    raise ValueError("rank_direction must be 'catalog', 'higher', or 'lower'")


def _positions_from_candidates(candidates: list[dict[str, Any]]) -> list[BacktestPosition]:
    if not candidates:
        return []
    weight = 1.0 / len(candidates)
    return [
        BacktestPosition(
            security_id=str(candidate["security_id"]),
            ticker=candidate.get("ticker"),
            stock_name=candidate.get("stock_name"),
            weight=weight,
            score=_float_or_none(candidate.get("score")),
            factor_values=dict(candidate.get("factor_values") or {}),
        )
        for candidate in candidates
    ]


def _segment_returns_from_matrix(
    price_returns: pd.DataFrame,
    *,
    security_ids: list[str],
    start_date: date,
    end_date: date,
    transaction_cost_bps: float,
) -> list[tuple[date, float]]:
    if price_returns.empty or not security_ids:
        return []

    columns = [security_id for security_id in security_ids if security_id in price_returns.columns]
    if not columns:
        return []

    segment = price_returns.loc[
        (price_returns.index >= start_date) & (price_returns.index <= end_date),
        columns,
    ]
    if segment.empty:
        return []

    daily_returns: list[tuple[date, float]] = []
    cost = transaction_cost_bps / 10_000.0
    for offset, trade_date in enumerate(segment.index):
        row = segment.loc[trade_date]
        valid = row.replace([math.inf, -math.inf], math.nan).dropna()
        daily_return = float(valid.mean()) if not valid.empty else 0.0
        if offset == 0 and cost:
            daily_return -= cost
        daily_returns.append((_as_date(trade_date), daily_return))
    return daily_returns


def _deduplicate_equity_points(
    points: list[BacktestEquityCurvePoint],
) -> list[BacktestEquityCurvePoint]:
    by_date = {point.trade_date: point for point in points}
    return [by_date[trade_date] for trade_date in sorted(by_date)]


def _flat_cash_equity_points(trading_days: list[date]) -> list[BacktestEquityCurvePoint]:
    return [
        BacktestEquityCurvePoint(trade_date=trade_date, strategy_nav=1.0)
        for trade_date in trading_days
    ]


def _summary(
    points: list[BacktestEquityCurvePoint],
    *,
    start_date: date,
    end_date: date,
    rebalance_frequency: str,
    rebalance_count: int,
) -> BacktestSummary:
    navs = [point.strategy_nav for point in points]
    dates = [point.trade_date for point in points]
    returns = _daily_returns(navs)
    cumulative_return = navs[-1] - 1 if navs else None
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25) if len(dates) >= 2 else None
    cagr = navs[-1] ** (1 / years) - 1 if years else None
    max_drawdown = _max_drawdown(navs)
    volatility = _stddev(returns) * math.sqrt(252) if len(returns) >= 2 else None
    average_return = sum(returns) / len(returns) if returns else None
    sharpe = (
        average_return / _stddev(returns) * math.sqrt(252)
        if average_return is not None and len(returns) >= 2 and _stddev(returns) > 0
        else None
    )
    non_zero_returns = [value for value in returns if value != 0]
    win_rate = (
        sum(1 for value in non_zero_returns if value > 0) / len(non_zero_returns)
        if non_zero_returns
        else None
    )
    return BacktestSummary(
        start_date=start_date,
        end_date=end_date,
        rebalance_frequency=rebalance_frequency,
        cumulative_return=_clean_number(cumulative_return),
        cagr=_clean_number(cagr),
        max_drawdown=_clean_number(max_drawdown),
        volatility=_clean_number(volatility),
        sharpe=_clean_number(sharpe),
        win_rate=_clean_number(win_rate),
        rebalance_count=rebalance_count,
    )


def _annual_returns(
    points: list[BacktestEquityCurvePoint],
    benchmark_ids: list[str],
) -> list[BacktestAnnualReturn]:
    by_year: dict[int, list[BacktestEquityCurvePoint]] = defaultdict(list)
    for point in points:
        by_year[point.trade_date.year].append(point)

    result = []
    previous_strategy_nav: float | None = None
    previous_benchmark_navs: dict[str, float | None] = {benchmark_id: None for benchmark_id in benchmark_ids}
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda point: point.trade_date)
        first_strategy = previous_strategy_nav if previous_strategy_nav is not None else 1.0
        last_strategy = rows[-1].strategy_nav
        strategy_return = last_strategy / first_strategy - 1 if first_strategy else None

        benchmark_returns: dict[str, float | None] = {}
        excess_returns: dict[str, float | None] = {}
        for benchmark_id in benchmark_ids:
            first_benchmark = previous_benchmark_navs.get(benchmark_id)
            if first_benchmark is None:
                first_benchmark = _first_present_benchmark(rows, benchmark_id)
            last_benchmark = _last_present_benchmark(rows, benchmark_id)
            benchmark_return = (
                last_benchmark / first_benchmark - 1
                if first_benchmark not in {None, 0} and last_benchmark is not None
                else None
            )
            benchmark_returns[benchmark_id] = _clean_number(benchmark_return)
            excess_returns[benchmark_id] = (
                _clean_number(strategy_return - benchmark_return)
                if strategy_return is not None and benchmark_return is not None
                else None
            )
            previous_benchmark_navs[benchmark_id] = last_benchmark

        result.append(
            BacktestAnnualReturn(
                year=year,
                strategy_return=_clean_number(strategy_return),
                benchmark_returns=benchmark_returns,
                excess_returns=excess_returns,
            )
        )
        previous_strategy_nav = last_strategy
    return result


def _daily_returns(navs: list[float]) -> list[float]:
    return [
        navs[index] / navs[index - 1] - 1
        for index in range(1, len(navs))
        if navs[index - 1] != 0
    ]


def _max_drawdown(navs: list[float]) -> float | None:
    if not navs:
        return None
    peak = navs[0]
    drawdowns = []
    for nav in navs:
        peak = max(peak, nav)
        drawdowns.append(nav / peak - 1 if peak else 0)
    return min(drawdowns) if drawdowns else None


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _first_present_benchmark(rows: list[BacktestEquityCurvePoint], benchmark_id: str) -> float | None:
    for row in rows:
        value = row.benchmark_navs.get(benchmark_id)
        if value is not None:
            return value
    return None


def _last_present_benchmark(rows: list[BacktestEquityCurvePoint], benchmark_id: str) -> float | None:
    for row in reversed(rows):
        value = row.benchmark_navs.get(benchmark_id)
        if value is not None:
            return value
    return None


def _normalize_benchmark_ids(values: list[str]) -> list[str]:
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


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


def _int_or_none(value: Any) -> int | None:
    number = _float_or_none(value)
    return None if number is None else int(number)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    text = str(value)
    return text or None


def _clean_number(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)
