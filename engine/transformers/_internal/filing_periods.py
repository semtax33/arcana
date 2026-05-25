from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.identifiers import security_id_of as market_security_id_of
from engine.core.paths import (
    DATA_LAKE,
    first_existing_path,
    market_csv_name,
    parse_statement_snapshot_filename,
)
from engine.markets.registry import market_config
from engine.transformers._internal.statement_files import (
    legacy_statement_snapshot_files,
    read_statement_period_frames,
)

FINANCIAL_DIR = DATA_LAKE.silver("dart", "normalized")
REPORT_METADATA_PATH = DATA_LAKE.silver("dart", market_csv_name("report_metadata"))
LEGACY_REPORT_METADATA_PATH = DATA_LAKE.silver("dart", "report_metadata.csv")

FLOW_STATEMENT_TYPES = {"IS", "CIS", "CF"}
BALANCE_STATEMENT_TYPES = {"BS"}
DEFAULT_CUMULATIVE_STATEMENT_TYPES = set(FLOW_STATEMENT_TYPES)


def normalize_stock_code(stock_code: Any) -> str:
    text = str(stock_code).strip()
    return text.zfill(6) if text.isdigit() else text


def security_id_of(stock_code: Any) -> str:
    return f"SEC_KR_{normalize_stock_code(stock_code)}"


def normalize_symbol_for_market(stock_code: Any, market: str = "kr") -> str:
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return normalize_stock_code(stock_code)
    return market_config(market).normalize_symbol(stock_code)


def security_id_for_market(stock_code: Any, market: str = "kr") -> str:
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return security_id_of(stock_code)
    return market_security_id_of(stock_code, market_config(market))


def parse_period_from_filename(path: str | Path) -> dict[str, int | str] | None:
    meta = parse_statement_snapshot_filename(path)
    if meta is None:
        return None

    return {
        "stock_code": meta["stock_code"],
        "year": meta["year"],
        "month": meta["month"],
    }


def period_end_date(year: int, month: int) -> pd.Timestamp:
    return pd.Timestamp(year=int(year), month=int(month), day=1) + pd.offsets.MonthEnd(0)


def load_report_metadata(path: str | Path = REPORT_METADATA_PATH, source_type: str = "statement") -> pd.DataFrame:
    path = first_existing_path(REPORT_METADATA_PATH, LEGACY_REPORT_METADATA_PATH) if path == REPORT_METADATA_PATH else Path(path)
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path, dtype={"stock_code": str, "rcept_no": str, "source_type": str})
    if df.empty:
        return df

    df["stock_code"] = df["stock_code"].map(normalize_stock_code)
    df["fiscal_year"] = pd.to_numeric(df.get("fiscal_year"), errors="coerce").astype("Int64")
    df["fiscal_month"] = pd.to_numeric(df.get("fiscal_month"), errors="coerce").astype("Int64")
    df["report_date"] = pd.to_datetime(df.get("report_date"), errors="coerce")
    if "period_end_date" in df.columns:
        df["period_end_date"] = pd.to_datetime(df["period_end_date"], errors="coerce")
    if source_type and "source_type" in df.columns:
        df = df.loc[df.get("source_type", "").astype(str).eq(source_type)].copy()

    return df.dropna(subset=["stock_code", "fiscal_year", "fiscal_month", "report_date"])


def attach_report_metadata(
    snapshot_df: pd.DataFrame,
    metadata_path: str | Path = REPORT_METADATA_PATH,
    *,
    source_type: str = "statement",
) -> pd.DataFrame:
    if snapshot_df.empty:
        return snapshot_df

    df = snapshot_df.copy()
    df["stock_code"] = df["stock_code"].map(normalize_stock_code)
    df["financial_period"] = pd.to_datetime(df["financial_period"], errors="coerce")
    if "fiscal_year" not in df.columns:
        df["fiscal_year"] = df["financial_period"].dt.year
    else:
        df["fiscal_year"] = pd.to_numeric(df["fiscal_year"], errors="coerce").astype("Int64")
    if "fiscal_month" not in df.columns:
        df["fiscal_month"] = df["financial_period"].dt.month
    else:
        df["fiscal_month"] = pd.to_numeric(df["fiscal_month"], errors="coerce").astype("Int64")
    fallback_report_date = df["financial_period"]

    metadata_df = load_report_metadata(metadata_path, source_type=source_type)
    if metadata_df.empty:
        df["report_date"] = pd.to_datetime(df.get("report_date", fallback_report_date), errors="coerce")
        df["report_date"] = df["report_date"].fillna(fallback_report_date)
        return df

    keep_columns = [
        column
        for column in ["stock_code", "fiscal_year", "fiscal_month", "report_date", "rcept_no", "report_name", "source_url"]
        if column in metadata_df.columns
    ]
    metadata_df = (
        metadata_df[keep_columns]
        .sort_values(["report_date", "rcept_no"], kind="stable")
        .drop_duplicates(["stock_code", "fiscal_year", "fiscal_month"], keep="last")
    )
    df = df.drop(columns=["report_date", "rcept_no", "report_name", "source_url"], errors="ignore")

    df = df.merge(
        metadata_df,
        on=["stock_code", "fiscal_year", "fiscal_month"],
        how="left",
    )
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").fillna(fallback_report_date)
    return df


