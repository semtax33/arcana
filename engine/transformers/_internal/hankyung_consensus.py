from __future__ import annotations

from datetime import datetime
import heapq
import json
import math
import re
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import DATA_LAKE


BRONZE_HANKYUNG_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "hankyung")
BRONZE_VALUEFINDER_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "valuefinder")
BRONZE_EQUITY_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "equity")
SILVER_HANKYUNG_CONSENSUS_DIR = DATA_LAKE.silver("consensus", "hankyung")
SILVER_KR_CONSENSUS_DIR = SILVER_HANKYUNG_CONSENSUS_DIR
SILVER_REPORTS_NAME = "kr_hankyung_consensus_reports.csv"
SILVER_ESTIMATES_NAME = "kr_hankyung_consensus_estimates.csv"
SILVER_DAILY_NAME = "kr_hankyung_consensus_daily.csv"
SILVER_TARGET_PRICE_NAME = "kr_hankyung_target_price_consensus.csv"
DEFAULT_STALE_DAYS = 180
TARGET_PRICE_LOOKBACK_DAYS = 120

REPORT_COLUMNS = [
    "security_id",
    "stock_code",
    "file_register_date",
    "file_year",
    "report_idx",
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
    "report_date",
    "grade_code",
    "grade_value",
    "old_grade_code",
    "old_grade_value",
    "opinion_end_prices",
    "target_stock_prices",
    "old_target_stock_prices",
    "change_stock_prices",
    "stock_settlement_day1",
    "stock_eps1",
    "stock_settlement_day2",
    "stock_eps2",
    "stock_settlement_day3",
    "stock_eps3",
    "stock_old_eps",
    "stock_net_profit1",
    "stock_net_profit2",
    "stock_net_profit3",
    "stock_settlement_day",
    "stock_expected_sales",
    "stock_pre_operating_profit",
    "stock_pre_net_income",
    "stock_pre_eps",
    "stock_pre_per",
    "stock_pre_pbr",
    "stock_pre_ev",
    "stock_pre_roe",
    "register_date",
    "update_date",
    "quality_flags",
    "payload_json",
    "updated_at",
]

ESTIMATE_COLUMNS = [
    "security_id",
    "stock_code",
    "file_register_date",
    "file_year",
    "report_idx",
    "broker_code",
    "broker_name",
    "analyst_name",
    "as_of_date",
    "target_period",
    "metric_id",
    "estimate_value",
    "currency",
    "source_field",
    "source_provider",
    "quality_flags",
    "updated_at",
]

DAILY_COLUMNS = [
    "security_id",
    "stock_code",
    "as_of_date",
    "target_period",
    "metric_id",
    "consensus_mean",
    "consensus_median",
    "consensus_low",
    "consensus_high",
    "report_count",
    "broker_count",
    "currency",
    "source_provider",
    "updated_at",
]

TARGET_PRICE_COLUMNS = [
    "security_id",
    "stock_code",
    "event_date",
    "target_price_mean",
    "target_price_median",
    "target_price_low",
    "target_price_high",
    "analyst_count",
    "currency",
    "source_provider",
    "updated_at",
]

PRE_METRIC_FIELDS = {
    "STOCK_PRE_EPS": "basic_eps",
    "STOCK_EXPECTED_SALES": "revenue",
    "STOCK_PRE_OPERATING_PROFIT": "operating_income",
    "STOCK_PRE_NET_INCOME": "net_income",
    "STOCK_PRE_PER": "forward_per",
    "STOCK_PRE_ROE": "forward_roe",
}

_BRONZE_FILE_RE = re.compile(r"^(?P<business_code>[^_]+)_(?P<register_date>[^.]+)\.json$", re.IGNORECASE)


