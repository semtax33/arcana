import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
base_dir = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
dividend_base_dir = PROJECT_ROOT / "data-lake" / "bronze" / "dart" / "dividend"
silver_dividend_dir = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "dividend"
silver_dividend_by_stock_kind_path = silver_dividend_dir / "dividend_by_stock_kind.csv"
silver_dividend_company_summary_path = silver_dividend_dir / "dividend_company_summary.csv"
price_file_path = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "price" / "normalized_price.csv"
krx_price_file_path = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "price" / "normalized_price.csv"

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


def coerce_dividend_result_dtypes(df):
    for column in ["dividend", "payout_ratio", "dividend_percent"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def calculate_net_income(stock_code, year, month):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    file_name = f"normalized_{stock_code}_{year}.{month:02d}.csv"
    file_path = base_dir / file_name

    if not file_path.exists():
        print(f"[SKIP] 파일 없음: {file_path}")
        return None
    
    statement_df = pd.read_csv(file_path)
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
    base_date = str(data.get("배당기준일") or "").strip()
    if not base_date:
        return [], []

    base = _base_dividend_row(file_path, data)
    base["bsns_year"] = int(base_date[:4]) if base_date[:4].isdigit() else base["bsns_year"]
    base["report_name"] = base["report_name"] or "dividend_decision"
    base["stlm_dt"] = base_date
    base["rcept_no"] = str(data.get("rcept_no") or "")

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


def _aggregate_decision_rows(by_kind_df, company_df):
    if not by_kind_df.empty:
        decision_mask = by_kind_df["stock_knd_infer_method"] == "decision_json"
        decision_df = by_kind_df.loc[decision_mask].copy()
        other_df = by_kind_df.loc[~decision_mask].copy()

        if not decision_df.empty:
            grouped_rows = []
            for key, rows in decision_df.groupby(
                ["stock_code", "corp_code", "corp_name", "bsns_year", "stock_knd"],
                dropna=False,
                sort=False,
            ):
                stock_code, corp_code, corp_name, bsns_year, stock_kind = key
                grouped_rows.append(
                    {
                        "stock_code": stock_code,
                        "corp_code": corp_code,
                        "corp_name": corp_name,
                        "bsns_year": bsns_year,
                        "reprt_code": "decision",
                        "report_name": "decision_annual_sum",
                        "rcept_no": "",
                        "stlm_dt": _latest_text(rows.sort_values("stlm_dt")["stlm_dt"]),
                        "stock_knd": stock_kind,
                        "market_dividend_yield_pct": None,
                        "per_share_cash_dividend_krw": pd.to_numeric(
                            rows["per_share_cash_dividend_krw"],
                            errors="coerce",
                        ).sum(),
                        "stock_knd_infer_method": "decision_json_annual_sum",
                        "source_file": _latest_text(rows.sort_values("source_file")["source_file"]),
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
            grouped_rows = []
            for key, rows in decision_df.groupby(
                ["stock_code", "corp_code", "corp_name", "bsns_year"],
                dropna=False,
                sort=False,
            ):
                stock_code, corp_code, corp_name, bsns_year = key
                total_krw = pd.to_numeric(
                    rows["dividend_payment_amount_krw"],
                    errors="coerce",
                ).sum()
                grouped_rows.append(
                    {
                        "stock_code": stock_code,
                        "corp_code": corp_code,
                        "corp_name": corp_name,
                        "bsns_year": bsns_year,
                        "reprt_code": "decision",
                        "report_name": "decision_annual_sum",
                        "rcept_no": "",
                        "stlm_dt": _latest_text(rows.sort_values("stlm_dt")["stlm_dt"]),
                        "dividend_payment_amount_million_krw": total_krw / 1_000_000,
                        "dividend_payment_amount_krw": total_krw,
                        "dividend_payout_ratio_pct": None,
                        "source_file": _latest_text(rows.sort_values("source_file")["source_file"]),
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
    bronze_root = Path(bronze_root) if bronze_root is not None else dividend_base_dir
    by_kind_rows = []
    company_rows = []
    failed_rows = []

    if not bronze_root.exists():
        return (
            pd.DataFrame(columns=DIVIDEND_BY_STOCK_KIND_COLUMNS),
            pd.DataFrame(columns=DIVIDEND_COMPANY_SUMMARY_COLUMNS),
            pd.DataFrame(
                [{"source_file": str(bronze_root), "reason": "bronze_root_not_found"}],
                columns=DIVIDEND_SUMMARY_FAILED_COLUMNS,
            ),
        )

    for file_path in sorted(bronze_root.rglob("*.json")):
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
    by_kind_df.to_csv(silver_dir / "dividend_by_stock_kind.csv", index=False, encoding="utf-8-sig")
    company_df.to_csv(silver_dir / "dividend_company_summary.csv", index=False, encoding="utf-8-sig")
    failed_df.to_csv(silver_dir / "dividend_company_summary.failed.csv", index=False, encoding="utf-8-sig")
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
    df = _read_silver_csv(silver_dividend_by_stock_kind_path)
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
    df = _read_silver_csv(silver_dividend_company_summary_path)
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
    labels = {str(share_type or "").strip()}
    if labels & {"보통주", "보통주식"}:
        labels.update({"보통주", "보통주식"})
    return {label for label in labels if label}


def _filter_share_type(df, share_type):
    if df.empty or "stock_knd" not in df.columns:
        return df

    stock_kind = df["stock_knd"].fillna("").astype(str).str.strip()
    labels = _share_type_labels(share_type)
    matched = df.loc[stock_kind.isin(labels)]
    if not matched.empty:
        return matched

    if labels & {"보통주", "보통주식"}:
        common = df.loc[
            stock_kind.str.lower().isin(COMMON_STOCK_KIND_LABELS)
            | stock_kind.str.contains("보통", regex=False)
        ]
        if not common.empty:
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
    if not silver_dividend_by_stock_kind_path.exists():
        print(f"[SKIP] 파일 없음: {silver_dividend_by_stock_kind_path}")
        return 0

    row = _silver_stock_kind_row_for_year(stock_code, year, share_type)
    if row is None:
        return 0

    value = normalize_decimal_amount(row.get("per_share_cash_dividend_krw"))
    return 0 if value is None else value


def calculate_silver_total_dividend_amount(stock_code, year):
    if not silver_dividend_company_summary_path.exists():
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
):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    price_df = get_daily_stock_prices(stock_code, path)

    schema_columns = [
        "security_id",
        "trade_date",
        "dividend",
        "payout_ratio",
        "dividend_percent",
        "currency",
        "updated_at",
    ]

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
):
    from company import kospi_kosdaq_corp_list

    schema_columns = [
        "security_id",
        "trade_date",
        "dividend",
        "payout_ratio",
        "dividend_percent",
        "currency",
        "updated_at",
    ]
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(
        normalize_stock_code(stock_code)
        for stock_code in corps_list["stock_code"].dropna().tolist()
    )
    resolved_path = resolve_price_file_path()

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

    for stock_code in stock_codes:
        security_id = f"SEC_KR_{stock_code}"
        stock_price_df = price_df.loc[price_df["security_id"] == security_id].copy()

        if stock_price_df.empty:
            continue

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