def financial_files(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    *,
    market: str = "kr",
) -> list[Path]:
    stock_code = normalize_symbol_for_market(stock_code, market)
    return legacy_statement_snapshot_files(stock_code, financial_dir, market=market)


def pick_largest_abs(series: pd.Series) -> float:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return math.nan
    return float(numeric.iloc[numeric.abs().argmax()])


def extract_amount_by_name(
    df: pd.DataFrame,
    patterns: list[str],
    statement_types: list[str] | None = None,
    absolute: bool = False,
) -> float:
    name = df["original_account_name"].fillna("").astype(str)
    mask = pd.Series(False, index=df.index)

    for pattern in patterns:
        mask = mask | name.str.contains(pattern, regex=True, na=False)

    if statement_types is not None:
        mask = mask & df["statement_type"].isin(statement_types)

    values = pd.to_numeric(df.loc[mask, "normalized_amount"], errors="coerce").dropna()
    if values.empty:
        return math.nan

    value = float(values.iloc[values.abs().argmax()])
    return abs(value) if absolute else value


def extract_fallback_values(df: pd.DataFrame) -> dict[str, float]:
    return {
        "RETAINED_EARNINGS_FALLBACK": extract_amount_by_name(
            df,
            [r"이익\s*잉여금", r"결손금"],
            statement_types=["BS"],
        ),
        "LONG_TERM_DEBT_FALLBACK": extract_amount_by_name(
            df,
            [r"장기\s*차입", r"사채"],
            statement_types=["BS"],
            absolute=True,
        ),
        "INTEREST_EXPENSE_FALLBACK": extract_amount_by_name(
            df,
            [r"이자\s*비용"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
        "INTEREST_PAID_FALLBACK": extract_amount_by_name(
            df,
            [r"이자\s*지급"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
        "FINANCE_COST_FALLBACK": extract_amount_by_name(
            df,
            [r"금융\s*비용"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
    }


def read_period_snapshots(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    report_metadata_path: str | Path = REPORT_METADATA_PATH,
    *,
    market: str = "kr",
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    stock_code = normalize_symbol_for_market(stock_code, market)

    for year, month, df in read_statement_period_frames(
        stock_code,
        financial_dir,
        market=market,
    ):
        if df.empty:
            continue

        df["normalized_amount"] = pd.to_numeric(df.get("normalized_amount"), errors="coerce")
        df = df[df["canonical_account_id"].notna()].copy()
        df = df[df["canonical_account_id"] != "UNMAPPED"].copy()
        if df.empty:
            continue

        grouped = (
            df.groupby(["statement_type", "canonical_account_id"], as_index=False)["normalized_amount"]
            .agg(pick_largest_abs)
        )

        values: dict[str, Any] = {
            "stock_code": stock_code,
            "security_id": security_id_for_market(stock_code, market),
            "fiscal_year": year,
            "fiscal_month": month,
            "financial_period": period_end_date(year, month),
        }

        fs_type_by_id: dict[str, str] = {}
        for record in grouped.itertuples(index=False):
            canonical_id = str(record.canonical_account_id)
            values[canonical_id] = record.normalized_amount
            fs_type_by_id[canonical_id] = str(record.statement_type)

        values.update(extract_fallback_values(df))
        values["_fs_type_by_id"] = fs_type_by_id
        rows.append(values)

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows).sort_values("financial_period").reset_index(drop=True)
    return attach_report_metadata(result, report_metadata_path)


def infer_account_statement_types(snapshot_df: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}

    for value in snapshot_df.get("_fs_type_by_id", pd.Series(dtype=object)).dropna():
        if not isinstance(value, dict):
            continue
        for canonical_id, statement_type in value.items():
            result.setdefault(canonical_id, statement_type)

    fallback_statement_types = {
        "RETAINED_EARNINGS_FALLBACK": "BS",
        "LONG_TERM_DEBT_FALLBACK": "BS",
        "INTEREST_EXPENSE_FALLBACK": "IS",
        "INTEREST_PAID_FALLBACK": "CF",
        "FINANCE_COST_FALLBACK": "IS",
    }
    for column, statement_type in fallback_statement_types.items():
        if column in snapshot_df.columns:
            result.setdefault(column, statement_type)

    return result


def add_quarter_and_ttm_amounts(
    snapshot_df: pd.DataFrame,
    cumulative_statement_types: set[str] | None = None,
) -> pd.DataFrame:
    if snapshot_df.empty:
        return snapshot_df

    cumulative_statement_types = cumulative_statement_types or DEFAULT_CUMULATIVE_STATEMENT_TYPES
    df = snapshot_df.sort_values("financial_period").reset_index(drop=True).copy()
    statement_type_by_id = infer_account_statement_types(df)

    metadata_columns = {
        "stock_code",
        "security_id",
        "fiscal_year",
        "fiscal_month",
        "financial_period",
        "report_date",
        "rcept_no",
        "report_name",
        "source_url",
        "_fs_type_by_id",
    }
    value_columns = [
        column
        for column in df.columns
        if column not in metadata_columns and pd.api.types.is_numeric_dtype(df[column])
    ]

    derived_columns: dict[str, pd.Series] = {}
    for column in value_columns:
        statement_type = statement_type_by_id.get(column, "")
        source = pd.to_numeric(df[column], errors="coerce")

        if statement_type in BALANCE_STATEMENT_TYPES:
            quarter_amount = source
            ttm_amount = source
            derived_columns[f"{column}_quarter"] = quarter_amount
            derived_columns[f"{column}_ttm"] = ttm_amount
            continue

        if statement_type in cumulative_statement_types:
            previous_ytd = source.groupby(df["fiscal_year"]).shift(1)
            quarter_amount = source.where(df["fiscal_month"] == 3, source - previous_ytd)
        else:
            quarter = source.copy()
            annual_mask = df["fiscal_month"] == 12
            prior_quarters = quarter.where(df["fiscal_month"].isin([3, 6, 9])).groupby(df["fiscal_year"]).cumsum()
            quarter_amount = quarter.copy()
            quarter_amount.loc[annual_mask] = source.loc[annual_mask] - prior_quarters.shift(1).loc[annual_mask]

        derived_columns[f"{column}_quarter"] = quarter_amount
        derived_columns[f"{column}_ttm"] = pd.to_numeric(
            quarter_amount,
            errors="coerce",
        ).rolling(window=4, min_periods=4).sum()

    if derived_columns:
        df = pd.concat([df, pd.DataFrame(derived_columns, index=df.index)], axis=1).copy()

    return df.drop(columns=["_fs_type_by_id"], errors="ignore")


def ttm_financial_frame(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    cumulative_statement_types: set[str] | None = None,
    report_metadata_path: str | Path = REPORT_METADATA_PATH,
    market: str = "kr",
) -> pd.DataFrame:
    periodized = add_quarter_and_ttm_amounts(
        read_period_snapshots(stock_code, financial_dir, report_metadata_path, market=market),
        cumulative_statement_types=cumulative_statement_types,
    )
    if periodized.empty:
        return periodized

    base_columns = ["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]
    if "report_date" in periodized.columns:
        base_columns.append("report_date")
    value_columns = {
        column[:-4]: periodized[column]
        for column in periodized.columns
        if column.endswith("_ttm")
    }
    output = periodized[base_columns].copy()
    if value_columns:
        output = pd.concat([output, pd.DataFrame(value_columns, index=periodized.index)], axis=1)

    return output


def quarterly_financial_frame(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    cumulative_statement_types: set[str] | None = None,
    report_metadata_path: str | Path = REPORT_METADATA_PATH,
    market: str = "kr",
) -> pd.DataFrame:
    periodized = add_quarter_and_ttm_amounts(
        read_period_snapshots(stock_code, financial_dir, report_metadata_path, market=market),
        cumulative_statement_types=cumulative_statement_types,
    )
    if periodized.empty:
        return periodized

    base_columns = ["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]
    if "report_date" in periodized.columns:
        base_columns.append("report_date")
    value_columns = {
        column[:-8]: periodized[column]
        for column in periodized.columns
        if column.endswith("_quarter")
    }
    output = periodized[base_columns].copy()
    if value_columns:
        output = pd.concat([output, pd.DataFrame(value_columns, index=periodized.index)], axis=1)

    return output
