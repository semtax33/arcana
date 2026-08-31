import contextlib
import io
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import yaml

from engine.core.paths import (
    DATA_LAKE,
    PROJECT_ROOT,
    first_existing_path,
    market_csv_name,
)
from engine.core.identifiers import security_id_of
from engine.extractors._internal.yfinance_market_prices import (
    normalize_yfinance_ticker,
    yfinance_price_storage_stem,
    yfinance_price_ticker_from_storage_stem,
)
from engine.markets.us import US_MARKET_CONFIG
from engine.transformers._internal.edgar_identity import (
    configure_edgar_data_directory,
    configure_edgar_identity,
)
from engine.transformers._internal.statement_files import read_statement_period_frames

base_dir = DATA_LAKE.silver("dart", "normalized")
dividend_base_dir = DATA_LAKE.bronze("dart", "dividend")
legacy_dividend_api_base_dir = DATA_LAKE.bronze("dart", "dividend_old")
silver_dividend_dir = DATA_LAKE.silver("dart", "dividend")
legacy_silver_dividend_by_stock_kind_path = silver_dividend_dir / "kr_dividend_by_stock_kind.csv"
silver_dividend_by_stock_kind_path = silver_dividend_dir / market_csv_name("dividend_by_stock_kind")
legacy_silver_dividend_company_summary_path = silver_dividend_dir / "kr_dividend_company_summary.csv"
silver_dividend_company_summary_path = silver_dividend_dir / market_csv_name("dividend_company_summary")
price_file_path = DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price"))
krx_price_file_path = DATA_LAKE.silver("krx", "price", "kr_normalized_price.csv")
us_price_base_dir = DATA_LAKE.bronze("yfinance", "price")
us_sec_notes_dir = DATA_LAKE.bronze("sec", "financial-statement-and-notes-data-set")
us_sec_financial_dir = DATA_LAKE.silver("sec", "normalized")
us_sec_ticker_map_path = DATA_LAKE.meta("sec_company_tickers.csv")
us_dividend_rule_path = DATA_LAKE.rules("us_dividend.yaml")
us_silver_dividend_dir = DATA_LAKE.silver("us", "dividend")
us_dividend_events_path = us_silver_dividend_dir / "us_dividend_events.csv"
us_dividend_normalized_path = us_silver_dividend_dir / market_csv_name("dividend_normalized", market="us")

COMMON_STOCK_KIND_LABELS = {"보통주", "보통주식", "common", "ordinary"}
REPORT_ORDER = {
    "11013": 1,  # q1
    "11012": 2,  # half
    "11014": 3,  # q3
    "11011": 4,  # annual
}
REPORT_NAME_BY_CODE = {
    "11011": "annual",
    "11012": "half",
    "11013": "q1",
    "11014": "q3",
}
DIVIDEND_BY_STOCK_KIND_COLUMNS = [
    "stock_code",
    "corp_code",
    "corp_name",
    "bsns_year",
    "reprt_code",
    "report_name",
    "rcept_no",
    "stlm_dt",
    "stock_knd",
    "market_dividend_yield_pct",
    "per_share_cash_dividend_krw",
    "stock_knd_infer_method",
    "source_file",
]
DIVIDEND_COMPANY_SUMMARY_COLUMNS = [
    "stock_code",
    "corp_code",
    "corp_name",
    "bsns_year",
    "reprt_code",
    "report_name",
    "rcept_no",
    "stlm_dt",
    "dividend_payment_amount_million_krw",
    "dividend_payment_amount_krw",
    "dividend_payout_ratio_pct",
    "source_file",
]
DIVIDEND_SUMMARY_FAILED_COLUMNS = [
    "source_file",
    "reason",
]
US_DIVIDEND_EVENT_COLUMNS = [
    "ticker",
    "cik",
    "company_name",
    "exchange",
    "dividend_declared_date",
    "dividend_record_date",
    "dividend_payment_date",
    "dividend_amount_per_share",
    "sec_filing_date",
    "source_form",
    "annual_dps",
    "annual_eps",
    "payout_ratio_dps_over_eps",
    "payout_ratio_total_dividends_over_net_income",
]
DEFAULT_US_DIVIDEND_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"}


