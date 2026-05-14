from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
FINANCIAL_DIR = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"

FLOW_STATEMENT_TYPES = {"IS", "CIS", "CF"}
BALANCE_STATEMENT_TYPES = {"BS"}
DEFAULT_CUMULATIVE_STATEMENT_TYPES = {"CF"}


def normalize_stock_code(stock_code: Any) -> str:
    return str(stock_code).strip().zfill(6)


def security_id_of(stock_code: Any) -> str:
    return f"SEC_KR_{normalize_stock_code(stock_code)}"


def parse_period_from_filename(path: str | Path) -> dict[str, int | str] | None:
    match = re.search(r"normalized_(\d{6})_(\d{4})[._](\d{2})\.csv$", Path(path).name)
    if not match:
        return None

    return {
        "stock_code": match.group(1),
        "year": int(match.group(2)),
        "month": int(match.group(3)),
    }


def period_end_date(year: int, month: int) -> pd.Timestamp:
    return pd.Timestamp(year=int(year), month=int(month), day=1) + pd.offsets.MonthEnd(0)


def financial_files(stock_code: Any, financial_dir: str | Path = FINANCIAL_DIR) -> list[Path]:
    stock_code = normalize_stock_code(stock_code)
    paths: list[Path] = []

    for path in Path(financial_dir).glob(f"normalized_{stock_code}_*.csv"):
        if ".debug" in path.name or ".validation" in path.name:
            continue
        meta = parse_period_from_filename(path)
        if meta and meta["month"] in {3, 6, 9, 12}:
            paths.append(path)

    return sorted(paths, key=lambda p: (parse_period_from_filename(p)["year"], parse_period_from_filename(p)["month"]))


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
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    for path in financial_files(stock_code, financial_dir):
        meta = parse_period_from_filename(path)
        if meta is None:
            continue

        df = pd.read_csv(path)
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
            "stock_code": normalize_stock_code(stock_code),
            "security_id": security_id_of(stock_code),
            "fiscal_year": meta["year"],
            "fiscal_month": meta["month"],
            "financial_period": period_end_date(meta["year"], meta["month"]),
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

    return pd.DataFrame(rows).sort_values("financial_period").reset_index(drop=True)


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
        "_fs_type_by_id",
    }
    value_columns = [
        column
        for column in df.columns
        if column not in metadata_columns and pd.api.types.is_numeric_dtype(df[column])
    ]

    for column in value_columns:
        statement_type = statement_type_by_id.get(column, "")
        source = pd.to_numeric(df[column], errors="coerce")

        if statement_type in BALANCE_STATEMENT_TYPES:
            df[f"{column}_quarter"] = source
            df[f"{column}_ttm"] = source
            continue

        if statement_type in cumulative_statement_types:
            previous_ytd = source.groupby(df["fiscal_year"]).shift(1)
            df[f"{column}_quarter"] = source.where(df["fiscal_month"] == 3, source - previous_ytd)
        else:
            quarter = source.copy()
            annual_mask = df["fiscal_month"] == 12
            prior_quarters = quarter.where(df["fiscal_month"].isin([3, 6, 9])).groupby(df["fiscal_year"]).cumsum()
            df.loc[annual_mask, f"{column}_quarter"] = source.loc[annual_mask] - prior_quarters.shift(1).loc[annual_mask]
            df.loc[~annual_mask, f"{column}_quarter"] = quarter.loc[~annual_mask]

        df[f"{column}_ttm"] = (
            pd.to_numeric(df[f"{column}_quarter"], errors="coerce")
            .rolling(window=4, min_periods=4)
            .sum()
        )

    return df.drop(columns=["_fs_type_by_id"], errors="ignore")


def ttm_financial_frame(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    cumulative_statement_types: set[str] | None = None,
) -> pd.DataFrame:
    periodized = add_quarter_and_ttm_amounts(
        read_period_snapshots(stock_code, financial_dir),
        cumulative_statement_types=cumulative_statement_types,
    )
    if periodized.empty:
        return periodized

    output = periodized[["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]].copy()
    for column in periodized.columns:
        if column.endswith("_ttm"):
            output[column[:-4]] = periodized[column]

    return output


def quarterly_financial_frame(
    stock_code: Any,
    financial_dir: str | Path = FINANCIAL_DIR,
    cumulative_statement_types: set[str] | None = None,
) -> pd.DataFrame:
    periodized = add_quarter_and_ttm_amounts(
        read_period_snapshots(stock_code, financial_dir),
        cumulative_statement_types=cumulative_statement_types,
    )
    if periodized.empty:
        return periodized

    output = periodized[["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]].copy()
    for column in periodized.columns:
        if column.endswith("_quarter"):
            output[column[:-8]] = periodized[column]

    return output
