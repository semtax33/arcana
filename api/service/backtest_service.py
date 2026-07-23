from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime, timedelta
import io
import math
import os
from typing import Any, Callable

import pandas as pd
from clickhouse_connect.driver.external import ExternalData

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
    DEFAULT_FACTOR_SNAPSHOT_TABLE,
    DEFAULT_FACTOR_TABLE,
    build_benchmark_history_query,
    build_factor_raw_batch_query,
    build_factor_snapshot_batch_query,
    build_factor_snapshot_query,
    build_portfolio_return_query,
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
from engine.transformers.benchmarks import normalize_benchmark_id


SURVIVOR_BIAS_WARNING = (
    "Survivor bias is not fully eliminated because delisted security history is not available. "
    "The backtest does not filter by current security_master.is_active and only uses securities "
    "with point-in-time factor and price data."
)
DEFAULT_KR_BENCHMARK_IDS = ["KOSPI200", "KOSDAQ"]
DEFAULT_US_BENCHMARK_IDS = ["US_NASDAQ", "US_SP500"]


class BacktestService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def run_factor_backtest(self, request: FactorBacktestRequestDto) -> FactorBacktestResult:
        _validate_request(request)
        conditions = [_to_repository_condition(condition) for condition in request.conditions]
        style_profile = _resolve_style_profile(request.style_profile, conditions)
        benchmark_ids = _resolve_benchmark_ids(request.benchmarks, market=request.market)
        warnings = [SURVIVOR_BIAS_WARNING]

        client = self._client_factory()
        try:
            trading_days = self._load_trading_days(
                client,
                request.start_date,
                request.end_date,
                market=request.market,
            )
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

            factor_table, factor_table_is_snapshot = _resolve_factor_table(
                client,
                requested_table=request.factor_table,
                conditions=conditions,
            )

            equity_points, rebalance_history = self._run_strategy(
                client,
                conditions=conditions,
                trading_days=trading_days,
                rebalance_dates=rebalance_dates,
                end_date=request.end_date,
                market=request.market,
                financial_basis=request.financial_basis or "annual",
                factor_table=factor_table,
                factor_table_is_snapshot=factor_table_is_snapshot,
                raw_lookback_days=_raw_lookback_days(),
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

    def _load_trading_days(
        self,
        client: Any,
        start_date: date,
        end_date: date,
        *,
        market: str | None = None,
    ) -> list[date]:
        query, params = build_trading_days_query(
            start_date=start_date,
            end_date=end_date,
            market=market,
        )
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
        factor_table: str,
        factor_table_is_snapshot: bool,
        raw_lookback_days: int | None,
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
        previous_positions_by_id: dict[str, BacktestPosition] = {}
        signal_dates_by_rebalance = {
            rebalance_date: _previous_trading_day(trading_days, rebalance_date)
            for rebalance_date in rebalance_dates
        }
        batched_snapshot_rows: dict[date, list[dict[str, Any]]] | None = None
        batch_signal_dates = sorted(
            {
                signal_date
                for signal_date in signal_dates_by_rebalance.values()
                if signal_date is not None
            }
        )
        snapshot_dates_by_signal: dict[date, date] = {}
        has_style_factors = any(
            is_style_score_factor(condition.factor_id) for condition in conditions
        )
        if factor_table_is_snapshot and batch_signal_dates:
            regular_factor_ids = sorted(
                {
                    condition.factor_id
                    for condition in conditions
                    if not is_style_score_factor(condition.factor_id)
                }
            )
            if regular_factor_ids:
                snapshot_dates_by_signal = _snapshot_dates_for_signal_dates(
                    client,
                    factor_table,
                    factor_ids=regular_factor_ids,
                    financial_basis=financial_basis,
                    signal_dates=batch_signal_dates,
                    carry_days=_snapshot_carry_days(),
                )
                raw_signal_count = len(batch_signal_dates) - len(snapshot_dates_by_signal)
                if raw_signal_count:
                    warnings.append(
                        f"Used raw factor fallback for {raw_signal_count} of "
                        f"{len(batch_signal_dates)} signal dates without a usable snapshot."
                    )
        if (
            factor_table_is_snapshot
            and len(snapshot_dates_by_signal) > 1
            and not has_style_factors
        ):
            snapshot_signal_dates = sorted(snapshot_dates_by_signal)
            batched_snapshot_rows = self._load_factor_snapshot_batch(
                client,
                conditions=conditions,
                signal_dates=snapshot_signal_dates,
                snapshot_dates=[
                    snapshot_dates_by_signal[signal_date]
                    for signal_date in snapshot_signal_dates
                ],
                market=market,
                financial_basis=financial_basis,
                factor_table=factor_table,
                sector_codes=sector_codes,
                industry_group_codes=industry_group_codes,
            )
        raw_signal_dates = (
            [
                signal_date
                for signal_date in batch_signal_dates
                if signal_date not in snapshot_dates_by_signal
            ]
            if factor_table_is_snapshot
            else batch_signal_dates
        )
        batched_raw_rows: dict[date, list[dict[str, Any]]] | None = None
        if len(raw_signal_dates) > 1 and not has_style_factors:
            batched_raw_rows = self._load_factor_raw_batch(
                client,
                conditions=conditions,
                signal_dates=raw_signal_dates,
                market=market,
                financial_basis=financial_basis,
                factor_table=(DEFAULT_FACTOR_TABLE if factor_table_is_snapshot else factor_table),
                raw_lookback_days=raw_lookback_days,
                sector_codes=sector_codes,
                industry_group_codes=industry_group_codes,
            )

        for index, rebalance_date in enumerate(rebalance_dates):
            signal_date = signal_dates_by_rebalance[rebalance_date]
            if signal_date is None:
                warnings.append(f"Skipped rebalance {rebalance_date.isoformat()}: no prior signal date.")
                continue

            if batched_snapshot_rows is not None and signal_date in snapshot_dates_by_signal:
                snapshot_rows = batched_snapshot_rows.get(signal_date, [])
            elif batched_raw_rows is not None and signal_date in raw_signal_dates:
                snapshot_rows = batched_raw_rows.get(signal_date, [])
            else:
                use_snapshot = (
                    factor_table_is_snapshot and signal_date in snapshot_dates_by_signal
                )
                query_factor_table = (
                    factor_table
                    if use_snapshot or not factor_table_is_snapshot
                    else DEFAULT_FACTOR_TABLE
                )
                snapshot_rows = self._load_factor_snapshot(
                    client,
                    conditions=conditions,
                    signal_date=signal_date,
                    snapshot_date=(
                        snapshot_dates_by_signal[signal_date] if use_snapshot else None
                    ),
                    market=market,
                    financial_basis=financial_basis,
                    factor_table=query_factor_table,
                    factor_table_is_snapshot=use_snapshot,
                    raw_lookback_days=None if use_snapshot else raw_lookback_days,
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

        first_segment_date = min(segment["start_date"] for segment in planned_segments)
        last_segment_date = max(segment["end_date"] for segment in planned_segments)
        portfolio_returns = self._load_portfolio_returns(
            client,
            segments=planned_segments,
            trading_days=[
                trading_day
                for trading_day in trading_days
                if first_segment_date <= trading_day <= last_segment_date
            ],
        )

        for segment_id, _segment in enumerate(planned_segments):
            segment_returns = portfolio_returns.get(segment_id, [])
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
        snapshot_date: date | None = None,
        market: str | None,
        financial_basis: str,
        factor_table: str,
        factor_table_is_snapshot: bool,
        raw_lookback_days: int | None,
        style_profile: str,
        sector_codes: list[str] | None,
        industry_group_codes: list[str] | None,
    ) -> list[dict[str, Any]]:
        query, params = build_factor_snapshot_query(
            conditions,
            signal_date=signal_date,
            snapshot_date=snapshot_date,
            market=market,
            financial_basis=financial_basis,
            factor_table=factor_table,
            factor_table_is_snapshot=factor_table_is_snapshot,
            raw_lookback_days=raw_lookback_days,
            style_profile=style_profile,
            sector_codes=sector_codes,
            industry_group_codes=industry_group_codes,
        )
        return _records(client.query_df(query, parameters=params))

    def _load_factor_snapshot_batch(
        self,
        client: Any,
        *,
        conditions: list[FactorCondition],
        signal_dates: list[date],
        snapshot_dates: list[date],
        market: str | None,
        financial_basis: str,
        factor_table: str,
        sector_codes: list[str] | None,
        industry_group_codes: list[str] | None,
    ) -> dict[date, list[dict[str, Any]]]:
        query, params = build_factor_snapshot_batch_query(
            conditions,
            signal_dates=signal_dates,
            snapshot_dates=snapshot_dates,
            market=market,
            financial_basis=financial_basis,
            factor_table=factor_table,
            sector_codes=sector_codes,
            industry_group_codes=industry_group_codes,
        )
        rows_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in _records(client.query_df(query, parameters=params)):
            signal_date = _as_date(row["signal_date"])
            rows_by_date[signal_date].append(row)
        return rows_by_date

    def _load_factor_raw_batch(
        self,
        client: Any,
        *,
        conditions: list[FactorCondition],
        signal_dates: list[date],
        market: str | None,
        financial_basis: str,
        factor_table: str,
        raw_lookback_days: int | None,
        sector_codes: list[str] | None,
        industry_group_codes: list[str] | None,
    ) -> dict[date, list[dict[str, Any]]]:
        query, params = build_factor_raw_batch_query(
            conditions,
            signal_dates=signal_dates,
            market=market,
            financial_basis=financial_basis,
            factor_table=factor_table,
            raw_lookback_days=raw_lookback_days,
            sector_codes=sector_codes,
            industry_group_codes=industry_group_codes,
        )
        rows_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in _records(client.query_df(query, parameters=params)):
            signal_date = _as_date(row["signal_date"])
            rows_by_date[signal_date].append(row)
        return rows_by_date

    def _load_portfolio_returns(
        self,
        client: Any,
        *,
        segments: list[dict[str, Any]],
        trading_days: list[date],
    ) -> dict[int, list[tuple[date, float]]]:
        query, params, position_rows = build_portfolio_return_query(
            segments=segments,
            trading_days=trading_days,
        )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(position_rows)
        external_data = ExternalData(
            file_name="portfolio_positions.csv",
            data=buffer.getvalue().encode("utf-8"),
            fmt="CSV",
            structure=(
                "segment_id UInt32, security_id String, start_date Date, "
                "end_date Date, transaction_cost_bps Float64"
            ),
        )
        rows = _records(
            client.query_df(
                query,
                parameters=params,
                external_data=external_data,
            )
        )
        if not rows:
            return {}
        returns_by_segment: dict[int, list[tuple[date, float]]] = defaultdict(list)
        for row in rows:
            daily_return = _float_or_none(row.get("daily_return"))
            returns_by_segment[int(row["segment_id"])].append(
                (_as_date(row["trade_date"]), daily_return if daily_return is not None else 0.0)
            )
        return returns_by_segment

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


def _resolve_factor_table(
    client: Any,
    *,
    requested_table: str | None,
    conditions: list[FactorCondition],
) -> tuple[str, bool]:
    if requested_table:
        return requested_table, False
    factor_ids = sorted(
        {
            condition.factor_id
            for condition in conditions
            if not is_style_score_factor(condition.factor_id)
        }
    )
    if factor_ids and _table_exists(client, DEFAULT_FACTOR_SNAPSHOT_TABLE):
        return DEFAULT_FACTOR_SNAPSHOT_TABLE, True
    return DEFAULT_FACTOR_TABLE, False


def _snapshot_dates_for_signal_dates(
    client: Any,
    table_name: str,
    *,
    factor_ids: list[str],
    financial_basis: str,
    signal_dates: list[date],
    carry_days: int,
) -> dict[date, date]:
    query = getattr(client, "query", None)
    if not callable(query):
        return {}
    normalized_signal_dates = sorted({_as_date(value) for value in signal_dates})
    if not normalized_signal_dates:
        return {}
    candidate_dates = sorted(
        {
            signal_date - timedelta(days=offset)
            for signal_date in normalized_signal_dates
            for offset in range(max(0, carry_days) + 1)
        }
    )
    try:
        rows = query(
            f"""
SELECT
    trade_date,
    max(source_trade_date) AS max_source_trade_date
FROM {table_name}
PREWHERE trade_date IN {{candidate_dates:Array(Date)}}
WHERE has({{factor_ids:Array(String)}}, factor_id)
    AND financial_basis = {{financial_basis:String}}
GROUP BY trade_date
HAVING countDistinct(factor_id) >= {{factor_count:UInt64}}
ORDER BY trade_date
""".strip(),
            parameters={
                "factor_ids": factor_ids,
                "factor_count": len(factor_ids),
                "financial_basis": financial_basis,
                "candidate_dates": [value.isoformat() for value in candidate_dates],
            },
        ).result_rows
    except Exception:
        return {}

    available_dates = {
        _as_date(row[0]): _as_date(row[1])
        for row in rows
        if row and row[0] is not None and row[1] is not None
    }
    resolved: dict[date, date] = {}
    for signal_date in normalized_signal_dates:
        earliest = signal_date - timedelta(days=max(0, carry_days))
        eligible = [
            snapshot_date
            for snapshot_date, max_source_trade_date in available_dates.items()
            if earliest <= snapshot_date <= signal_date
            and max_source_trade_date <= signal_date
        ]
        if eligible:
            resolved[signal_date] = max(eligible)
    return resolved


def _table_exists(client: Any, table_name: str) -> bool:
    query = getattr(client, "query", None)
    if not callable(query):
        return False
    try:
        rows = query(f"EXISTS TABLE {table_name}").result_rows
    except Exception:
        return False
    return bool(rows and rows[0][0])


def _raw_lookback_days() -> int | None:
    value = os.getenv("ARCANA_FACTOR_RAW_LOOKBACK_DAYS", "540").strip()
    if not value:
        return None
    try:
        days = int(value)
    except ValueError:
        return 540
    return days if days > 0 else None


def _snapshot_carry_days() -> int:
    value = os.getenv("ARCANA_FACTOR_SNAPSHOT_CARRY_DAYS", "14").strip()
    try:
        days = int(value)
    except ValueError:
        return 14
    return max(0, days)


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
        "monthly": set(range(1, 13)),
        "quarterly": {3, 6, 9, 12},
        "semiannual": {6, 12},
        "annual": {12},
    }
    if frequency not in months_by_frequency:
        raise ValueError("rebalance_frequency must be one of: monthly, quarterly, semiannual, annual")

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
    return_observations = len(returns)
    average_return = sum(returns) / len(returns) if returns else None
    return_stddev = _stddev(returns) if return_observations >= 2 else None
    volatility = return_stddev * math.sqrt(252) if return_stddev is not None else None
    sharpe = (
        average_return / return_stddev * math.sqrt(252)
        if average_return is not None and return_stddev is not None and return_stddev > 0
        else None
    )
    t_stat = (
        average_return / (return_stddev / math.sqrt(return_observations))
        if average_return is not None and return_stddev is not None and return_stddev > 0
        else None
    )
    p_value = (
        _two_sided_t_p_value(t_stat, degrees_of_freedom=return_observations - 1)
        if t_stat is not None
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
        t_stat=_clean_number(t_stat),
        p_value=_clean_number(p_value),
        return_observations=return_observations,
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


def _two_sided_t_p_value(t_stat: float, *, degrees_of_freedom: int) -> float | None:
    """Return the exact two-sided p-value for a Student t statistic."""
    if degrees_of_freedom <= 0 or not math.isfinite(t_stat):
        return None
    x = degrees_of_freedom / (degrees_of_freedom + t_stat * t_stat)
    return _regularized_incomplete_beta(x, degrees_of_freedom / 2.0, 0.5)


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        result = front * _beta_continued_fraction(a, b, x) / a
    else:
        result = 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b
    return min(1.0, max(0.0, result))


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    max_iterations = 100
    epsilon = 3e-14
    minimum = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < minimum:
        d = minimum
    d = 1.0 / d
    result = d

    for iteration in range(1, max_iterations + 1):
        doubled = 2 * iteration
        coefficient = (
            iteration
            * (b - iteration)
            * x
            / ((qam + doubled) * (a + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < minimum:
            d = minimum
        c = 1.0 + coefficient / c
        if abs(c) < minimum:
            c = minimum
        d = 1.0 / d
        result *= d * c

        coefficient = (
            -(a + iteration)
            * (qab + iteration)
            * x
            / ((a + doubled) * (qap + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < minimum:
            d = minimum
        c = 1.0 + coefficient / c
        if abs(c) < minimum:
            c = minimum
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            break

    return result


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


def _resolve_benchmark_ids(values: list[str], *, market: str | None) -> list[str]:
    benchmark_ids = _normalize_benchmark_ids(values)
    normalized_market = str(market or "").strip().lower()
    if normalized_market == "us" and (not benchmark_ids or benchmark_ids == sorted(DEFAULT_KR_BENCHMARK_IDS)):
        return sorted(DEFAULT_US_BENCHMARK_IDS)
    if normalized_market == "kr" and not benchmark_ids:
        return sorted(DEFAULT_KR_BENCHMARK_IDS)
    return benchmark_ids


def _normalize_benchmark_ids(values: list[str]) -> list[str]:
    return sorted({normalize_benchmark_id(value) for value in values if str(value).strip()})


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