def coerce_dividend_result_dtypes(df):
    for column in ["dividend", "payout_ratio", "dividend_percent"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def calculate_net_income(stock_code, year, month):
    stock_code = normalize_stock_code(stock_code)
    frames = [
        frame
        for frame_year, frame_month, frame in read_statement_period_frames(
            stock_code,
            base_dir,
            market="kr",
            months={int(month)},
        )
        if int(frame_year) == int(year) and int(frame_month) == int(month)
    ]
    file_path = f"{stock_code} {year}.{int(month):02d}"

    if not frames:
        print(f"[SKIP] 파일 없음: {file_path}")
        return None
    
    statement_df = pd.concat(frames, ignore_index=True)
    net_income_matched = statement_df.loc[statement_df["canonical_account_id"] == "NET_INCOME", "normalized_amount"]
    if not net_income_matched.empty:
        net_income = net_income_matched.iloc[0]
    else:
        net_income = None
    
    return net_income


def normalize_dividend_amount(amount):
    if amount is None:
        return 0

    if isinstance(amount, float) and math.isnan(amount):
        return 0

    amount_text = str(amount).strip()
    if not amount_text or amount_text == "-":
        return 0

    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", amount_text)
    if not match:
        return 0

    normalized = match.group(0).replace(",", "")
    value = float(normalized)
    if not math.isfinite(value):
        return 0
    return int(value) if value.is_integer() else value


def normalize_numeric_amount(amount):
    if amount is None:
        return None

    if isinstance(amount, float) and math.isnan(amount):
        return None

    amount_text = str(amount).strip()
    if not amount_text or amount_text == "-":
        return None

    normalized = re.sub(r"[^0-9.-]", "", amount_text)
    if not normalized or normalized in {"-", ".", "-."}:
        return None

    return int(float(normalized))


def normalize_decimal_amount(amount):
    if amount is None:
        return None

    if isinstance(amount, float) and math.isnan(amount):
        return None

    amount_text = str(amount).strip()
    if not amount_text or amount_text == "-":
        return None

    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", amount_text)
    if not match:
        return None

    value = float(match.group(0).replace(",", ""))
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else value


def _relative_source_path(path):
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _normalize_stock_kind(stock_kind):
    text = str(stock_kind or "").strip()
    if text == "보통주식":
        return "보통주"
    if text == "종류주식":
        return "우선주"
    if text in {"-", ""}:
        return ""
    return text


def _infer_report_meta_from_path(file_path):
    reprt_code = ""
    report_name = ""
    bsns_year = None

    if file_path.stem.startswith("finance_statement_dividend_"):
        return bsns_year, reprt_code, report_name

    if file_path.parent.name.isdigit() and len(file_path.parent.name) == 4:
        bsns_year = int(file_path.parent.name)

    stem = file_path.stem
    if "_" in stem:
        maybe_code, maybe_name = stem.split("_", 1)
        if maybe_code in REPORT_NAME_BY_CODE:
            reprt_code = maybe_code
            report_name = maybe_name or REPORT_NAME_BY_CODE[maybe_code]

    return bsns_year, reprt_code, report_name


def _infer_stock_code_from_path(file_path):
    if file_path.parent.name.isdigit() and len(file_path.parent.name) == 4:
        return file_path.parent.parent.name
    return file_path.parent.name


def _base_dividend_row(file_path, data, sample_row=None):
    sample_row = sample_row or {}
    path_year, path_report_code, path_report_name = _infer_report_meta_from_path(file_path)
    stock_code = sample_row.get("stock_code") or data.get("stock_code") or _infer_stock_code_from_path(file_path)
    bsns_year = sample_row.get("bsns_year") or data.get("bsns_year") or path_year
    reprt_code = sample_row.get("reprt_code") or data.get("reprt_code") or path_report_code
    report_name = REPORT_NAME_BY_CODE.get(str(reprt_code), path_report_name)

    return {
        "stock_code": normalize_stock_code(stock_code),
        "corp_code": str(sample_row.get("corp_code") or data.get("corp_code") or "").zfill(8)
        if sample_row.get("corp_code") or data.get("corp_code")
        else "",
        "corp_name": sample_row.get("corp_name") or data.get("corp_name") or "",
        "bsns_year": int(bsns_year) if bsns_year not in {None, ""} else None,
        "reprt_code": str(reprt_code or ""),
        "report_name": str(sample_row.get("report_name") or data.get("report_name") or report_name or ""),
        "rcept_no": str(sample_row.get("rcept_no") or data.get("rcept_no") or ""),
        "stlm_dt": sample_row.get("stlm_dt") or data.get("stlm_dt") or "",
        "source_file": _relative_source_path(file_path),
    }


def _first_non_null(values):
    for value in values:
        normalized = normalize_decimal_amount(value)
        if normalized is not None:
            return normalized
    return None


def _parse_alot_matter_dividend_json(file_path, data):
    records = data.get("list") or []
    if not records:
        return [], []

    by_kind_rows = []
    company_rows = []
    base = _base_dividend_row(file_path, data, records[0])
    grouped = {}

    for row in records:
        item = dict(row)
        item["_se"] = str(item.get("se") or "").strip()
        item["_stock_knd"] = _normalize_stock_kind(item.get("stock_knd"))
        key = item["_stock_knd"]
        grouped.setdefault(key, []).append(item)

    for stock_kind, rows in grouped.items():
        per_share = _first_non_null(
            row.get("thstrm")
            for row in rows
            if "주당" in row["_se"] and "배당" in row["_se"]
        )
        yield_pct = _first_non_null(
            row.get("thstrm")
            for row in rows
            if "배당수익률" in row["_se"]
        )

        if per_share is not None or yield_pct is not None:
            row = {
                **base,
                "stock_knd": stock_kind,
                "market_dividend_yield_pct": yield_pct,
                "per_share_cash_dividend_krw": per_share,
                "stock_knd_infer_method": "raw_or_normalized",
            }
            by_kind_rows.append(row)

    total_million = _first_non_null(
        row.get("thstrm")
        for row in records
        if "배당금총액" in str(row.get("se") or "")
    )
    payout_pct = _first_non_null(
        row.get("thstrm")
        for row in records
        if "배당성향" in str(row.get("se") or "")
    )

    if total_million is not None or payout_pct is not None:
        total_krw = None if total_million is None else int(total_million * 1_000_000)
        company_rows.append(
            {
                **base,
                "dividend_payment_amount_million_krw": total_million,
                "dividend_payment_amount_krw": total_krw,
                "dividend_payout_ratio_pct": payout_pct,
            }
        )

    return by_kind_rows, company_rows


def _parse_decision_dividend_json(file_path, data):
    embedded_stock_code = str(data.get("stock_code") or "").strip()
    rcept_no = str(data.get("rcept_no") or "").strip()
    if not embedded_stock_code or not rcept_no:
        raise ValueError(
            "untrusted dividend decision JSON: embedded stock_code and rcept_no are required"
        )
    source_report_name = str(data.get("source_report_name") or "").replace(" ", "")
    if "자회사의주요경영사항" in source_report_name:
        raise ValueError("subsidiary dividend decision JSON is not an issuer dividend")

    base_date = str(data.get("배당기준일") or "").strip()
    if not base_date:
        return [], []

    base = _base_dividend_row(file_path, data)
    base["bsns_year"] = int(base_date[:4]) if base_date[:4].isdigit() else base["bsns_year"]
    base["report_name"] = base["report_name"] or "dividend_decision"
    base["stlm_dt"] = base_date
    base["rcept_no"] = rcept_no

    by_kind_rows = []
    dps_data = data.get("1주당배당금")
    if isinstance(dps_data, dict):
        dps_items = dps_data.items()
    else:
        dps_items = [("보통주식", dps_data)]

    for stock_kind, dps in dps_items:
        per_share = normalize_decimal_amount(dps)
        if per_share is None:
            continue

        by_kind_rows.append(
            {
                **base,
                "stock_knd": _normalize_stock_kind(stock_kind),
                "market_dividend_yield_pct": None,
                "per_share_cash_dividend_krw": per_share,
                "stock_knd_infer_method": "decision_json",
            }
        )

    total_krw = normalize_decimal_amount(data.get("배당금총액"))
    company_rows = []
    if total_krw is not None:
        company_rows.append(
            {
                **base,
                "dividend_payment_amount_million_krw": total_krw / 1_000_000,
                "dividend_payment_amount_krw": total_krw,
                "dividend_payout_ratio_pct": None,
            }
        )

    return by_kind_rows, company_rows


def _parse_bronze_dividend_json(file_path):
    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return _parse_alot_matter_dividend_json(file_path, data)

    if isinstance(data, dict) and "배당기준일" in data:
        return _parse_decision_dividend_json(file_path, data)

    return [], []


def _latest_text(series):
    values = [str(value) for value in series.dropna().tolist() if str(value)]
    return values[-1] if values else ""


def _deduplicate_decision_events(rows, event_columns):
    if rows.empty:
        return rows

    result = rows.copy()
    result["_rcept_no_numeric"] = pd.to_numeric(result["rcept_no"], errors="coerce")
    result = result.sort_values(
        ["_rcept_no_numeric", "rcept_no", "source_file"],
        na_position="first",
    )
    return result.drop_duplicates(subset=event_columns, keep="last").drop(
        columns=["_rcept_no_numeric"],
        errors="ignore",
    )


def _latest_decision_disclosure(rows):
    ordered = rows.copy()
    ordered["_rcept_no_numeric"] = pd.to_numeric(ordered["rcept_no"], errors="coerce")
    return ordered.sort_values(
        ["_rcept_no_numeric", "rcept_no", "source_file"],
        na_position="first",
    ).iloc[-1]


def _aggregate_decision_rows(by_kind_df, company_df):
    if not by_kind_df.empty:
        decision_mask = by_kind_df["stock_knd_infer_method"] == "decision_json"
        decision_df = by_kind_df.loc[decision_mask].copy()
        other_df = by_kind_df.loc[~decision_mask].copy()

        if not decision_df.empty:
            decision_df = _deduplicate_decision_events(
                decision_df,
                ["stock_code", "corp_code", "bsns_year", "stock_knd", "stlm_dt"],
            )
            grouped_rows = []
            for key, rows in decision_df.groupby(
                ["stock_code", "corp_code", "bsns_year", "stock_knd"],
                dropna=False,
                sort=False,
            ):
                stock_code, corp_code, bsns_year, stock_kind = key
                latest_disclosure = _latest_decision_disclosure(rows)
                grouped_rows.append(
                    {
                        "stock_code": stock_code,
                        "corp_code": corp_code,
                        "corp_name": str(latest_disclosure.get("corp_name") or ""),
                        "bsns_year": bsns_year,
                        "reprt_code": "decision",
                        "report_name": "decision_annual_sum",
                        "rcept_no": str(latest_disclosure.get("rcept_no") or ""),
                        "stlm_dt": _latest_text(rows.sort_values("stlm_dt")["stlm_dt"]),
                        "stock_knd": stock_kind,
                        "market_dividend_yield_pct": None,
                        "per_share_cash_dividend_krw": pd.to_numeric(
                            rows["per_share_cash_dividend_krw"],
                            errors="coerce",
                        ).sum(),
                        "stock_knd_infer_method": "decision_json_annual_sum",
                        "source_file": str(latest_disclosure.get("source_file") or ""),
                    }
                )

            by_kind_df = pd.concat(
                [other_df, pd.DataFrame(grouped_rows, columns=DIVIDEND_BY_STOCK_KIND_COLUMNS)],
                ignore_index=True,
            )

    if not company_df.empty:
        decision_mask = company_df["report_name"] == "dividend_decision"
        decision_df = company_df.loc[decision_mask].copy()
        other_df = company_df.loc[~decision_mask].copy()

        if not decision_df.empty:
            decision_df = _deduplicate_decision_events(
                decision_df,
                ["stock_code", "corp_code", "bsns_year", "stlm_dt"],
            )
            grouped_rows = []
            for key, rows in decision_df.groupby(
                ["stock_code", "corp_code", "bsns_year"],
                dropna=False,
                sort=False,
            ):
                stock_code, corp_code, bsns_year = key
                latest_disclosure = _latest_decision_disclosure(rows)
                total_krw = pd.to_numeric(
                    rows["dividend_payment_amount_krw"],
                    errors="coerce",
                ).sum()
                grouped_rows.append(
                    {
                        "stock_code": stock_code,
                        "corp_code": corp_code,
                        "corp_name": str(latest_disclosure.get("corp_name") or ""),
                        "bsns_year": bsns_year,
                        "reprt_code": "decision",
                        "report_name": "decision_annual_sum",
                        "rcept_no": str(latest_disclosure.get("rcept_no") or ""),
                        "stlm_dt": _latest_text(rows.sort_values("stlm_dt")["stlm_dt"]),
                        "dividend_payment_amount_million_krw": total_krw / 1_000_000,
                        "dividend_payment_amount_krw": total_krw,
                        "dividend_payout_ratio_pct": None,
                        "source_file": str(latest_disclosure.get("source_file") or ""),
                    }
                )

            company_df = pd.concat(
                [other_df, pd.DataFrame(grouped_rows, columns=DIVIDEND_COMPANY_SUMMARY_COLUMNS)],
                ignore_index=True,
            )

    return by_kind_df, company_df


def build_silver_dividend_summary_dataframes(
    bronze_root=None,
):
    if bronze_root is not None:
        bronze_roots = [Path(bronze_root)]
    else:
        bronze_roots = [legacy_dividend_api_base_dir, dividend_base_dir]
    bronze_roots = list(dict.fromkeys(Path(root) for root in bronze_roots))
    by_kind_rows = []
    company_rows = []
    failed_rows = []

    existing_roots = [root for root in bronze_roots if root.exists()]
    if not existing_roots:
        missing_root = bronze_roots[-1]
        return (
            pd.DataFrame(columns=DIVIDEND_BY_STOCK_KIND_COLUMNS),
            pd.DataFrame(columns=DIVIDEND_COMPANY_SUMMARY_COLUMNS),
            pd.DataFrame(
                [{"source_file": str(missing_root), "reason": "bronze_root_not_found"}],
                columns=DIVIDEND_SUMMARY_FAILED_COLUMNS,
            ),
        )

    for root in existing_roots:
        for file_path in sorted(root.rglob("*.json")):
            try:
                parsed_by_kind, parsed_company = _parse_bronze_dividend_json(file_path)
                by_kind_rows.extend(parsed_by_kind)
                company_rows.extend(parsed_company)
            except Exception as exc:
                failed_rows.append(
                    {
                        "source_file": _relative_source_path(file_path),
                        "reason": repr(exc),
                    }
                )

    by_kind_df = pd.DataFrame(by_kind_rows, columns=DIVIDEND_BY_STOCK_KIND_COLUMNS)
    company_df = pd.DataFrame(company_rows, columns=DIVIDEND_COMPANY_SUMMARY_COLUMNS)
    failed_df = pd.DataFrame(failed_rows, columns=DIVIDEND_SUMMARY_FAILED_COLUMNS)
    by_kind_df, company_df = _aggregate_decision_rows(by_kind_df, company_df)

    if not by_kind_df.empty:
        by_kind_df = by_kind_df.sort_values(
            ["stock_code", "bsns_year", "reprt_code", "stock_knd", "rcept_no", "source_file"],
            na_position="last",
        ).reset_index(drop=True)

    if not company_df.empty:
        company_df = company_df.sort_values(
            ["stock_code", "bsns_year", "reprt_code", "rcept_no", "source_file"],
            na_position="last",
        ).reset_index(drop=True)

    return by_kind_df, company_df, failed_df


def write_silver_dividend_summary_files(
    bronze_root=None,
    silver_dir=None,
):
    silver_dir = Path(silver_dir) if silver_dir is not None else silver_dividend_dir
    silver_dir.mkdir(parents=True, exist_ok=True)

    by_kind_df, company_df, failed_df = build_silver_dividend_summary_dataframes(bronze_root)
    by_kind_df.to_csv(silver_dir / market_csv_name("dividend_by_stock_kind"), index=False, encoding="utf-8-sig")
    company_df.to_csv(silver_dir / market_csv_name("dividend_company_summary"), index=False, encoding="utf-8-sig")
    failed_df.to_csv(silver_dir / "kr_dividend_company_summary.failed.csv", index=False, encoding="utf-8-sig")
    clear_silver_dividend_cache()

    return by_kind_df, company_df, failed_df


def _read_silver_csv(path):
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path, dtype={"stock_code": str, "reprt_code": str, "rcept_no": str})
    return df.drop(
        columns=[column for column in df.columns if column.startswith("Unnamed")],
        errors="ignore",
    )