def normalize_hankyung_consensus(
    *,
    bronze_dir: str | Path = BRONZE_HANKYUNG_CONSENSUS_DIR,
    output_dir: str | Path = SILVER_HANKYUNG_CONSENSUS_DIR,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict[str, Path | int]:
    bronze_path = Path(bronze_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    reports_df, estimates_df = build_hankyung_consensus_frames(bronze_path)
    daily_df = build_hankyung_daily_consensus(estimates_df, stale_days=stale_days)
    target_price_df = build_hankyung_target_price_consensus(reports_df)

    reports_path = output_path / SILVER_REPORTS_NAME
    estimates_path = output_path / SILVER_ESTIMATES_NAME
    daily_path = output_path / SILVER_DAILY_NAME
    target_price_path = output_path / SILVER_TARGET_PRICE_NAME
    _write_csv(reports_path, reports_df, REPORT_COLUMNS)
    _write_csv(estimates_path, estimates_df, ESTIMATE_COLUMNS)
    _write_csv(daily_path, daily_df, DAILY_COLUMNS)
    _write_csv(target_price_path, target_price_df, TARGET_PRICE_COLUMNS)

    print(
        "[DONE] hankyung consensus normalize "
        f"reports={len(reports_df):,}, estimates={len(estimates_df):,}, "
        f"daily={len(daily_df):,}, target_prices={len(target_price_df):,}",
        flush=True,
    )
    return {
        "reports_path": reports_path,
        "estimates_path": estimates_path,
        "daily_path": daily_path,
        "target_price_path": target_price_path,
        "reports": len(reports_df),
        "estimates": len(estimates_df),
        "daily": len(daily_df),
        "target_prices": len(target_price_df),
    }


def normalize_kr_consensus(
    *,
    hankyung_bronze_dir: str | Path = BRONZE_HANKYUNG_CONSENSUS_DIR,
    valuefinder_bronze_dir: str | Path | None = BRONZE_VALUEFINDER_CONSENSUS_DIR,
    equity_bronze_dir: str | Path | None = BRONZE_EQUITY_CONSENSUS_DIR,
    output_dir: str | Path = SILVER_KR_CONSENSUS_DIR,
    stale_days: int = DEFAULT_STALE_DAYS,
    stock_name_lookup: dict[str, str] | None = None,
) -> dict[str, Path | int]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    hankyung_reports, estimates_df = build_hankyung_consensus_frames(hankyung_bronze_dir)
    reports_df = _combine_kr_consensus_reports(
        hankyung_reports,
        valuefinder_bronze_dir=valuefinder_bronze_dir,
        equity_bronze_dir=equity_bronze_dir,
        stock_name_lookup=stock_name_lookup,
    )
    daily_df = build_hankyung_daily_consensus(estimates_df, stale_days=stale_days)
    target_price_df = build_hankyung_target_price_consensus(hankyung_reports)

    reports_path = output_path / SILVER_REPORTS_NAME
    estimates_path = output_path / SILVER_ESTIMATES_NAME
    daily_path = output_path / SILVER_DAILY_NAME
    target_price_path = output_path / SILVER_TARGET_PRICE_NAME
    _write_csv(reports_path, reports_df, REPORT_COLUMNS)
    _write_csv(estimates_path, estimates_df, ESTIMATE_COLUMNS)
    _write_csv(daily_path, daily_df, DAILY_COLUMNS)
    _write_csv(target_price_path, target_price_df, TARGET_PRICE_COLUMNS)

    print(
        "[DONE] kr consensus normalize "
        f"reports={len(reports_df):,}, estimates={len(estimates_df):,}, "
        f"daily={len(daily_df):,}, target_prices={len(target_price_df):,}",
        flush=True,
    )
    return {
        "reports_path": reports_path,
        "estimates_path": estimates_path,
        "daily_path": daily_path,
        "target_price_path": target_price_path,
        "reports": len(reports_df),
        "estimates": len(estimates_df),
        "daily": len(daily_df),
        "target_prices": len(target_price_df),
    }


def build_kr_consensus_frames(
    *,
    hankyung_bronze_dir: str | Path = BRONZE_HANKYUNG_CONSENSUS_DIR,
    valuefinder_bronze_dir: str | Path | None = BRONZE_VALUEFINDER_CONSENSUS_DIR,
    equity_bronze_dir: str | Path | None = BRONZE_EQUITY_CONSENSUS_DIR,
    stock_name_lookup: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    hankyung_reports, hankyung_estimates = build_hankyung_consensus_frames(hankyung_bronze_dir)
    reports_df = _combine_kr_consensus_reports(
        hankyung_reports,
        valuefinder_bronze_dir=valuefinder_bronze_dir,
        equity_bronze_dir=equity_bronze_dir,
        stock_name_lookup=stock_name_lookup,
    )
    return reports_df, hankyung_estimates


def _combine_kr_consensus_reports(
    hankyung_reports: pd.DataFrame,
    *,
    valuefinder_bronze_dir: str | Path | None,
    equity_bronze_dir: str | Path | None,
    stock_name_lookup: dict[str, str] | None,
) -> pd.DataFrame:
    report_frames = []
    if not hankyung_reports.empty:
        report_frames.append(hankyung_reports)

    for bronze_dir in (valuefinder_bronze_dir, equity_bronze_dir):
        if bronze_dir is None:
            continue
        frame = build_html_consensus_report_frame(bronze_dir, stock_name_lookup=stock_name_lookup)
        if not frame.empty:
            report_frames.append(frame)

    reports_df = (
        pd.concat([frame.dropna(axis=1, how="all") for frame in report_frames], ignore_index=True, sort=False)
        if report_frames
        else pd.DataFrame(columns=REPORT_COLUMNS)
    )
    if reports_df.empty:
        reports_df = pd.DataFrame(columns=REPORT_COLUMNS)
    else:
        for column in REPORT_COLUMNS:
            if column not in reports_df.columns:
                reports_df[column] = pd.NA
        reports_df = reports_df[REPORT_COLUMNS]

    return reports_df


def build_hankyung_consensus_frames(bronze_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    reports: list[dict[str, Any]] = []
    estimates: list[dict[str, Any]] = []
    for path in sorted(Path(bronze_dir).glob("*.json")):
        file_meta = parse_hankyung_bronze_filename(path)
        if file_meta is None:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        report_record = report_record_from_row(row, file_meta)
        reports.append(report_record)
        estimates.extend(estimate_records_from_row(row, report_record))

    reports_df = pd.DataFrame(reports, columns=REPORT_COLUMNS)
    estimates_df = pd.DataFrame(estimates, columns=ESTIMATE_COLUMNS)
    return reports_df, estimates_df


def build_html_consensus_report_frame(
    bronze_dir: str | Path,
    *,
    stock_name_lookup: dict[str, str] | None = None,
) -> pd.DataFrame:
    reports: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(bronze_dir).glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)

    lookup = stock_name_lookup
    if lookup is None and any(
        not _stock_code(row.get("BUSINESS_CODE")) and _text(row.get("BUSINESS_NAME")) for row in rows
    ):
        lookup = _load_kr_stock_name_lookup()

    for row in rows:
        resolved_row = _resolve_html_stock_code(row, stock_name_lookup=lookup or {})
        file_meta = file_meta_from_report_row(resolved_row)
        reports.append(report_record_from_row(resolved_row, file_meta))

    return pd.DataFrame(reports, columns=REPORT_COLUMNS)


def parse_hankyung_bronze_filename(path: str | Path) -> dict[str, Any] | None:
    match = _BRONZE_FILE_RE.match(Path(path).name)
    if not match:
        return None
    register_date = match.group("register_date").strip()
    file_date = _date_from_register_date(register_date)
    return {
        "stock_code": _stock_code(match.group("business_code")),
        "register_date": register_date,
        "file_register_date": file_date,
        "file_year": file_date[:4] if file_date else register_date[:4],
    }


def file_meta_from_report_row(row: dict[str, Any]) -> dict[str, Any]:
    register_date = _text(row.get("REGISTER_DATE"))
    file_date = _date_from_register_date(register_date) or _date_text(row.get("REPORT_DATE"))
    file_year = file_date[:4] if file_date else register_date[:4]
    return {
        "stock_code": _stock_code(row.get("BUSINESS_CODE")),
        "register_date": register_date,
        "file_register_date": file_date,
        "file_year": file_year,
    }


def report_record_from_row(row: dict[str, Any], file_meta: dict[str, Any]) -> dict[str, Any]:
    stock_code = str(file_meta["stock_code"])
    register_date = str(file_meta.get("register_date") or "")
    quality_flags = _quality_flags(row, stock_code=stock_code, register_date=register_date)
    updated_at = _updated_at()
    return {
        "security_id": f"SEC_KR_{stock_code}" if stock_code else "",
        "stock_code": stock_code,
        "file_register_date": file_meta.get("file_register_date") or "",
        "file_year": file_meta.get("file_year") or "",
        "report_idx": row.get("REPORT_IDX"),
        "publish_code": _text(row.get("PUBLISH_CODE")),
        "office_name": _text(row.get("OFFICE_NAME")),
        "business_code": _text(row.get("BUSINESS_CODE")),
        "business_name": _text(row.get("BUSINESS_NAME")),
        "industry_code": _text(row.get("INDUSTRY_CODE")),
        "industry_name": _text(row.get("INDUSTRY_NAME")),
        "market_type": _text(row.get("MARKET_TYPE")),
        "report_type": _text(row.get("REPORT_TYPE")),
        "report_title": _text(row.get("REPORT_TITLE")),
        "report_writer": _text(row.get("REPORT_WRITER")),
        "report_content": _text(row.get("REPORT_CONTENT")),
        "report_filepath": _text(row.get("REPORT_FILEPATH")),
        "report_filename": _text(row.get("REPORT_FILENAME")),
        "report_date": _date_text(row.get("REPORT_DATE")) or file_meta.get("file_register_date") or "",
        "grade_code": _text(row.get("GRADE_CODE")),
        "grade_value": _text(row.get("GRADE_VALUE")),
        "old_grade_code": _text(row.get("OLD_GRADE_CODE")),
        "old_grade_value": _text(row.get("OLD_GRADE_VALUE")),
        "opinion_end_prices": _number(row.get("OPINON_END_PRICES")),
        "target_stock_prices": _number(row.get("TARGET_STOCK_PRICES")),
        "old_target_stock_prices": _number(row.get("OLD_TARGET_STOCK_PRICES")),
        "change_stock_prices": _number(row.get("CHANGE_STOCK_PRICES")),
        "stock_settlement_day1": _text(row.get("STOCK_SETTLEMENT_DAY1")),
        "stock_eps1": _number(row.get("STOCK_EPS1")),
        "stock_settlement_day2": _text(row.get("STOCK_SETTLEMENT_DAY2")),
        "stock_eps2": _number(row.get("STOCK_EPS2")),
        "stock_settlement_day3": _text(row.get("STOCK_SETTLEMENT_DAY3")),
        "stock_eps3": _number(row.get("STOCK_EPS3")),
        "stock_old_eps": _number(row.get("STOCK_OLD_EPS")),
        "stock_net_profit1": _number(row.get("STOCK_NET_PROFIT1")),
        "stock_net_profit2": _number(row.get("STOCK_NET_PROFIT2")),
        "stock_net_profit3": _number(row.get("STOCK_NET_PROFIT3")),
        "stock_settlement_day": _text(row.get("STOCK_SETTLEMENT_DAY")),
        "stock_expected_sales": _number(row.get("STOCK_EXPECTED_SALES")),
        "stock_pre_operating_profit": _number(row.get("STOCK_PRE_OPERATING_PROFIT")),
        "stock_pre_net_income": _number(row.get("STOCK_PRE_NET_INCOME")),
        "stock_pre_eps": _number(row.get("STOCK_PRE_EPS")),
        "stock_pre_per": _number(row.get("STOCK_PRE_PER")),
        "stock_pre_pbr": _number(row.get("STOCK_PRE_PBR")),
        "stock_pre_ev": _number(row.get("STOCK_PRE_EV")),
        "stock_pre_roe": _number(row.get("STOCK_PRE_ROE")),
        "register_date": _date_from_register_date(row.get("REGISTER_DATE")) or file_meta.get("file_register_date") or "",
        "update_date": _date_from_register_date(row.get("UPDATE_DATE")) or "",
        "quality_flags": quality_flags,
        "payload_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        "updated_at": updated_at,
    }


def estimate_records_from_row(row: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    as_of_date = report["report_date"] or report["file_register_date"]
    base = {
        "security_id": report["security_id"],
        "stock_code": report["stock_code"],
        "file_register_date": report["file_register_date"],
        "file_year": report["file_year"],
        "report_idx": report["report_idx"],
        "broker_code": report["publish_code"],
        "broker_name": report["office_name"],
        "analyst_name": report["report_writer"],
        "as_of_date": as_of_date,
        "currency": "KRW",
        "source_provider": "hankyung",
        "quality_flags": report["quality_flags"],
        "updated_at": report["updated_at"],
    }

    pre_target_period = _target_period(row.get("STOCK_SETTLEMENT_DAY"), report.get("file_year"))
    for source_field, metric_id in PRE_METRIC_FIELDS.items():
        value = _number(row.get(source_field))
        if value is None or pre_target_period is None:
            continue
        records.append(
            {
                **base,
                "target_period": pre_target_period,
                "metric_id": metric_id,
                "estimate_value": value,
                "source_field": source_field,
            }
        )

    for index in range(1, 4):
        value = _number(row.get(f"STOCK_EPS{index}"))
        target_period = _target_period(row.get(f"STOCK_SETTLEMENT_DAY{index}"), None)
        if value is None or target_period is None:
            continue
        records.append(
            {
                **base,
                "target_period": target_period,
                "metric_id": "basic_eps",
                "estimate_value": value,
                "source_field": f"STOCK_EPS{index}",
            }
        )

    return records


def build_hankyung_daily_consensus(
    estimates_df: pd.DataFrame,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> pd.DataFrame:
    if estimates_df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    df = estimates_df.copy()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"], errors="coerce")
    df["estimate_value"] = pd.to_numeric(df["estimate_value"], errors="coerce")
    df = df.dropna(subset=["as_of_date", "target_period", "metric_id", "estimate_value"])
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    df["_broker_key"] = df["broker_code"].astype(str).where(
        df["broker_code"].astype(str).str.strip().ne(""),
        df["broker_name"].astype(str) + ":" + df["analyst_name"].astype(str),
    )
    df["_report_sort"] = pd.to_numeric(df.get("report_idx"), errors="coerce").fillna(0)
    rows: list[dict[str, Any]] = []
    updated_at = _updated_at()
    group_columns = ["security_id", "stock_code", "target_period", "metric_id"]
    for key, group in df.sort_values(["as_of_date", "_report_sort"]).groupby(group_columns, dropna=False):
        as_of_dates = sorted(group["as_of_date"].dropna().unique())
        for as_of_date in as_of_dates:
            cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=int(stale_days))
            active = group.loc[(group["as_of_date"] <= as_of_date) & (group["as_of_date"] >= cutoff)].copy()
            active = active.sort_values(["as_of_date", "_report_sort"]).drop_duplicates("_broker_key", keep="last")
            values = pd.to_numeric(active["estimate_value"], errors="coerce").dropna()
            values = values.loc[values.map(math.isfinite)]
            if values.empty:
                continue
            rows.append(
                {
                    "security_id": key[0],
                    "stock_code": key[1],
                    "as_of_date": pd.Timestamp(as_of_date).date().isoformat(),
                    "target_period": key[2],
                    "metric_id": key[3],
                    "consensus_mean": float(values.mean()),
                    "consensus_median": float(values.median()),
                    "consensus_low": float(values.min()),
                    "consensus_high": float(values.max()),
                    "report_count": int(len(values)),
                    "broker_count": int(active["_broker_key"].nunique()),
                    "currency": "KRW",
                    "source_provider": "hankyung",
                    "updated_at": updated_at,
                }
            )

    if not rows:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    return pd.DataFrame(rows, columns=DAILY_COLUMNS).sort_values(
        ["stock_code", "target_period", "metric_id", "as_of_date"]
    ).reset_index(drop=True)


def build_hankyung_target_price_consensus(
    reports_df: pd.DataFrame,
    *,
    lookback_days: int = TARGET_PRICE_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Build PIT target-price states at report and expiry boundaries."""

    if int(lookback_days) <= 0:
        raise ValueError("lookback_days must be positive")
    if reports_df is None or reports_df.empty:
        return pd.DataFrame(columns=TARGET_PRICE_COLUMNS)

    df = reports_df.copy().reset_index(drop=True)
    for column in [
        "security_id",
        "stock_code",
        "report_date",
        "file_register_date",
        "report_idx",
        "office_name",
        "report_writer",
        "target_stock_prices",
    ]:
        if column not in df.columns:
            df[column] = pd.NA

    df["_row_order"] = range(len(df))
    df["_report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["_file_register_date"] = pd.to_datetime(df["file_register_date"], errors="coerce")
    df["_report_date"] = df["_report_date"].fillna(df["_file_register_date"])
    df["_target_price"] = pd.to_numeric(df["target_stock_prices"], errors="coerce")
    df = df.loc[
        df["_report_date"].notna()
        & df["stock_code"].fillna("").astype(str).str.strip().ne("")
        & df["_target_price"].gt(0)
        & df["_target_price"].map(math.isfinite)
    ].copy()
    if df.empty:
        return pd.DataFrame(columns=TARGET_PRICE_COLUMNS)

    broker = df["office_name"].fillna("").astype(str).str.strip().str.casefold()
    analyst = df["report_writer"].fillna("").astype(str).str.strip().str.casefold()
    df["_analyst_key"] = broker + "|" + analyst
    missing_identity = broker.eq("") & analyst.eq("")
    report_idx = df["report_idx"].fillna("").astype(str).str.strip()
    fallback_key = "report:" + report_idx
    fallback_key = fallback_key.where(
        report_idx.ne(""),
        "row:" + df["_row_order"].astype(str),
    )
    df.loc[missing_identity, "_analyst_key"] = fallback_key.loc[missing_identity]
    df["_report_sort"] = pd.to_numeric(df["report_idx"], errors="coerce").fillna(0)

    rows: list[dict[str, Any]] = []
    updated_at = _updated_at()
    lookback = pd.Timedelta(days=int(lookback_days))
    sort_columns = [
        "_report_date",
        "_file_register_date",
        "_report_sort",
        "_row_order",
    ]
    for (security_id, stock_code), group in df.groupby(
        ["security_id", "stock_code"],
        dropna=False,
    ):
        group = group.sort_values(sort_columns).reset_index(drop=True)
        report_events: dict[pd.Timestamp, list[tuple[str, float, int]]] = {}
        expiry_dates: set[pd.Timestamp] = set()
        event_records = group[
            ["_report_date", "_analyst_key", "_target_price"]
        ].itertuples(index=False, name=None)
        for sequence, (
            report_date_value,
            analyst_key_value,
            target_price_value,
        ) in enumerate(event_records):
            report_date = pd.Timestamp(report_date_value)
            analyst_key = str(analyst_key_value)
            target_price = float(target_price_value)
            report_events.setdefault(report_date, []).append(
                (analyst_key, target_price, sequence)
            )
            expiry_dates.add(report_date + lookback)

        boundary_dates = sorted(set(report_events) | expiry_dates)
        current_targets: dict[str, tuple[int, float]] = {}
        expiry_heap: list[tuple[pd.Timestamp, int, str]] = []
        for boundary_date in boundary_dates:
            boundary = pd.Timestamp(boundary_date)
            while expiry_heap and expiry_heap[0][0] <= boundary:
                _, sequence, analyst_key = heapq.heappop(expiry_heap)
                current = current_targets.get(analyst_key)
                if current is not None and current[0] == sequence:
                    del current_targets[analyst_key]

            for analyst_key, target_price, sequence in report_events.get(boundary, []):
                current_targets[analyst_key] = (sequence, target_price)
                heapq.heappush(
                    expiry_heap,
                    (boundary + lookback, sequence, analyst_key),
                )

            values = [target_price for _, target_price in current_targets.values()]
            rows.append(
                {
                    "security_id": security_id,
                    "stock_code": stock_code,
                    "event_date": boundary.date().isoformat(),
                    "target_price_mean": (
                        float(math.fsum(values) / len(values))
                        if values
                        else math.nan
                    ),
                    "target_price_median": float(median(values)) if values else math.nan,
                    "target_price_low": float(min(values)) if values else math.nan,
                    "target_price_high": float(max(values)) if values else math.nan,
                    "analyst_count": int(len(values)),
                    "currency": "KRW",
                    "source_provider": "hankyung",
                    "updated_at": updated_at,
                }
            )

    if not rows:
        return pd.DataFrame(columns=TARGET_PRICE_COLUMNS)
    return pd.DataFrame(rows, columns=TARGET_PRICE_COLUMNS).sort_values(
        ["stock_code", "event_date"]
    ).reset_index(drop=True)


def _write_csv(path: Path, frame: pd.DataFrame, columns: list[str]) -> None:
    output = frame.copy()
    for column in columns:
        if column not in output.columns:
            output[column] = pd.NA
    output = output[columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, encoding="utf-8-sig")


def _quality_flags(row: dict[str, Any], *, stock_code: str, register_date: str) -> str:
    flags = _split_quality_flags(row.get("QUALITY_FLAGS"))
    row_stock_code = _stock_code(row.get("BUSINESS_CODE"))
    row_register_date = str(row.get("REGISTER_DATE") or "").strip()
    if not stock_code:
        flags.append("missing_stock_code")
    if row_stock_code and stock_code and row_stock_code != stock_code:
        flags.append("filename_business_code_mismatch")
    if row_register_date and register_date and row_register_date != register_date:
        flags.append("filename_register_date_mismatch")
    return "|".join(dict.fromkeys(flag for flag in flags if flag))


def _resolve_html_stock_code(row: dict[str, Any], *, stock_name_lookup: dict[str, str]) -> dict[str, Any]:
    result = dict(row)
    if _stock_code(result.get("BUSINESS_CODE")):
        return result

    stock_name = _text(result.get("BUSINESS_NAME"))
    resolved_code = _stock_code(stock_name_lookup.get(_stock_name_key(stock_name)))
    flags = [flag for flag in _split_quality_flags(result.get("QUALITY_FLAGS")) if flag != "missing_stock_code"]
    if resolved_code:
        result["BUSINESS_CODE"] = resolved_code
        flags.append("stock_code_resolved_by_name")
    else:
        flags.append("missing_stock_code")
    result["QUALITY_FLAGS"] = "|".join(dict.fromkeys(flag for flag in flags if flag))
    return result


def _load_kr_stock_name_lookup() -> dict[str, str]:
    try:
        from engine.extractors.market_universe import kospi_kosdaq_corp_list

        frame = kospi_kosdaq_corp_list()
    except Exception:
        return {}
    if frame is None or frame.empty:
        return {}

    lookup: dict[str, str] = {}
    name_columns = [
        column
        for column in ["corp_name", "company_name", "stock_name", "security_name", "name", "title"]
        if column in frame.columns
    ]
    if "stock_code" not in frame.columns or not name_columns:
        return {}
    for row in frame.to_dict("records"):
        stock_code = _stock_code(row.get("stock_code"))
        if not stock_code:
            continue
        for column in name_columns:
            key = _stock_name_key(row.get(column))
            if key and key not in lookup:
                lookup[key] = stock_code
    return lookup


def _split_quality_flags(value: Any) -> list[str]:
    return [flag for flag in str(value or "").split("|") if flag]


def _stock_name_key(value: Any) -> str:
    text = _text(value).lower()
    text = text.replace("(주)", "").replace("㈜", "").replace("주식회사", "")
    return re.sub(r"\s+", "", text)


def _target_period(value: Any, fallback_year: Any = None) -> str | None:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 6:
        return f"{digits[:4]}.{digits[4:6]}"
    if len(digits) == 4:
        return f"{digits}.12"
    if fallback_year:
        year_digits = re.sub(r"\D", "", str(fallback_year))
        if len(year_digits) >= 4:
            return f"{year_digits[:4]}.12"
    return None


def _date_from_register_date(value: Any) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return ""
    return _date_text(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError:
        return ""


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _updated_at() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
