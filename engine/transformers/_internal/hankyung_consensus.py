from __future__ import annotations

from datetime import datetime
import json
import math
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import DATA_LAKE


BRONZE_HANKYUNG_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "hankyung")
SILVER_HANKYUNG_CONSENSUS_DIR = DATA_LAKE.silver("consensus", "hankyung")
SILVER_REPORTS_NAME = "kr_hankyung_consensus_reports.csv"
SILVER_ESTIMATES_NAME = "kr_hankyung_consensus_estimates.csv"
SILVER_DAILY_NAME = "kr_hankyung_consensus_daily.csv"
DEFAULT_STALE_DAYS = 180

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

PRE_METRIC_FIELDS = {
    "STOCK_PRE_EPS": "basic_eps",
    "STOCK_EXPECTED_SALES": "revenue",
    "STOCK_PRE_OPERATING_PROFIT": "operating_income",
    "STOCK_PRE_NET_INCOME": "net_income",
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

    reports_path = output_path / SILVER_REPORTS_NAME
    estimates_path = output_path / SILVER_ESTIMATES_NAME
    daily_path = output_path / SILVER_DAILY_NAME
    _write_csv(reports_path, reports_df, REPORT_COLUMNS)
    _write_csv(estimates_path, estimates_df, ESTIMATE_COLUMNS)
    _write_csv(daily_path, daily_df, DAILY_COLUMNS)

    print(
        "[DONE] hankyung consensus normalize "
        f"reports={len(reports_df):,}, estimates={len(estimates_df):,}, daily={len(daily_df):,}",
        flush=True,
    )
    return {
        "reports_path": reports_path,
        "estimates_path": estimates_path,
        "daily_path": daily_path,
        "reports": len(reports_df),
        "estimates": len(estimates_df),
        "daily": len(daily_df),
    }


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


def report_record_from_row(row: dict[str, Any], file_meta: dict[str, Any]) -> dict[str, Any]:
    stock_code = str(file_meta["stock_code"])
    register_date = str(file_meta.get("register_date") or "")
    quality_flags = _quality_flags(row, stock_code=stock_code, register_date=register_date)
    updated_at = _updated_at()
    return {
        "security_id": f"SEC_KR_{stock_code}",
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


def _write_csv(path: Path, frame: pd.DataFrame, columns: list[str]) -> None:
    output = frame.copy()
    for column in columns:
        if column not in output.columns:
            output[column] = pd.NA
    output = output[columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, encoding="utf-8-sig")


def _quality_flags(row: dict[str, Any], *, stock_code: str, register_date: str) -> str:
    flags = []
    row_stock_code = _stock_code(row.get("BUSINESS_CODE"))
    row_register_date = str(row.get("REGISTER_DATE") or "").strip()
    if row_stock_code and row_stock_code != stock_code:
        flags.append("filename_business_code_mismatch")
    if row_register_date and row_register_date != register_date:
        flags.append("filename_register_date_mismatch")
    return "|".join(flags)


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