@lru_cache(maxsize=1)
def _silver_dividend_by_stock_kind_df():
    df = _read_silver_csv(
        first_existing_path(
            silver_dividend_by_stock_kind_path,
            legacy_silver_dividend_by_stock_kind_path,
        )
    )
    if df.empty:
        return df

    df["stock_code"] = df["stock_code"].map(normalize_stock_code)
    df["bsns_year"] = pd.to_numeric(df["bsns_year"], errors="coerce")
    df["_stlm_dt"] = pd.to_datetime(df.get("stlm_dt"), errors="coerce")
    df["_report_order"] = df["reprt_code"].map(REPORT_ORDER).fillna(0)
    df["_rcept_no"] = pd.to_numeric(df.get("rcept_no"), errors="coerce")
    return df


@lru_cache(maxsize=1)
def _silver_dividend_company_summary_df():
    df = _read_silver_csv(
        first_existing_path(
            silver_dividend_company_summary_path,
            legacy_silver_dividend_company_summary_path,
        )
    )
    if df.empty:
        return df

    df["stock_code"] = df["stock_code"].map(normalize_stock_code)
    df["bsns_year"] = pd.to_numeric(df["bsns_year"], errors="coerce")
    df["_stlm_dt"] = pd.to_datetime(df.get("stlm_dt"), errors="coerce")
    df["_report_order"] = df["reprt_code"].map(REPORT_ORDER).fillna(0)
    df["_rcept_no"] = pd.to_numeric(df.get("rcept_no"), errors="coerce")
    return df


def _group_by_stock_code(df):
    if df.empty or "stock_code" not in df.columns:
        return {}

    return {
        stock_code: rows.copy()
        for stock_code, rows in df.groupby("stock_code", sort=False)
    }


@lru_cache(maxsize=1)
def _silver_stock_kind_rows_by_stock_code():
    return _group_by_stock_code(_silver_dividend_by_stock_kind_df())


@lru_cache(maxsize=1)
def _silver_company_summary_rows_by_stock_code():
    return _group_by_stock_code(_silver_dividend_company_summary_df())


def clear_silver_dividend_cache():
    _silver_dividend_by_stock_kind_df.cache_clear()
    _silver_dividend_company_summary_df.cache_clear()
    _silver_stock_kind_rows_by_stock_code.cache_clear()
    _silver_company_summary_rows_by_stock_code.cache_clear()
    _silver_stock_kind_year_rows.cache_clear()
    _silver_company_summary_year_rows.cache_clear()


def _share_type_labels(share_type):
    normalized = str(share_type or "").strip().lower()
    labels = {normalized}
    if normalized in COMMON_STOCK_KIND_LABELS:
        labels.update(COMMON_STOCK_KIND_LABELS)
    return {label for label in labels if label}


def _filter_share_type(df, share_type):
    if df.empty or "stock_knd" not in df.columns:
        return df

    stock_kind = df["stock_knd"].fillna("").astype(str).str.strip()
    normalized_stock_kind = stock_kind.str.lower()
    labels = _share_type_labels(share_type)
    matched = df.loc[normalized_stock_kind.isin(labels)]
    if not matched.empty:
        return matched

    if labels & COMMON_STOCK_KIND_LABELS:
        common = df.loc[
            normalized_stock_kind.isin(COMMON_STOCK_KIND_LABELS)
            | stock_kind.str.contains("보통", regex=False)
        ]
        return common

    return df


def _latest_report_row(df):
    if df.empty:
        return None

    sort_columns = [
        column
        for column in ["_stlm_dt", "_report_order", "_rcept_no", "source_file"]
        if column in df.columns
    ]
    if not sort_columns:
        return df.iloc[-1]

    return df.sort_values(sort_columns, na_position="first").iloc[-1]


def _silver_stock_kind_rows(stock_code, share_type="보통주식"):
    stock_code = normalize_stock_code(stock_code)
    rows = _silver_stock_kind_rows_by_stock_code().get(stock_code)
    if rows is None:
        return pd.DataFrame()

    return _filter_share_type(rows, share_type)


@lru_cache(maxsize=None)
def _silver_stock_kind_year_rows(stock_code, share_type="보통주식"):
    rows = _silver_stock_kind_rows(stock_code, share_type)
    if rows.empty:
        return {}

    return {
        int(year): _latest_report_row(year_rows)
        for year, year_rows in rows.groupby("bsns_year", sort=False)
        if not pd.isna(year)
    }


def _silver_company_summary_rows(stock_code):
    stock_code = normalize_stock_code(stock_code)
    rows = _silver_company_summary_rows_by_stock_code().get(stock_code)
    if rows is None:
        return pd.DataFrame()

    return rows


@lru_cache(maxsize=None)
def _silver_company_summary_year_rows(stock_code):
    rows = _silver_company_summary_rows(stock_code)
    if rows.empty:
        return {}

    return {
        int(year): _latest_report_row(year_rows)
        for year, year_rows in rows.groupby("bsns_year", sort=False)
        if not pd.isna(year)
    }


def _silver_stock_kind_row_for_year(stock_code, year, share_type="보통주식"):
    return _silver_stock_kind_year_rows(
        normalize_stock_code(stock_code),
        str(share_type or "").strip(),
    ).get(int(year))


def _silver_company_summary_row_for_year(stock_code, year):
    return _silver_company_summary_year_rows(normalize_stock_code(stock_code)).get(int(year))


def has_silver_dividend_year(stock_code, year, share_type="보통주식"):
    return _silver_stock_kind_row_for_year(stock_code, year, share_type) is not None


def calculate_silver_total_dividend_per_share(stock_code, year, share_type="보통주식"):
    if not first_existing_path(silver_dividend_by_stock_kind_path, legacy_silver_dividend_by_stock_kind_path).exists():
        print(f"[SKIP] 파일 없음: {silver_dividend_by_stock_kind_path}")
        return 0

    row = _silver_stock_kind_row_for_year(stock_code, year, share_type)
    if row is None:
        return 0

    value = normalize_decimal_amount(row.get("per_share_cash_dividend_krw"))
    return 0 if value is None else value


def calculate_silver_total_dividend_amount(stock_code, year):
    if not first_existing_path(silver_dividend_company_summary_path, legacy_silver_dividend_company_summary_path).exists():
        print(f"[SKIP] 파일 없음: {silver_dividend_company_summary_path}")
        return None

    stock_rows = _silver_company_summary_rows(stock_code)
    if stock_rows.empty:
        return None

    row = _silver_company_summary_row_for_year(stock_code, year)
    if row is None:
        return 0

    return normalize_decimal_amount(row.get("dividend_payment_amount_krw"))


def calculate_silver_payout_ratio(stock_code, year):
    row = _silver_company_summary_row_for_year(stock_code, year)
    payout_ratio_pct = None
    if row is not None:
        payout_ratio_pct = normalize_decimal_amount(row.get("dividend_payout_ratio_pct"))

    if payout_ratio_pct is not None:
        payout_ratio = payout_ratio_pct / 100
        return payout_ratio if payout_ratio >= 0 else None

    total_dividend_amount = calculate_silver_total_dividend_amount(stock_code, year)
    net_income = normalize_numeric_amount(calculate_net_income(stock_code, year, 12))

    if total_dividend_amount is None or net_income is None or net_income <= 0:
        return None

    payout_ratio = total_dividend_amount / net_income
    if payout_ratio < 0:
        return None

    return payout_ratio


def find_latest_silver_dividend_year(stock_code, year, share_type="보통주식", min_year=2015):
    for current_year in range(int(year), int(min_year) - 1, -1):
        if has_silver_dividend_year(stock_code, current_year, share_type):
            return current_year

    return None


def calculate_silver_total_dividend_per_share_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    dividend_year = find_latest_silver_dividend_year(stock_code, year, share_type, min_year)
    if dividend_year is None:
        return 0

    return calculate_silver_total_dividend_per_share(stock_code, dividend_year, share_type)


