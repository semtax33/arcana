from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.consensus import (
    DAILY_COLUMNS,
    ESTIMATE_COLUMNS,
    REPORT_COLUMNS,
    SILVER_DAILY_NAME,
    SILVER_ESTIMATES_NAME,
    SILVER_HANKYUNG_CONSENSUS_DIR,
    SILVER_REPORTS_NAME,
    SILVER_US_CONSENSUS_DIR,
    US_EVENT_COLUMNS,
    US_EVENTS_NAME,
    US_FACTOR_COLUMNS,
    US_FACTORS_NAME,
    US_OBSERVATION_COLUMNS,
    US_OBSERVATIONS_NAME,
    US_TARGET_PRICE_CONSENSUS_COLUMNS,
    US_TARGET_PRICE_CONSENSUS_NAME,
    US_TARGET_PRICE_RATING_COLUMNS,
    US_TARGET_PRICE_RATINGS_NAME,
)


CONSENSUS_TABLE_FILES = {
    "real_consensus_reports": (SILVER_REPORTS_NAME, REPORT_COLUMNS),
    "real_consensus_estimates": (SILVER_ESTIMATES_NAME, ESTIMATE_COLUMNS),
    "real_consensus_daily": (SILVER_DAILY_NAME, DAILY_COLUMNS),
}

