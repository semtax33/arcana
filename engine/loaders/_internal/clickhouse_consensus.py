from __future__ import annotations

import argparse
from datetime import date, datetime
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
)


CONSENSUS_TABLE_FILES = {
    "real_consensus_reports": (SILVER_REPORTS_NAME, REPORT_COLUMNS),
    "real_consensus_estimates": (SILVER_ESTIMATES_NAME, ESTIMATE_COLUMNS),
    "real_consensus_daily": (SILVER_DAILY_NAME, DAILY_COLUMNS),
}

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
}

DATE_COLUMNS = {
    "file_register_date",
    "report_date",
    "register_date",
    "update_date",
    "as_of_date",
}

DATETIME_COLUMNS = {"updated_at"}

INTEGER_COLUMNS = {
    "file_year",
    "report_idx",
    "report_count",
    "broker_count",
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
}


def load_hankyung_consensus(
    *,
    market: str = "kr",
    silver_dir: str | Path = SILVER_HANKYUNG_CONSENSUS_DIR,
    dry_run: bool = False,
    client: Any = None,
) -> dict[str, int]:
    market = str(market or "kr").strip().lower()
    if market != "kr":
        raise NotImplementedError("US consensus not supported yet")

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
    parser = argparse.ArgumentParser(description="Load normalized KR consensus CSVs into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--silver-dir", default=str(SILVER_HANKYUNG_CONSENSUS_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_hankyung_consensus(
        market=args.market,
        silver_dir=args.silver_dir,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