def calculate_silver_payout_ratio_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    for current_year in range(int(year), int(min_year) - 1, -1):
        if not has_silver_dividend_year(stock_code, current_year, share_type):
            continue

        payout_ratio = calculate_silver_payout_ratio(stock_code, current_year)
        if payout_ratio is not None:
            return payout_ratio

    return None


def calculate_total_dividend_amount(stock_code, year):
    return calculate_silver_total_dividend_amount(stock_code, year)


def calculate_payout_ratio(stock_code, year):
    return calculate_silver_payout_ratio(stock_code, year)


def _dividend_record_sort_value(record):
    return (
        str(record.get("dividend_disclosure_date") or ""),
        str(record.get("source_file") or ""),
    )


def deduplicate_dividend_records(records):
    latest_by_event = {}
    for record in records:
        key = (
            str(record.get("dividend_base_date") or ""),
            str(record.get("dividend_type") or ""),
        )
        if not key[0]:
            key = (*key, str(record.get("source_file") or ""))

        previous = latest_by_event.get(key)
        if previous is None or _dividend_record_sort_value(record) >= _dividend_record_sort_value(previous):
            latest_by_event[key] = record

    return sorted(
        latest_by_event.values(),
        key=lambda record: (
            str(record.get("dividend_base_date") or ""),
            str(record.get("dividend_disclosure_date") or ""),
            str(record.get("source_file") or ""),
        ),
    )


def get_dividend_records(stock_code, year, share_type="보통주식"):
    stock_code = normalize_stock_code(stock_code)
    stock_dividend_dir = dividend_base_dir / stock_code

    if not stock_dividend_dir.exists():
        print(f"[SKIP] 파일 없음: {stock_dividend_dir}")
        return []

    records = []
    target_year = str(year)

    for file_path in stock_dividend_dir.glob("finance_statement_dividend_*.json"):
        with file_path.open("r", encoding="utf-8") as f:
            dividend_data = json.load(f)

        dividend_base_date = str(dividend_data.get("배당기준일", ""))
        if not dividend_base_date.startswith(target_year):
            continue

        dividend_per_share_data = dividend_data.get("1주당배당금", {})
        if isinstance(dividend_per_share_data, dict):
            dividend_per_share = dividend_per_share_data.get(share_type)
        else:
            dividend_per_share = dividend_per_share_data

        records.append(
            {
                "stock_code": stock_code,
                "dividend_type": dividend_data.get("배당구분"),
                "dividend_per_share": normalize_dividend_amount(dividend_per_share),
                "total_dividend_amount": normalize_dividend_amount(
                    dividend_data.get("배당금총액")
                ),
                "dividend_base_date": dividend_base_date,
                "dividend_payment_date": dividend_data.get("배당지급일"),
                "dividend_disclosure_date": dividend_data.get("배당공시일"),
                "source_file": file_path.name,
            }
        )

    return deduplicate_dividend_records(records)


def get_dividend_per_share_records(stock_code, year, share_type="보통주식"):
    return get_dividend_records(stock_code, year, share_type)


def calculate_total_dividend_per_share(stock_code, year, share_type="보통주식"):
    return calculate_silver_total_dividend_per_share(stock_code, year, share_type)


def find_latest_dividend_year(stock_code, year, share_type="보통주식", min_year=2015):
    return find_latest_silver_dividend_year(stock_code, year, share_type, min_year)


def calculate_total_dividend_per_share_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    return calculate_silver_total_dividend_per_share_with_fallback(
        stock_code,
        year,
        share_type,
        min_year,
    )


def calculate_payout_ratio_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    return calculate_silver_payout_ratio_with_fallback(
        stock_code,
        year,
        share_type,
        min_year,
    )


def resolve_price_file_path(path=None):
    if path is not None:
        return Path(path)

    if price_file_path.exists():
        return price_file_path

    return krx_price_file_path


def get_daily_stock_prices(stock_code, path=None):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    security_id = f"SEC_KR_{stock_code}"
    resolved_path = resolve_price_file_path(path)

    if not resolved_path.exists():
        print(f"[SKIP] 파일 없음: {resolved_path}")
        return pd.DataFrame()

    price_df = pd.read_csv(resolved_path)
    price_df = price_df.drop(
        columns=[column for column in price_df.columns if column.startswith("Unnamed")],
        errors="ignore",
    )
    price_df = price_df.loc[price_df["security_id"] == security_id].copy()
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    price_df["stock_code"] = stock_code

    return price_df.sort_values("trade_date").reset_index(drop=True)


def _dividend_schema_columns():
    return [
        "security_id",
        "trade_date",
        "dividend",
        "payout_ratio",
        "dividend_percent",
        "currency",
        "updated_at",
    ]


def _pick_column(df, candidates, required=True):
    normalized = {str(column).strip().lower(): column for column in df.columns}
    for candidate in candidates:
        column = normalized.get(str(candidate).lower())
        if column is not None:
            return column
    if required:
        raise ValueError(f"missing required column: {candidates[0]}")
    return None


def _safe_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _normalize_cik(value):
    text = re.sub(r"\D", "", _safe_text(value))
    return str(int(text)) if text else ""


def _normalize_us_dividend_tag(value):
    text = _safe_text(value)
    if ":" in text:
        text = text.split(":", 1)[1]
    return text


def _normalize_us_dividend_date(value):
    text = _safe_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text
    return parsed.strftime("%Y-%m-%d")


def _is_full_iso_date(value):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", _safe_text(value)))


def _read_tsv_if_exists(path, *, usecols=None, chunksize=None):
    path = Path(path)
    if not path.exists():
        if chunksize:
            return []
        return pd.DataFrame()
    return pd.read_csv(
        path,
        sep="\t",
        dtype=str,
        keep_default_na=False,
        usecols=usecols,
        chunksize=chunksize,
    )


def _load_us_dividend_rules(path=None):
    path = Path(path) if path is not None else us_dividend_rule_path
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError(f"fields must be a mapping: {path}")

    for field in [
        "dividend_declared_date",
        "dividend_record_date",
        "dividend_payment_date",
        "dividend_amount_per_share",
    ]:
        if not isinstance(fields.get(field, []), list):
            raise ValueError(f"{field} must be a list: {path}")

    return {
        "allowed_forms": {
            _safe_text(form).upper()
            for form in data.get("allowed_forms", DEFAULT_US_DIVIDEND_FORMS)
            if _safe_text(form)
        },
        "fields": fields,
        "dimension_exclude_patterns": [
            re.compile(pattern)
            for pattern in data.get("dimension_exclude_patterns", [])
        ],
    }


def _us_dividend_field_by_tag(rules):
    result = {}
    for field, items in rules["fields"].items():
        for priority, item in enumerate(items):
            tags = item.get("tags", []) if isinstance(item, dict) else []
            for tag in tags:
                result[_normalize_us_dividend_tag(tag)] = (field, priority)
    return result


def _load_sec_ticker_map(path=None):
    path = Path(path) if path is not None else us_sec_ticker_map_path
    columns = ["cik", "ticker", "title"]
    if not path.exists():
        return pd.DataFrame(columns=columns)

    df = pd.read_csv(path, dtype=str).fillna("")
    lower = {str(column).lower(): column for column in df.columns}
    rename_map = {}
    for canonical, aliases in {
        "cik": ["cik", "cik_str"],
        "ticker": ["ticker", "symbol"],
        "title": ["title", "name", "company_name"],
    }.items():
        for alias in aliases:
            if alias in lower:
                rename_map[lower[alias]] = canonical
                break
    df = df.rename(columns=rename_map)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    df["cik"] = df["cik"].map(_normalize_cik)
    df["ticker"] = df["ticker"].map(lambda value: US_MARKET_CONFIG.normalize_symbol(value))
    return df[columns].drop_duplicates()


def _iter_sec_notes_dirs(notes_root):
    notes_root = Path(notes_root)
    if (notes_root / "sub.tsv").exists():
        yield notes_root
        return
    if not notes_root.exists():
        return
    for child in sorted(notes_root.iterdir()):
        if child.is_dir() and (child / "sub.tsv").exists():
            yield child


def _field_value(field, values):
    if not values:
        return "", False
    best_priority = min(priority for priority, _, _ in values)
    best_values = [
        value
        for priority, value, _ in values
        if priority == best_priority and _safe_text(value)
    ]
    distinct = sorted({_safe_text(value) for value in best_values})
    if not distinct:
        return "", False
    if len(distinct) > 1:
        return "", True
    return distinct[0], False


def _add_group_value(groups, key, field, priority, value, tag):
    if not _safe_text(value):
        return
    groups.setdefault(key, {}).setdefault(field, []).append((priority, value, tag))


def _reports_for_fact(pre_reports, adsh, tag, version):
    reports = pre_reports.get((adsh, tag, version))
    if reports:
        return reports
    reports = pre_reports.get((adsh, tag, ""))
    if reports:
        return reports
    return {""}


def _dimension_allowed(dimh, dim_segments, rules):
    if not dimh or dimh == "0x00000000":
        return True
    segments = dim_segments.get(dimh, "")
    if not segments:
        return True
    return not any(pattern.search(segments) for pattern in rules["dimension_exclude_patterns"])


def _load_dim_segments(notes_dir, dimh_values):
    if not dimh_values or not (Path(notes_dir) / "dim.tsv").exists():
        return {}
    wanted = set(dimh_values)
    segments = {}
    for chunk in _read_tsv_if_exists(
        Path(notes_dir) / "dim.tsv",
        usecols=lambda column: column in {"dimhash", "segments"},
        chunksize=200_000,
    ):
        part = chunk.loc[chunk["dimhash"].isin(wanted)].copy()
        for row in part.to_dict("records"):
            segments[_safe_text(row.get("dimhash"))] = _safe_text(row.get("segments"))
    return segments