US_CONSENSUS_TABLE_FILES = {
    "us_consensus_observations": (US_OBSERVATIONS_NAME, US_OBSERVATION_COLUMNS),
    "us_consensus_events": (US_EVENTS_NAME, US_EVENT_COLUMNS),
    "us_consensus_factors": (US_FACTORS_NAME, US_FACTOR_COLUMNS),
    "us_target_price_ratings": (
        US_TARGET_PRICE_RATINGS_NAME,
        US_TARGET_PRICE_RATING_COLUMNS,
    ),
    "us_target_price_consensus": (
        US_TARGET_PRICE_CONSENSUS_NAME,
        US_TARGET_PRICE_CONSENSUS_COLUMNS,
    ),
}
US_CONSENSUS_TABLE_DATE_COLUMNS = {
    "us_consensus_observations": "snapshot_date",
    "us_consensus_events": "event_date",
    "us_consensus_factors": "factor_date",
    "us_target_price_ratings": "snapshot_date",
    "us_target_price_consensus": "event_date",
}
US_TARGET_PRICE_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS us_target_price_ratings
    (
        rating_key String, symbol String, security_id String,
        provider LowCardinality(String), snapshot_date Date,
        rating_date Nullable(Date), availability_date Nullable(Date),
        target_date Nullable(Date), analyst_name String, analyst_firm String,
        analyst_role String, price_target Nullable(Float64), rating String,
        conclusion String, currency LowCardinality(String), raw_path String
    )
    ENGINE = ReplacingMergeTree
    PARTITION BY toYYYYMM(snapshot_date)
    ORDER BY (symbol, provider, rating_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS us_target_price_consensus
    (
        consensus_key String, symbol String, security_id String,
        provider LowCardinality(String), consensus_kind LowCardinality(String),
        source_regime LowCardinality(String), snapshot_date Date,
        event_date Date, availability_date Date,
        target_price_mean Nullable(Float64),
        target_price_median Nullable(Float64),
        target_price_low Nullable(Float64),
        target_price_high Nullable(Float64),
        analyst_count Nullable(UInt32), buy_count Nullable(UInt32),
        hold_count Nullable(UInt32), sell_count Nullable(UInt32),
        currency LowCardinality(String), raw_path String
    )
    ENGINE = ReplacingMergeTree
    PARTITION BY toYYYYMM(event_date)
    ORDER BY (symbol, provider, consensus_kind, event_date, consensus_key)
    """,
)

US_CONSENSUS_FACTORS_SCHEMA_MIGRATIONS = (
    "ALTER TABLE us_consensus_factors "
    "ADD COLUMN IF NOT EXISTS us_operating_income_consensus Nullable(Float64) "
    "AFTER us_revenue_consensus",
    "ALTER TABLE us_consensus_factors "
    "ADD COLUMN IF NOT EXISTS us_target_price Nullable(Float64) "
    "AFTER us_operating_income_consensus",
)
US_CONSENSUS_OBSERVATIONS_SCHEMA_MIGRATIONS = (
    "ALTER TABLE us_consensus_observations "
    "ADD COLUMN IF NOT EXISTS publishers_json String DEFAULT '[]' "
    "AFTER analyst_count",
    "ALTER TABLE us_consensus_observations "
    "ADD COLUMN IF NOT EXISTS consensus_row_key UInt64 AFTER raw_path, "
    "MODIFY ORDER BY "
    "(symbol, snapshot_date, provider, horizon, metric, statistic, "
    "consensus_row_key)",
)
US_CONSENSUS_FACTORS_ORDER_MIGRATION = (
    "ALTER TABLE us_consensus_factors "
    "ADD COLUMN IF NOT EXISTS consensus_row_key UInt64 AFTER raw_path, "
    "MODIFY ORDER BY "
    "(security_id, factor_date, provider, source_regime, horizon, "
    "consensus_row_key)"
)

STRING_COLUMNS = {
    "security_id",
    "stock_code",
    "publish_code",
    "office_name",
    "business_code",
    "business_name",
    "industry_code",
    "industry_name",
    "market_type",
    "report_type",
    "report_title",
    "report_writer",
    "report_content",
    "report_filepath",
    "report_filename",
    "grade_code",
    "grade_value",
    "old_grade_code",
    "old_grade_value",
    "stock_settlement_day1",
    "stock_settlement_day2",
    "stock_settlement_day3",
    "stock_settlement_day",
    "broker_code",
    "broker_name",
    "analyst_name",
    "target_period",
    "metric_id",
    "currency",
    "source_field",
    "source_provider",
    "quality_flags",
    "payload_json",
    "symbol",
    "provider",
    "dataset",
    "source_regime",
    "horizon",
    "period_type",
    "forecast_slot",
    "metric",
    "statistic",
    "event_type",
    "raw_path",
    "publishers_json",
    "rating_key",
    "consensus_key",
    "consensus_kind",
    "rating",
    "conclusion",
    "analyst_firm",
    "analyst_role",
}

DATE_COLUMNS = {
    "file_register_date",
    "report_date",
    "register_date",
    "update_date",
    "as_of_date",
    "snapshot_date",
    "availability_date",
    "event_date",
    "factor_date",
    "fiscal_period_end",
    "rating_date",
    "target_date",
}

DATETIME_COLUMNS = {"updated_at"}

INTEGER_COLUMNS = {
    "file_year",
    "report_idx",
    "report_count",
    "broker_count",
    "lookback_days",
    "analyst_count",
    "buy_count",
    "hold_count",
    "sell_count",
}

NON_NULL_DATE_DEFAULTS = {
    "file_register_date": date(1970, 1, 1),
    "as_of_date": date(1970, 1, 1),
}

NON_NULL_INTEGER_DEFAULTS = {
    "file_year": 0,
    "report_idx": 0,
}

FLOAT_COLUMNS = {
    "opinion_end_prices",
    "target_stock_prices",
    "old_target_stock_prices",
    "change_stock_prices",
    "stock_eps1",
    "stock_eps2",
    "stock_eps3",
    "stock_old_eps",
    "stock_net_profit1",
    "stock_net_profit2",
    "stock_net_profit3",
    "stock_expected_sales",
    "stock_pre_operating_profit",
    "stock_pre_net_income",
    "stock_pre_eps",
    "stock_pre_per",
    "stock_pre_pbr",
    "stock_pre_ev",
    "stock_pre_roe",
    "estimate_value",
    "consensus_mean",
    "consensus_median",
    "consensus_low",
    "consensus_high",
    "value",
    "reported_eps",
    "estimated_eps",
    "surprise_pct",
    "us_eps_consensus",
    "us_revenue_consensus",
    "us_operating_income_consensus",
    "us_target_price",
    "us_eps_revision_7d_pct",
    "us_eps_revision_30d_pct",
    "us_eps_revision_60d_pct",
    "us_eps_revision_90d_pct",
    "us_eps_revision_breadth_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_dispersion_pct",
    "us_revenue_dispersion_pct",
    "us_eps_surprise_pct",
    "price_target",
    "target_price_mean",
    "target_price_median",
    "target_price_low",
    "target_price_high",
}


def load_hankyung_consensus(
    *,
    market: str = "kr",
    silver_dir: str | Path = SILVER_HANKYUNG_CONSENSUS_DIR,
    dry_run: bool = False,
    client: Any = None,
) -> dict[str, int]:
    market = str(market or "kr").strip().lower()
    if market == "us":
        resolved_silver_dir = Path(silver_dir)
        return load_us_consensus(
            silver_dir=SILVER_US_CONSENSUS_DIR if resolved_silver_dir == SILVER_HANKYUNG_CONSENSUS_DIR else resolved_silver_dir,
            dry_run=dry_run,
            client=client,
        )
    if market != "kr":
        raise ValueError("market must be 'kr' or 'us'")

    counts: dict[str, int] = {}
    owns_client = client is None
    if not dry_run:
        client = client or get_clickhouse_client()
    try:
        for table_name, (file_name, columns) in CONSENSUS_TABLE_FILES.items():
            path = Path(silver_dir) / file_name
            frame = _read_silver_csv(path, columns=columns)
            counts[table_name] = len(frame)
            if dry_run or frame.empty:
                continue
            client.insert_df(table_name, frame, column_names=list(frame.columns))
    finally:
        close = getattr(client, "close", None)
        if owns_client and callable(close):
            close()

    print(
        "[DONE] kr consensus load "
        f"reports={counts.get('real_consensus_reports', 0):,}, "
        f"estimates={counts.get('real_consensus_estimates', 0):,}, "
        f"daily={counts.get('real_consensus_daily', 0):,}",
        flush=True,
    )
    return counts


def load_us_consensus(
    *,
    silver_dir: str | Path = SILVER_US_CONSENSUS_DIR,
    dry_run: bool = False,
    client: Any = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    owns_client = client is None
    if not dry_run:
        client = client or get_clickhouse_client()
    try:
        if not dry_run:
            ensure_us_consensus_schema(client)
        for table_name, (file_name, columns) in US_CONSENSUS_TABLE_FILES.items():
            frame = _read_silver_csv(Path(silver_dir) / file_name, columns=columns)
            counts[table_name] = len(frame)
            if not dry_run and not frame.empty:
                _delete_legacy_zero_key_rows(client, table_name)
                _insert_us_consensus_frame(client, table_name, frame)
    finally:
        close = getattr(client, "close", None)
        if owns_client and callable(close):
            close()
    print(
        "[DONE] us consensus load "
        f"observations={counts.get('us_consensus_observations', 0):,}, "
        f"events={counts.get('us_consensus_events', 0):,}, "
        f"factors={counts.get('us_consensus_factors', 0):,}, "
        f"target_ratings={counts.get('us_target_price_ratings', 0):,}, "
        f"target_consensus={counts.get('us_target_price_consensus', 0):,}",
        flush=True,
    )
    return counts


def ensure_us_consensus_factor_schema(client: Any) -> None:
    for query in US_CONSENSUS_FACTORS_SCHEMA_MIGRATIONS:
        command = getattr(client, "command", None)
        if callable(command):
            command(query)
        else:
            client.execute(query)


def ensure_us_consensus_schema(client: Any) -> None:
    for query in US_TARGET_PRICE_TABLE_DDL:
        command = getattr(client, "command", None)
        if callable(command):
            command(query)
        else:
            client.execute(query)
    ensure_us_consensus_factor_schema(client)
    for query in US_CONSENSUS_OBSERVATIONS_SCHEMA_MIGRATIONS:
        command = getattr(client, "command", None)
        if callable(command):
            command(query)
        else:
            client.execute(query)
    command = getattr(client, "command", None)
    if callable(command):
        command(US_CONSENSUS_FACTORS_ORDER_MIGRATION)
    else:
        client.execute(US_CONSENSUS_FACTORS_ORDER_MIGRATION)


def _delete_legacy_zero_key_rows(client: Any, table_name: str) -> None:
    if table_name not in {
        "us_consensus_observations",
        "us_consensus_factors",
    }:
        return
    query = (
        f"ALTER TABLE {table_name} DELETE WHERE consensus_row_key = 0 "
        "SETTINGS mutations_sync = 2"
    )
    command = getattr(client, "command", None)
    if callable(command):
        command(query)
    else:
        client.execute(query)


def _insert_us_consensus_frame(client: Any, table_name: str, frame: pd.DataFrame) -> None:
    """Insert one ClickHouse month at a time to stay below its partition limit."""
    frame = _with_consensus_row_key(table_name, frame)
    date_column = US_CONSENSUS_TABLE_DATE_COLUMNS[table_name]
    dates = pd.to_datetime(frame[date_column], errors="coerce")
    if dates.isna().any():
        invalid_count = int(dates.isna().sum())
        raise ValueError(
            f"{table_name}.{date_column} contains {invalid_count:,} invalid date value(s)"
        )
    batches = frame.assign(_partition=dates.dt.to_period("M")).groupby(
        "_partition",
        sort=True,
        observed=True,
    )
    for _, batch in batches:
        prepared = batch.drop(columns="_partition")
        client.insert_df(table_name, prepared, column_names=list(prepared.columns))


def _with_consensus_row_key(table_name: str, frame: pd.DataFrame) -> pd.DataFrame:
    if table_name == "us_consensus_observations":
        identity_columns = (
            "dataset",
            "source_regime",
            "availability_date",
            "period_type",
            "fiscal_period_end",
            "forecast_slot",
            "lookback_days",
        )
    elif table_name == "us_consensus_factors":
        identity_columns = ("raw_path",)
    else:
        return frame
    result = frame.copy()
    result["consensus_row_key"] = [
        _stable_row_key(row, identity_columns)
        for _, row in result.iterrows()
    ]
    return result


def _stable_row_key(row: pd.Series, columns: tuple[str, ...]) -> int:
    values = []
    for column in columns:
        value = row.get(column)
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            values.append("")
        elif hasattr(value, "isoformat"):
            values.append(value.isoformat())
        else:
            values.append(str(value))
    digest = hashlib.blake2b(
        "\x1f".join(values).encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def _read_silver_csv(path: Path, *, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in columns:
        if column not in frame.columns:
            frame[column] = ""
    return _prepare_insert_frame(frame[columns])


def _prepare_insert_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if column in STRING_COLUMNS:
            result[column] = result[column].map(_string_value)
        elif column in DATE_COLUMNS:
            result[column] = result[column].map(_date_value)
            if column in NON_NULL_DATE_DEFAULTS:
                result[column] = result[column].map(
                    lambda value, default=NON_NULL_DATE_DEFAULTS[column]: value or default
                )
        elif column in DATETIME_COLUMNS:
            result[column] = result[column].map(_datetime_value)
        elif column in INTEGER_COLUMNS:
            result[column] = result[column].map(_int_value)
            if column in NON_NULL_INTEGER_DEFAULTS:
                result[column] = result[column].map(
                    lambda value, default=NON_NULL_INTEGER_DEFAULTS[column]: value if value is not None else default
                )
        elif column in FLOAT_COLUMNS:
            result[column] = result[column].map(_float_value)
    return result


def _string_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _date_value(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _datetime_value(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return pd.to_datetime(text, errors="raise").to_pydatetime()
        except Exception:
            return None


def _int_value(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _float_value(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Load normalized consensus CSVs into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--silver-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_hankyung_consensus(
        market=args.market,
        silver_dir=args.silver_dir or (SILVER_US_CONSENSUS_DIR if args.market == "us" else SILVER_HANKYUNG_CONSENSUS_DIR),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