def _extract_sec_notes_dividend_events(
    *,
    notes_root=None,
    ticker_map_path=None,
    rules_path=None,
    symbols=None,
):
    notes_root = Path(notes_root) if notes_root is not None else us_sec_notes_dir
    rules = _load_us_dividend_rules(rules_path)
    field_by_tag = _us_dividend_field_by_tag(rules)
    wanted_tags = set(field_by_tag)
    ticker_map = _load_sec_ticker_map(ticker_map_path)
    if ticker_map.empty or not wanted_tags:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)

    wanted_symbols = None
    if symbols is not None:
        wanted_symbols = {US_MARKET_CONFIG.normalize_symbol(symbol) for symbol in symbols}
        ticker_map = ticker_map.loc[ticker_map["ticker"].isin(wanted_symbols)].copy()
    if ticker_map.empty:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)

    ticker_by_cik = dict(zip(ticker_map["cik"], ticker_map["ticker"]))
    title_by_cik = dict(zip(ticker_map["cik"], ticker_map["title"]))
    rows = []

    for notes_dir in _iter_sec_notes_dirs(notes_root):
        sub = _read_tsv_if_exists(
            notes_dir / "sub.tsv",
            usecols=lambda column: column in {"adsh", "cik", "name", "form", "filed"},
        )
        if sub.empty:
            continue
        sub["cik"] = sub["cik"].map(_normalize_cik)
        sub["form"] = sub["form"].astype(str).str.upper()
        sub = sub.loc[
            sub["cik"].isin(ticker_by_cik)
            & sub["form"].isin(rules["allowed_forms"])
        ].copy()
        if sub.empty:
            continue

        submissions = {
            row["adsh"]: {
                "cik": row["cik"],
                "ticker": ticker_by_cik.get(row["cik"], ""),
                "company_name": _safe_text(row.get("name")) or title_by_cik.get(row["cik"], ""),
                "form": row["form"],
                "filed": _normalize_us_dividend_date(row.get("filed")),
                "exchange": "",
            }
            for row in sub.to_dict("records")
        }
        needed_adsh = set(submissions)

        pre_reports = {}
        pre_path = notes_dir / "pre.tsv"
        if pre_path.exists():
            for chunk in _read_tsv_if_exists(
                pre_path,
                usecols=lambda column: column in {"adsh", "report", "tag", "version"},
                chunksize=300_000,
            ):
                tag_series = chunk["tag"].map(_normalize_us_dividend_tag)
                part = chunk.loc[chunk["adsh"].isin(needed_adsh) & tag_series.isin(wanted_tags)].copy()
                if part.empty:
                    continue
                part["_tag"] = part["tag"].map(_normalize_us_dividend_tag)
                for row in part.to_dict("records"):
                    key = (_safe_text(row.get("adsh")), _safe_text(row.get("_tag")), _safe_text(row.get("version")))
                    pre_reports.setdefault(key, set()).add(_safe_text(row.get("report")))

        groups = {}
        dimh_values = set()
        num_path = notes_dir / "num.tsv"
        if num_path.exists():
            for chunk in _read_tsv_if_exists(
                num_path,
                usecols=lambda column: column in {"adsh", "tag", "version", "ddate", "uom", "dimh", "value"},
                chunksize=300_000,
            ):
                tag_series = chunk["tag"].map(_normalize_us_dividend_tag)
                part = chunk.loc[chunk["adsh"].isin(needed_adsh) & tag_series.isin(wanted_tags)].copy()
                if part.empty:
                    continue
                part["_tag"] = part["tag"].map(_normalize_us_dividend_tag)
                for row in part.to_dict("records"):
                    tag = _safe_text(row.get("_tag"))
                    field, priority = field_by_tag[tag]
                    if field != "dividend_amount_per_share":
                        continue
                    amount = normalize_decimal_amount(row.get("value"))
                    if amount is None or amount <= 0:
                        continue
                    adsh = _safe_text(row.get("adsh"))
                    version = _safe_text(row.get("version"))
                    dimh = _safe_text(row.get("dimh"))
                    dimh_values.add(dimh)
                    for report in _reports_for_fact(pre_reports, adsh, tag, version):
                        _add_group_value(groups, (adsh, report, dimh), field, priority, amount, tag)

        txt_path = notes_dir / "txt.tsv"
        if txt_path.exists():
            meta_tags = {"SecurityExchangeName", "TradingSymbol"}
            for chunk in _read_tsv_if_exists(
                txt_path,
                usecols=lambda column: column in {"adsh", "tag", "version", "ddate", "dimh", "value"},
                chunksize=300_000,
            ):
                tag_series = chunk["tag"].map(_normalize_us_dividend_tag)
                part = chunk.loc[
                    chunk["adsh"].isin(needed_adsh)
                    & (tag_series.isin(wanted_tags) | tag_series.isin(meta_tags))
                ].copy()
                if part.empty:
                    continue
                part["_tag"] = part["tag"].map(_normalize_us_dividend_tag)
                for row in part.to_dict("records"):
                    tag = _safe_text(row.get("_tag"))
                    adsh = _safe_text(row.get("adsh"))
                    value = _safe_text(row.get("value"))
                    if tag == "SecurityExchangeName":
                        submissions[adsh]["exchange"] = value
                        continue
                    if tag == "TradingSymbol":
                        if value and not submissions[adsh]["ticker"]:
                            submissions[adsh]["ticker"] = US_MARKET_CONFIG.normalize_symbol(value)
                        continue
                    field, priority = field_by_tag[tag]
                    if field == "dividend_amount_per_share":
                        continue
                    version = _safe_text(row.get("version"))
                    dimh = _safe_text(row.get("dimh"))
                    dimh_values.add(dimh)
                    date_value = _normalize_us_dividend_date(value)
                    for report in _reports_for_fact(pre_reports, adsh, tag, version):
                        _add_group_value(groups, (adsh, report, dimh), field, priority, date_value, tag)

        dim_segments = _load_dim_segments(notes_dir, dimh_values)
        for (adsh, _, dimh), values_by_field in groups.items():
            if not _dimension_allowed(dimh, dim_segments, rules):
                continue
            amount, amount_ambiguous = _field_value("dividend_amount_per_share", values_by_field.get("dividend_amount_per_share", []))
            payment_date, payment_ambiguous = _field_value("dividend_payment_date", values_by_field.get("dividend_payment_date", []))
            if amount_ambiguous or payment_ambiguous or not amount or not payment_date:
                continue
            declared_date, declared_ambiguous = _field_value("dividend_declared_date", values_by_field.get("dividend_declared_date", []))
            record_date, record_ambiguous = _field_value("dividend_record_date", values_by_field.get("dividend_record_date", []))
            if declared_ambiguous or record_ambiguous:
                continue
            sub_row = submissions.get(adsh, {})
            rows.append(
                {
                    "ticker": sub_row.get("ticker", ""),
                    "cik": sub_row.get("cik", ""),
                    "company_name": sub_row.get("company_name", ""),
                    "exchange": sub_row.get("exchange", ""),
                    "dividend_declared_date": declared_date,
                    "dividend_record_date": record_date,
                    "dividend_payment_date": payment_date,
                    "dividend_amount_per_share": amount,
                    "sec_filing_date": sub_row.get("filed", ""),
                    "source_form": sub_row.get("form", ""),
                    "_source": "sec_notes",
                }
            )

    return pd.DataFrame(rows)


def _default_us_dividend_edgartools_provider(symbol, cik, company_name, rules):
    configure_edgar_data_directory()
    try:
        from edgar import Company, set_identity  # type: ignore
    except Exception as exc:
        print(f"[WARN] edgartools dividend fallback skipped for {symbol}: import failed ({type(exc).__name__})")
        return []

    try:
        configure_edgar_identity(set_identity)
    except Exception:
        pass

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            company = Company(symbol or int(cik))
            facts_obj = getattr(company, "facts", None)
            facts_obj = facts_obj() if callable(facts_obj) else facts_obj
            to_pandas = getattr(facts_obj, "to_pandas", None)
            if callable(to_pandas):
                return to_pandas().to_dict("records")
            if isinstance(facts_obj, pd.DataFrame):
                return facts_obj.to_dict("records")
    except Exception as exc:
        message = str(exc)
        expected_empty_fact_error = (
            "No company facts found" in message
            or "No facts found" in message
            or type(exc).__name__ in {"NoCompanyFactsFound", "NoFactsFound"}
        )
        if not expected_empty_fact_error:
            print(f"[WARN] edgartools dividend fallback skipped for {symbol}: {type(exc).__name__}: {exc}")
    return []


def _extract_edgartools_dividend_events(
    *,
    ticker_map_path=None,
    rules_path=None,
    symbols=None,
    provider=None,
):
    provider = provider or _default_us_dividend_edgartools_provider
    rules = _load_us_dividend_rules(rules_path)
    field_by_tag = _us_dividend_field_by_tag(rules)
    ticker_map = _load_sec_ticker_map(ticker_map_path)
    if symbols is not None:
        wanted_symbols = {US_MARKET_CONFIG.normalize_symbol(symbol) for symbol in symbols}
        ticker_map = ticker_map.loc[ticker_map["ticker"].isin(wanted_symbols)].copy()
    rows = []
    attempted_count = 0
    empty_result_count = 0
    for item in ticker_map.to_dict("records"):
        ticker = item["ticker"]
        cik = item["cik"]
        company_name = item["title"]
        attempted_count += 1
        raw_rows = provider(ticker, cik, company_name, rules)
        if not raw_rows:
            empty_result_count += 1
            continue
        event_rows = []
        grouped = {}
        for raw in raw_rows:
            if "dividend_amount_per_share" in raw or "dividend_payment_date" in raw:
                event_rows.append(raw)
                continue
            lower = {str(key).lower(): key for key in raw}
            tag_key = next((lower[name] for name in ["tag", "concept", "name"] if name in lower), None)
            value_key = next((lower[name] for name in ["value", "val"] if name in lower), None)
            if tag_key is None or value_key is None:
                continue
            tag = _normalize_us_dividend_tag(raw.get(tag_key))
            if tag not in field_by_tag:
                continue
            field, priority = field_by_tag[tag]
            accn = _safe_text(raw.get(lower.get("accn", "")) or raw.get(lower.get("accession", "")) or raw.get(lower.get("adsh", "")))
            dimh = _safe_text(raw.get(lower.get("dimh", "")))
            key = (accn, dimh)
            value = normalize_decimal_amount(raw.get(value_key)) if field == "dividend_amount_per_share" else _normalize_us_dividend_date(raw.get(value_key))
            _add_group_value(grouped, key, field, priority, value, tag)

        for raw in event_rows:
            rows.append(
                {
                    "ticker": US_MARKET_CONFIG.normalize_symbol(raw.get("ticker") or raw.get("symbol") or ticker),
                    "cik": _normalize_cik(raw.get("cik") or cik),
                    "company_name": _safe_text(raw.get("company_name") or raw.get("entity_name") or company_name),
                    "exchange": _safe_text(raw.get("exchange")),
                    "dividend_declared_date": _normalize_us_dividend_date(raw.get("dividend_declared_date")),
                    "dividend_record_date": _normalize_us_dividend_date(raw.get("dividend_record_date")),
                    "dividend_payment_date": _normalize_us_dividend_date(raw.get("dividend_payment_date")),
                    "dividend_amount_per_share": normalize_decimal_amount(raw.get("dividend_amount_per_share")),
                    "sec_filing_date": _normalize_us_dividend_date(raw.get("sec_filing_date") or raw.get("filed")),
                    "source_form": _safe_text(raw.get("source_form") or raw.get("form")),
                    "_source": "edgartools",
                }
            )

        for values_by_field in grouped.values():
            amount, amount_ambiguous = _field_value("dividend_amount_per_share", values_by_field.get("dividend_amount_per_share", []))
            payment_date, payment_ambiguous = _field_value("dividend_payment_date", values_by_field.get("dividend_payment_date", []))
            if amount_ambiguous or payment_ambiguous or not amount or not payment_date:
                continue
            declared_date, declared_ambiguous = _field_value("dividend_declared_date", values_by_field.get("dividend_declared_date", []))
            record_date, record_ambiguous = _field_value("dividend_record_date", values_by_field.get("dividend_record_date", []))
            if declared_ambiguous or record_ambiguous:
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "company_name": company_name,
                    "exchange": "",
                    "dividend_declared_date": declared_date,
                    "dividend_record_date": record_date,
                    "dividend_payment_date": payment_date,
                    "dividend_amount_per_share": amount,
                    "sec_filing_date": "",
                    "source_form": "",
                    "_source": "edgartools",
                }
            )

    if attempted_count and empty_result_count:
        print(
            "[INFO] edgartools dividend fallback skipped symbols with no usable facts "
            f"symbols={empty_result_count}/{attempted_count}"
        )

    return pd.DataFrame(rows)


def _extract_yfinance_dividend_events(
    *,
    ticker_map_path=None,
    symbols=None,
    price_dir=None,
    exclude_tickers=None,
):
    ticker_map = _load_sec_ticker_map(ticker_map_path)
    if symbols is not None:
        wanted_symbols = {US_MARKET_CONFIG.normalize_symbol(symbol) for symbol in symbols}
        ticker_map = ticker_map.loc[ticker_map["ticker"].isin(wanted_symbols)].copy()
    excluded = {US_MARKET_CONFIG.normalize_symbol(ticker) for ticker in (exclude_tickers or [])}
    if excluded:
        ticker_map = ticker_map.loc[~ticker_map["ticker"].isin(excluded)].copy()
    if ticker_map.empty:
        return pd.DataFrame(columns=[*US_DIVIDEND_EVENT_COLUMNS, "_source"])

    price_dir = Path(price_dir) if price_dir is not None else us_price_base_dir
    rows = []
    symbols_with_dividends = set()
    for item in ticker_map.to_dict("records"):
        ticker = US_MARKET_CONFIG.normalize_symbol(item.get("ticker"))
        if not ticker:
            continue
        file_path = price_dir / f"{yfinance_price_storage_stem(ticker)}.csv"
        if not file_path.exists():
            continue
        try:
            raw = pd.read_csv(file_path)
            if raw.empty:
                continue
            date_col = _pick_column(raw, ["Date", "Datetime", "trade_date", "date", "index"], required=False)
            dividend_col = _pick_column(raw, ["Dividends", "dividends", "dividend"], required=False)
            if date_col is None or dividend_col is None:
                continue
            frame = pd.DataFrame(
                {
                    "date": pd.to_datetime(raw[date_col], errors="coerce"),
                    "dividend": pd.to_numeric(raw[dividend_col], errors="coerce"),
                }
            )
            frame = frame.dropna(subset=["date", "dividend"])
            frame = frame.loc[frame["dividend"] > 0].copy()
            if frame.empty:
                continue
        except Exception as exc:
            print(f"[WARN] yfinance dividend fallback skipped for {ticker}: {type(exc).__name__}: {exc}")
            continue

        symbols_with_dividends.add(ticker)
        for row in frame.to_dict("records"):
            dividend_date = row["date"].strftime("%Y-%m-%d")
            rows.append(
                {
                    "ticker": ticker,
                    "cik": _normalize_cik(item.get("cik")),
                    "company_name": _safe_text(item.get("title")),
                    "exchange": "",
                    "dividend_declared_date": "",
                    "dividend_record_date": "",
                    "dividend_payment_date": dividend_date,
                    "dividend_amount_per_share": float(row["dividend"]),
                    "sec_filing_date": "",
                    "source_form": "YFINANCE",
                    "_source": "yfinance",
                }
            )

    if rows:
        print(
            "[INFO] yfinance dividend fallback "
            f"symbols={len(symbols_with_dividends)}, events={len(rows)}"
        )
    return pd.DataFrame(rows)


def _read_us_annual_financial_metrics(tickers, financial_dir=None):
    financial_dir = Path(financial_dir) if financial_dir is not None else us_sec_financial_dir
    result = {}
    for ticker in sorted({US_MARKET_CONFIG.normalize_symbol(ticker) for ticker in tickers if _safe_text(ticker)}):
        path = financial_dir / f"us_normalized_{ticker}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        required = {"canonical_account_id", "normalized_amount", "fiscal_year"}
        if not required.issubset(df.columns):
            continue
        df["fiscal_year"] = pd.to_numeric(df["fiscal_year"], errors="coerce")
        df["fiscal_month"] = pd.to_numeric(df.get("fiscal_month"), errors="coerce")
        df["normalized_amount"] = pd.to_numeric(df["normalized_amount"], errors="coerce")
        for year, year_df in df.dropna(subset=["fiscal_year"]).groupby("fiscal_year"):
            year_key = int(year)
            by_id = {}
            for account_id, account_df in year_df.groupby("canonical_account_id", sort=False):
                ordered = account_df.sort_values("fiscal_month", na_position="first")
                values = ordered["normalized_amount"].dropna()
                if not values.empty:
                    by_id[str(account_id)] = float(values.iloc[-1])
            eps = by_id.get("DILUTED_EPS")
            if eps is None:
                eps = by_id.get("BASIC_EPS")
            net_income = by_id.get("NET_INCOME")
            div_paid = by_id.get("DIV_PAID")
            result[(ticker, year_key)] = {
                "annual_eps": eps,
                "payout_ratio_total_dividends_over_net_income": (
                    abs(div_paid) / net_income
                    if div_paid is not None and net_income is not None and net_income > 0
                    else None
                ),
            }
    return result


def _dedupe_us_dividend_events(df):
    if df.empty:
        return pd.DataFrame(columns=[*US_DIVIDEND_EVENT_COLUMNS, "_source"])
    df = df.copy()
    df["ticker"] = df["ticker"].map(lambda value: US_MARKET_CONFIG.normalize_symbol(value))
    df["cik"] = df["cik"].map(_normalize_cik)
    df["dividend_amount_per_share"] = pd.to_numeric(df["dividend_amount_per_share"], errors="coerce")
    df = df.dropna(subset=["dividend_amount_per_share"])
    df = df.loc[(df["ticker"] != "") & (df["cik"] != "") & (df["dividend_payment_date"] != "")]
    if df.empty:
        return pd.DataFrame(columns=[*US_DIVIDEND_EVENT_COLUMNS, "_source"])
    df["_amount_key"] = df["dividend_amount_per_share"].map(lambda value: format(float(value), ".12g"))
    source_series = (
        df["_source"]
        if "_source" in df.columns
        else pd.Series([""] * len(df), index=df.index)
    )
    df["_source_rank"] = source_series.map({"sec_notes": 0, "edgartools": 1, "yfinance": 2}).fillna(9)
    df = df.sort_values(
        ["_source_rank", "sec_filing_date", "source_form"],
        ascending=[True, False, False],
    )
    df = (
        df.groupby(["ticker", "cik", "dividend_payment_date", "_amount_key"], as_index=False, sort=False)
        .head(1)
        .sort_values(["ticker", "dividend_payment_date", "sec_filing_date"])
        .reset_index(drop=True)
    )
    return df.drop(columns=["_amount_key", "_source_rank"], errors="ignore")


def _add_us_annual_dividend_metrics(df, financial_dir=None):
    if df.empty:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    df = df.copy()
    df["_payment_year"] = df["dividend_payment_date"].astype(str).str.extract(r"^(\d{4})")[0]
    df["_payment_year"] = pd.to_numeric(df["_payment_year"], errors="coerce")
    dps_by_year = (
        df.dropna(subset=["_payment_year"])
        .groupby(["ticker", "_payment_year"], dropna=False)["dividend_amount_per_share"]
        .sum()
        .to_dict()
    )
    financials = _read_us_annual_financial_metrics(df["ticker"].dropna().unique(), financial_dir)

    annual_dps = []
    annual_eps = []
    payout_eps = []
    payout_total = []
    for row in df.to_dict("records"):
        year = row.get("_payment_year")
        year_key = int(year) if not pd.isna(year) else None
        ticker = row.get("ticker")
        dps = dps_by_year.get((ticker, year)) if year_key is not None else None
        metrics = financials.get((ticker, year_key), {}) if year_key is not None else {}
        eps = metrics.get("annual_eps")
        annual_dps.append(dps)
        annual_eps.append(eps)
        payout_eps.append(dps / eps if dps is not None and eps is not None and eps > 0 else None)
        payout_total.append(metrics.get("payout_ratio_total_dividends_over_net_income"))

    df["annual_dps"] = annual_dps
    df["annual_eps"] = annual_eps
    df["payout_ratio_dps_over_eps"] = payout_eps
    df["payout_ratio_total_dividends_over_net_income"] = payout_total
    return df[US_DIVIDEND_EVENT_COLUMNS]


def build_us_sec_dividend_events_dataframe(
    *,
    notes_root=None,
    ticker_map_path=None,
    rules_path=None,
    financial_dir=None,
    symbols=None,
    use_edgartools=True,
    edgartools_provider=None,
    use_yfinance_fallback=True,
    yfinance_price_dir=None,
):
    frames = [
        _extract_sec_notes_dividend_events(
            notes_root=notes_root,
            ticker_map_path=ticker_map_path,
            rules_path=rules_path,
            symbols=symbols,
        )
    ]
    if use_edgartools:
        frames.append(
            _extract_edgartools_dividend_events(
                ticker_map_path=ticker_map_path,
                rules_path=rules_path,
                symbols=symbols,
                provider=edgartools_provider,
            )
        )
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if non_empty:
        events = _dedupe_us_dividend_events(pd.concat(non_empty, ignore_index=True))
    else:
        events = pd.DataFrame(columns=[*US_DIVIDEND_EVENT_COLUMNS, "_source"])

    if use_yfinance_fallback:
        covered_tickers = (
            set(events["ticker"].dropna().astype(str))
            if "ticker" in events.columns and not events.empty
            else set()
        )
        yfinance_events = _extract_yfinance_dividend_events(
            ticker_map_path=ticker_map_path,
            symbols=symbols,
            price_dir=yfinance_price_dir,
            exclude_tickers=covered_tickers,
        )
        if yfinance_events is not None and not yfinance_events.empty:
            events = _dedupe_us_dividend_events(
                pd.concat([events, yfinance_events], ignore_index=True)
            )

    if events.empty:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    return _add_us_annual_dividend_metrics(events, financial_dir)


def write_us_sec_dividend_events_file(
    *,
    output_path=None,
    notes_root=None,
    ticker_map_path=None,
    rules_path=None,
    financial_dir=None,
    symbols=None,
    use_edgartools=True,
    edgartools_provider=None,
    use_yfinance_fallback=True,
    yfinance_price_dir=None,
):
    output_path = Path(output_path) if output_path is not None else us_dividend_events_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events = build_us_sec_dividend_events_dataframe(
        notes_root=notes_root,
        ticker_map_path=ticker_map_path,
        rules_path=rules_path,
        financial_dir=financial_dir,
        symbols=symbols,
        use_edgartools=use_edgartools,
        edgartools_provider=edgartools_provider,
        use_yfinance_fallback=use_yfinance_fallback,
        yfinance_price_dir=yfinance_price_dir,
    )
    events.to_csv(output_path, index=False, encoding="utf-8-sig")
    return events


def _read_us_dividend_events(path=None):
    path = Path(path) if path is not None else us_dividend_events_path
    if not path.exists():
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    df = pd.read_csv(path, dtype={"ticker": str, "cik": str}).drop(
        columns=[column for column in pd.read_csv(path, nrows=0).columns if str(column).startswith("Unnamed")],
        errors="ignore",
    )
    for column in US_DIVIDEND_EVENT_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    return df[US_DIVIDEND_EVENT_COLUMNS]


def _us_dividend_events_to_daily_frame(events, stock_code=None):
    schema_columns = _dividend_schema_columns()
    if events.empty:
        return pd.DataFrame(columns=schema_columns)
    df = events.copy()
    df["ticker"] = df["ticker"].map(lambda value: US_MARKET_CONFIG.normalize_symbol(value))
    if stock_code is not None:
        ticker = US_MARKET_CONFIG.normalize_symbol(stock_code)
        df = df.loc[df["ticker"] == ticker].copy()
    df["trade_date"] = df["dividend_payment_date"].map(_normalize_us_dividend_date)
    df = df.loc[df["trade_date"].map(_is_full_iso_date)].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["dividend"] = pd.to_numeric(df["dividend_amount_per_share"], errors="coerce")
    df = df.dropna(subset=["trade_date", "dividend"])
    df = df.loc[df["dividend"] > 0].copy()
    if df.empty:
        return pd.DataFrame(columns=schema_columns)
    df["security_id"] = df["ticker"].map(lambda value: security_id_of(value, US_MARKET_CONFIG))
    df["payout_ratio"] = pd.to_numeric(df["payout_ratio_dps_over_eps"], errors="coerce")
    df["dividend_percent"] = pd.NA
    df["currency"] = US_MARKET_CONFIG.currency
    df["updated_at"] = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    df = df[schema_columns]
    return coerce_dividend_result_dtypes(df).sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def _read_yfinance_dividend_frame(file_path, price_column="Close"):
    file_path = Path(file_path)
    ticker = yfinance_price_ticker_from_storage_stem(file_path.stem)
    raw = pd.read_csv(file_path)
    if raw.empty:
        return pd.DataFrame(columns=_dividend_schema_columns())

    date_col = _pick_column(raw, ["Date", "Datetime", "trade_date", "date", "index"])
    dividend_col = _pick_column(raw, ["Dividends", "dividends", "dividend"], required=False)
    close_col = _pick_column(raw, [price_column, "Close", "close", "Adj Close", "adj_close"], required=False)
    if dividend_col is None:
        return pd.DataFrame(columns=_dividend_schema_columns())

    close_values = (
        pd.to_numeric(raw[close_col], errors="coerce")
        if close_col
        else pd.Series([pd.NA] * len(raw), index=raw.index)
    )
    df = pd.DataFrame(
        {
            "security_id": security_id_of(ticker, US_MARKET_CONFIG),
            "trade_date": pd.to_datetime(raw[date_col], errors="coerce"),
            "dividend": pd.to_numeric(raw[dividend_col], errors="coerce"),
            "_close": close_values,
        }
    )
    df = df.dropna(subset=["trade_date", "dividend"])
    df = df.loc[df["dividend"] > 0].copy()
    if df.empty:
        return pd.DataFrame(columns=_dividend_schema_columns())

    updated_at = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    df["payout_ratio"] = pd.NA
    df["dividend_percent"] = (df["dividend"] / df["_close"]) * 100
    df.loc[pd.to_numeric(df["_close"], errors="coerce") <= 0, "dividend_percent"] = pd.NA
    df["currency"] = US_MARKET_CONFIG.currency
    df["updated_at"] = updated_at
    df = df[_dividend_schema_columns()]
    return coerce_dividend_result_dtypes(df).sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def create_us_stock_dividend_dataframe(
    stock_code=None,
    *,
    path=None,
    price_column="Close",
):
    schema_columns = _dividend_schema_columns()
    if path is None:
        return _us_dividend_events_to_daily_frame(_read_us_dividend_events(), stock_code)

    source_path = Path(path)
    if source_path.exists():
        header = pd.read_csv(source_path, nrows=0)
        if {"dividend_payment_date", "dividend_amount_per_share"}.issubset(header.columns):
            return _us_dividend_events_to_daily_frame(_read_us_dividend_events(source_path), stock_code)

    files = [source_path]

    frames = []
    for file_path in files:
        if not file_path.exists():
            continue
        frames.append(_read_yfinance_dividend_frame(file_path, price_column=price_column))

    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=schema_columns)
    return pd.concat(non_empty, ignore_index=True).sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def calculate_daily_dividend_yield(
    stock_code,
    year,
    share_type="보통주식",
    price_column="close",
    path=None,
):
    import pandas as pd

    total_dividend_per_share = calculate_total_dividend_per_share_with_fallback(
        stock_code,
        year,
        share_type,
    )
    price_df = get_daily_stock_prices(stock_code, path)

    if price_df.empty:
        return price_df

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    daily_dividend_yield_df = price_df.loc[
        (price_df["trade_date"].dt.year == int(year))
        & (price_df["trade_date"].dt.date <= today)
    ].copy()

    if daily_dividend_yield_df.empty:
        return daily_dividend_yield_df

    daily_dividend_yield_df[price_column] = pd.to_numeric(
        daily_dividend_yield_df[price_column],
        errors="coerce",
    )
    daily_dividend_yield_df["annual_dividend_per_share"] = total_dividend_per_share
    daily_dividend_yield_df["dividend_yield"] = (
        daily_dividend_yield_df["annual_dividend_per_share"]
        / daily_dividend_yield_df[price_column]
    )
    daily_dividend_yield_df["dividend_yield_percent"] = (
        daily_dividend_yield_df["dividend_yield"] * 100
    )

    return daily_dividend_yield_df


def create_stock_dividend_dataframe(
    stock_code,
    year=None,
    share_type="보통주식",
    price_column="close",
    path=None,
    *,
    market="kr",
):
    import pandas as pd

    market = str(market or "kr").strip().lower()
    if market == "us":
        return create_us_stock_dividend_dataframe(stock_code, path=path, price_column=price_column)
    if market != "kr":
        raise ValueError(f"unsupported market: {market}")

    stock_code = normalize_stock_code(stock_code)
    price_df = get_daily_stock_prices(stock_code, path)

    schema_columns = _dividend_schema_columns()

    if price_df.empty:
        return pd.DataFrame(columns=schema_columns)

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    price_df = price_df.loc[price_df["trade_date"].dt.date <= today].copy()
    price_df[price_column] = pd.to_numeric(price_df[price_column], errors="coerce")
    price_df["year"] = price_df["trade_date"].dt.year

    if year is not None:
        price_df = price_df.loc[price_df["year"] == int(year)].copy()

    if price_df.empty:
        return pd.DataFrame(columns=schema_columns)

    years = sorted(price_df["year"].dropna().astype(int).unique())
    dividend_by_year = {
        current_year: calculate_total_dividend_per_share_with_fallback(
            stock_code,
            current_year,
            share_type,
        )
        for current_year in years
    }
    payout_ratio_by_year = {
        current_year: calculate_payout_ratio_with_fallback(
            stock_code,
            current_year,
            share_type,
        )
        for current_year in years
    }

    updated_at = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    result_df = price_df[["security_id", "trade_date", price_column, "currency", "year"]].copy()
    result_df["dividend"] = result_df["year"].map(dividend_by_year)
    result_df["payout_ratio"] = result_df["year"].map(payout_ratio_by_year)
    result_df["dividend_percent"] = (result_df["dividend"] / result_df[price_column]) * 100
    result_df["currency"] = "KRW"
    result_df["updated_at"] = updated_at

    result_df = result_df[schema_columns]
    result_df = coerce_dividend_result_dtypes(result_df)
    return result_df.sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def create_all_stock_dividend_dataframe(
    share_type="보통주식",
    price_column="close",
    *,
    market="kr",
    path=None,
):
    market = str(market or "kr").strip().lower()
    if market == "us":
        return create_us_stock_dividend_dataframe(path=path, price_column=price_column)
    if market != "kr":
        raise ValueError(f"unsupported market: {market}")

    schema_columns = _dividend_schema_columns()
    resolved_path = resolve_price_file_path(path)

    if not resolved_path.exists():
        print(f"[SKIP] 파일 없음: {resolved_path}")
        return pd.DataFrame(columns=schema_columns)

    price_df = pd.read_csv(resolved_path)
    price_df = price_df.drop(
        columns=[column for column in price_df.columns if column.startswith("Unnamed")],
        errors="ignore",
    )
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    price_df = price_df.loc[price_df["trade_date"].dt.date <= today].copy()
    price_df[price_column] = pd.to_numeric(price_df[price_column], errors="coerce")
    price_df["year"] = price_df["trade_date"].dt.year

    updated_at = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    result_dfs = []

    # The normalized price file is the authoritative local universe for this
    # transformation.  Avoid an unrelated online KRX universe lookup so a
    # Silver rebuild remains reproducible when KRX credentials are unavailable.
    # Group once instead of scanning the full price frame for every stock.
    for security_id, stock_price_df in price_df.groupby("security_id", sort=True):
        security_id = str(security_id).strip()
        if not security_id.startswith("SEC_KR_"):
            continue
        stock_code = normalize_stock_code(security_id.removeprefix("SEC_KR_"))
        stock_price_df = stock_price_df.copy()

        for year in range(2015, today.year + 1):
            yearly_price_df = stock_price_df.loc[
                stock_price_df["year"] == year
            ].copy()

            if yearly_price_df.empty:
                continue

            dividend = calculate_total_dividend_per_share_with_fallback(
                stock_code,
                year,
                share_type,
            )
            payout_ratio = calculate_payout_ratio_with_fallback(
                stock_code,
                year,
                share_type,
            )

            yearly_result_df = yearly_price_df[[
                "security_id",
                "trade_date",
                price_column,
            ]].copy()
            yearly_result_df["dividend"] = dividend
            yearly_result_df["payout_ratio"] = payout_ratio
            yearly_result_df["dividend_percent"] = (
                yearly_result_df["dividend"] / yearly_result_df[price_column]
            ) * 100
            yearly_result_df["currency"] = "KRW"
            yearly_result_df["updated_at"] = updated_at
            yearly_result_df = yearly_result_df[schema_columns]
            yearly_result_df = coerce_dividend_result_dtypes(yearly_result_df)
            result_dfs.append(yearly_result_df)

    if not result_dfs:
        return pd.DataFrame(columns=schema_columns)

    return (
        pd.concat(result_dfs, ignore_index=True)
        .sort_values(["security_id", "trade_date"])
        .reset_index(drop=True)
    )


def report_date_from_dividend_rcept_no(rcept_no):
    text = str(rcept_no or "").strip()
    if not re.fullmatch(r"\d{8}\d*", text):
        return None
    try:
        return pd.Timestamp(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
    except ValueError:
        return None


def report_date_from_dividend_source_file(source_file):
    text = str(source_file or "").strip()
    if not text or text.lower() == "nan":
        return None
    match = re.search(r"finance_statement_dividend_(\d{4}-\d{2}-\d{2})", text)
    if not match:
        return None
    try:
        return pd.Timestamp(match.group(1))
    except ValueError:
        return None


def _dividend_report_date(row):
    return (
        report_date_from_dividend_rcept_no(row.get("rcept_no"))
        or report_date_from_dividend_source_file(row.get("source_file"))
    )


def _clean_dividend_text(value):
    if value is None or pd.isna(value):
        return ""
    return str(value)


def silver_dividend_asof_events(stock_code, share_type="보통주식"):
    stock_code = normalize_stock_code(stock_code)
    stock_kind_rows = _silver_stock_kind_rows(stock_code, share_type)
    company_rows = _silver_company_summary_rows(stock_code)
    columns = [
        "stock_code",
        "bsns_year",
        "report_date",
        "rcept_no",
        "report_name",
        "annual_dividend_per_share",
        "payout_ratio",
        "total_dividend_amount",
    ]
    event_rows = []

    if not stock_kind_rows.empty:
        for row in stock_kind_rows.to_dict("records"):
            report_date = _dividend_report_date(row)
            if report_date is None:
                continue
            event_rows.append(
                {
                    "stock_code": stock_code,
                    "bsns_year": int(row["bsns_year"]) if not pd.isna(row.get("bsns_year")) else None,
                    "report_date": report_date,
                    "rcept_no": _clean_dividend_text(row.get("rcept_no")),
                    "report_name": _clean_dividend_text(row.get("report_name")),
                    "annual_dividend_per_share": normalize_decimal_amount(
                        row.get("per_share_cash_dividend_krw")
                    ),
                    "payout_ratio": None,
                    "total_dividend_amount": None,
                }
            )

    if not company_rows.empty:
        for row in company_rows.to_dict("records"):
            report_date = _dividend_report_date(row)
            if report_date is None:
                continue
            payout_ratio_pct = normalize_decimal_amount(row.get("dividend_payout_ratio_pct"))
            event_rows.append(
                {
                    "stock_code": stock_code,
                    "bsns_year": int(row["bsns_year"]) if not pd.isna(row.get("bsns_year")) else None,
                    "report_date": report_date,
                    "rcept_no": _clean_dividend_text(row.get("rcept_no")),
                    "report_name": _clean_dividend_text(row.get("report_name")),
                    "annual_dividend_per_share": None,
                    "payout_ratio": None if payout_ratio_pct is None else payout_ratio_pct / 100,
                    "total_dividend_amount": normalize_decimal_amount(
                        row.get("dividend_payment_amount_krw")
                    ),
                }
            )

    if not event_rows:
        return pd.DataFrame(columns=columns)

    event_df = pd.DataFrame(event_rows, columns=columns)
    event_df["_rcept_no_numeric"] = pd.to_numeric(event_df["rcept_no"], errors="coerce")
    event_df = event_df.sort_values(
        ["report_date", "_rcept_no_numeric"],
        na_position="first",
    )
    event_df = (
        event_df.groupby(["report_date", "rcept_no"], as_index=False, dropna=False)
        .agg(
            {
                "stock_code": "last",
                "bsns_year": "last",
                "report_name": "last",
                "annual_dividend_per_share": "last",
                "payout_ratio": "last",
                "total_dividend_amount": "last",
            }
        )
        .sort_values(["report_date", "rcept_no"])
        .reset_index(drop=True)
    )
    for column in ["annual_dividend_per_share", "payout_ratio", "total_dividend_amount"]:
        event_df[column] = pd.to_numeric(event_df[column], errors="coerce")
    return event_df[columns]
