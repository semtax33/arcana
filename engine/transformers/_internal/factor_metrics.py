import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import (
    DATA_LAKE,
    first_existing_path,
    market_csv_name,
    parse_statement_snapshot_filename,
)
from engine.transformers.dividends import silver_dividend_asof_events
from engine.transformers.filing_periods import (
    REPORT_METADATA_PATH,
    attach_report_metadata,
    quarterly_financial_frame,
    ttm_financial_frame,
)


FINANCIAL_DIR = DATA_LAKE.silver("dart", "normalized")
PRICE_PATH = DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price"))
LEGACY_PRICE_PATHS = (
    DATA_LAKE.silver("krx", "price", "kr_normalized_price.csv"),
    DATA_LAKE.silver("price", "kr_normalized_price.csv"),
)
KRX_PRICE_PATH = LEGACY_PRICE_PATHS[0]
SHARES_PATH = DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares"))
LEGACY_SHARES_PATHS = (DATA_LAKE.silver("krx", "shares", "normalized_shares.csv"),)
ANNUAL_MONTH = 12


BASE_COLUMNS = [
    "security_id",
    "trade_date",
    "stock_code",
    "fiscal_year",
    "financial_period",
    "close",
    "volume",
    "trading_value",
    "shares",
    "market_cap",
    "currency",
    "updated_at",
]
NON_FACTOR_COLUMNS = set(BASE_COLUMNS) | {
    "open",
    "high",
    "low",
    "adj_close",
    "등락률",
    "fiscal_month",
    "security_id_fin",
    "stock_code_fin",
}
PERCENT_RATIO_FACTOR_COLUMNS = {
    "fcf_margin",
    "gpm",
    "net_margin",
    "opm",
    "operating_profit_margin",
    "ebitda_margin",
    "npm",
    "rnd_margin",
    "rnd_to_sales",
    "tax_rate",
    "roe",
    "roic_financial",
    "roic_operational",
}


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def security_id_of(stock_code):
    return f"SEC_KR_{normalize_stock_code(stock_code)}"


def resolve_price_path(path=None):
    if path is not None:
        return Path(path)
    return first_existing_path(PRICE_PATH, *LEGACY_PRICE_PATHS)


def safe_div(numerator, denominator):
    if numerator is None or denominator is None:
        return math.nan
    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return math.nan
    return numerator / denominator


def numeric_column(df, column, default=math.nan):
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype="float64")


def convert_ratio_columns_to_percent(df, columns):
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce") * 100
    return df


def first_value_frame(df, *columns):
    result = pd.Series(math.nan, index=df.index, dtype="float64")
    for column in columns:
        if column in df.columns:
            result = result.fillna(pd.to_numeric(df[column], errors="coerce"))
    return result


def bound_by_reference(series, reference, max_abs_multiple):
    values = pd.to_numeric(series, errors="coerce")
    bounds = pd.to_numeric(reference, errors="coerce").abs() * max_abs_multiple
    valid = values.notna() & (bounds.isna() | (bounds == 0) | (values.abs() <= bounds))
    return values.where(valid)


def first_bounded_value_frame(df, reference, *columns, max_abs_multiple=1.5):
    result = pd.Series(math.nan, index=df.index, dtype="float64")
    for column in columns:
        if column not in df.columns:
            continue
        values = bound_by_reference(df[column], reference, max_abs_multiple)
        result = result.fillna(values)
    return result


def sanitize_temporal_amount_outliers(df, max_neighbor_multiple=100):
    result = df.copy()
    metadata_columns = {
        "stock_code",
        "security_id",
        "fiscal_year",
        "fiscal_month",
        "financial_period",
        "report_date",
    }
    value_columns = [
        column
        for column in result.columns
        if column not in metadata_columns and pd.api.types.is_numeric_dtype(result[column])
    ]

    for column in value_columns:
        values = pd.to_numeric(result[column], errors="coerce")
        prev_abs = values.shift(1).abs()
        next_abs = values.shift(-1).abs()
        neighbor_abs = pd.concat([prev_abs, next_abs], axis=1).max(axis=1)
        outlier_mask = (
            values.notna()
            & prev_abs.notna()
            & next_abs.notna()
            & (neighbor_abs > 0)
            & (values.abs() > neighbor_abs * max_neighbor_multiple)
        )
        if outlier_mask.any():
            result.loc[outlier_mask, column] = math.nan

    return result


def read_stock_csv_by_security_id(path, security_id, chunksize=500_000):
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()

    chunks = []
    for chunk in pd.read_csv(path, chunksize=chunksize):
        chunk = chunk.drop(
            columns=[column for column in chunk.columns if column.startswith("Unnamed")],
            errors="ignore",
        )
        if "security_id" not in chunk.columns:
            continue
        matched = chunk.loc[chunk["security_id"] == security_id].copy()
        if not matched.empty:
            chunks.append(matched)

    if not chunks:
        return pd.DataFrame()

    return pd.concat(chunks, ignore_index=True)


def read_stock_prices(stock_code, path=None):
    security_id = security_id_of(stock_code)
    price_df = read_stock_csv_by_security_id(resolve_price_path(path), security_id)

    if price_df.empty:
        return price_df

    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    for column in ["open", "high", "low", "close", "volume", "adj_close"]:
        if column in price_df.columns:
            price_df[column] = pd.to_numeric(price_df[column], errors="coerce")
    if "currency" not in price_df.columns:
        price_df["currency"] = "KRW"
    price_df["stock_code"] = normalize_stock_code(stock_code)

    return price_df.sort_values("trade_date").reset_index(drop=True)


def read_stock_shares(stock_code, path=None):
    security_id = security_id_of(stock_code)
    shares_df = read_stock_csv_by_security_id(
        first_existing_path(SHARES_PATH, *LEGACY_SHARES_PATHS) if path is None else path,
        security_id,
    )

    if shares_df.empty:
        return shares_df

    shares_df["trade_date"] = pd.to_datetime(shares_df["trade_date"])
    for column in ["shares", "market_cap"]:
        shares_df[column] = pd.to_numeric(shares_df[column], errors="coerce")

    return shares_df.sort_values("trade_date").reset_index(drop=True)


def parse_period_from_filename(path):
    meta = parse_statement_snapshot_filename(path)
    if meta is None:
        return None

    return {
        "stock_code": meta["stock_code"],
        "year": meta["year"],
        "month": meta["month"],
    }


def period_end_date(year, month):
    return pd.Timestamp(year=int(year), month=int(month), day=1) + pd.offsets.MonthEnd(0)


def annual_financial_files(stock_code):
    stock_code = normalize_stock_code(stock_code)
    paths_by_year = {}

    for path in FINANCIAL_DIR.glob(f"*normalized_{stock_code}_*.csv"):
        if ".debug" in path.name or ".validation" in path.name:
            continue
        meta = parse_period_from_filename(path)
        if meta and meta["month"] == ANNUAL_MONTH:
            year = int(meta["year"])
            if year not in paths_by_year or path.name.startswith("kr_"):
                paths_by_year[year] = path

    return sorted(paths_by_year.values(), key=lambda p: parse_period_from_filename(p)["year"])


def pick_largest_abs(series):
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return math.nan
    return numeric.iloc[numeric.abs().argmax()]


def extract_amount_by_name(df, patterns, statement_types=None, absolute=False):
    name = df["original_account_name"].fillna("").astype(str)
    mask = pd.Series(False, index=df.index)

    for pattern in patterns:
        mask = mask | name.str.contains(pattern, regex=True, na=False)

    if statement_types is not None:
        mask = mask & df["statement_type"].isin(statement_types)

    values = pd.to_numeric(df.loc[mask, "normalized_amount"], errors="coerce").dropna()
    if values.empty:
        return math.nan

    value = values.iloc[values.abs().argmax()]
    return abs(value) if absolute else value


def extract_fallback_values(df):
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


def read_annual_financials(stock_code, report_metadata_path=REPORT_METADATA_PATH):
    rows = []

    for path in annual_financial_files(stock_code):
        meta = parse_period_from_filename(path)
        df = pd.read_csv(path)
        if df.empty:
            continue

        df["normalized_amount"] = pd.to_numeric(df.get("normalized_amount"), errors="coerce")
        df = df[df["canonical_account_id"].notna()].copy()
        grouped = (
            df.groupby("canonical_account_id", as_index=False)["normalized_amount"]
            .agg(pick_largest_abs)
        )
        values = dict(zip(grouped["canonical_account_id"], grouped["normalized_amount"]))
        values.update(extract_fallback_values(df))
        values.update(
            {
                "stock_code": normalize_stock_code(stock_code),
                "security_id": security_id_of(stock_code),
                "fiscal_year": meta["year"],
                "fiscal_month": meta["month"],
                "financial_period": period_end_date(meta["year"], meta["month"]),
            }
        )
        rows.append(values)

    if not rows:
        return pd.DataFrame()

    financial_df = pd.DataFrame(rows).sort_values("financial_period").reset_index(drop=True)
    financial_df = attach_report_metadata(financial_df, report_metadata_path)
    return add_annual_financial_factors(financial_df, periods_per_year=1)


def read_ttm_financials(stock_code, cumulative_statement_types=None, report_metadata_path=REPORT_METADATA_PATH):
    financial_df = ttm_financial_frame(
        stock_code,
        FINANCIAL_DIR,
        cumulative_statement_types=cumulative_statement_types,
        report_metadata_path=report_metadata_path,
    )
    if financial_df.empty:
        return financial_df
    return add_annual_financial_factors(financial_df, periods_per_year=4)


def read_quarterly_financials(stock_code, cumulative_statement_types=None, report_metadata_path=REPORT_METADATA_PATH):
    financial_df = quarterly_financial_frame(
        stock_code,
        FINANCIAL_DIR,
        cumulative_statement_types=cumulative_statement_types,
        report_metadata_path=report_metadata_path,
    )
    if financial_df.empty:
        return financial_df
    return add_annual_financial_factors(financial_df, periods_per_year=4)


def yoy_pct(series, periods=1):
    return (series / series.shift(periods) - 1) * 100


def growth_pct(series, periods=1):
    previous = series.shift(periods)
    return (series - previous) / previous.abs() * 100


def cagr_pct(series, years, periods_per_year=1):
    periods = max(int(round(years * periods_per_year)), 1)
    previous = series.shift(periods)
    current = pd.to_numeric(series, errors="coerce")
    ratio = current / previous
    result = (ratio ** (1 / years) - 1) * 100
    return result.where((current > 0) & (previous > 0))


def add_annual_financial_factors(financial_df, periods_per_year=1):
    df = sanitize_temporal_amount_outliers(financial_df)
    lag = max(int(periods_per_year), 1)

    df["at"] = numeric_column(df, "TOTAL_ASSETS")
    df["seq"] = bound_by_reference(numeric_column(df, "TOTAL_EQUITY"), df["at"], 2)
    df["ceq"] = first_bounded_value_frame(df, df["at"], "EAOP", "TOTAL_EQUITY", max_abs_multiple=2)
    df["ppent"] = bound_by_reference(numeric_column(df, "PPE"), df["at"], 1.5)
    df["act"] = bound_by_reference(numeric_column(df, "CURRENT_ASSETS"), df["at"], 1.5)
    df["lct"] = bound_by_reference(numeric_column(df, "CURRENT_LIABILITIES"), df["at"], 2)
    df["invt"] = bound_by_reference(numeric_column(df, "INVENTORIES"), df["at"], 1.5)
    df["rect"] = first_bounded_value_frame(
        df,
        df["at"],
        "TRADE_RECEIVABLES",
        "TRADE_AND_OTHER_RECEIVABLES",
        "OTHER_RECEIVABLES",
        max_abs_multiple=1.5,
    )
    df["ap"] = first_bounded_value_frame(
        df,
        df["at"],
        "TRADE_PAYABLES",
        "TRADE_AND_OTHER_PAYABLES",
        "OTHER_PAYABLES",
        max_abs_multiple=1.5,
    )
    df["dltt"] = first_value_frame(df, "LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK")
    df["dlc"] = numeric_column(df, "SHORT_TERM_DEBT")
    df["che"] = bound_by_reference(
        numeric_column(df, "CASH_AND_EQUIVALENTS", 0).fillna(0)
        + numeric_column(df, "SHORT_TERM_FINANCIAL_ASSETS", 0).fillna(0),
        df["at"],
        1.5,
    )

    df["sale"] = numeric_column(df, "REVENUE")
    df["ni"] = numeric_column(df, "NET_INCOME")
    df["ni_parent"] = first_value_frame(df, "NET_INCOME_PARENT", "NET_INCOME")
    df["oiadp"] = numeric_column(df, "OPERATING_INCOME")
    df["cogs"] = numeric_column(df, "COGS")
    df["xrd"] = numeric_column(df, "RND")
    df["xint"] = first_value_frame(
        df,
        "INTEREST_EXPENSE_FALLBACK",
        "INT_PAID",
        "INTEREST_PAID_FALLBACK",
        "FINANCE_COST_FALLBACK",
    )
    df["tax_expense"] = numeric_column(df, "TAX_EXPENSE")
    df["pbt"] = numeric_column(df, "PBT")
    df["gross_profit"] = first_value_frame(df, "GROSS_PROFIT")
    df["gross_profit"] = df["gross_profit"].fillna(df["sale"] - df["cogs"])

    cf_depreciation = numeric_column(df, "DEPRECIATION_EXPENSE")
    cf_amortization = numeric_column(df, "AMORTIZATION")
    df["dp"] = first_value_frame(df, "DNA_IS")
    df["dp"] = df["dp"].fillna(cf_depreciation.fillna(0) + cf_amortization.fillna(0))
    df["oibdp"] = first_value_frame(df, "EBITDA")
    df["oibdp"] = df["oibdp"].fillna(df["oiadp"] + df["dp"].fillna(0))

    df["oancf"] = numeric_column(df, "CFO")
    df["capx"] = numeric_column(df, "CAPEX_PPE").abs()
    df["fcf"] = df["oancf"] - df["capx"]
    df["ffo"] = df["ni"] + df["dp"].fillna(0)
    df["sstk"] = numeric_column(df, "EQ_ISSUE")
    df["prstkc"] = numeric_column(df, "BUYBACK")
    df["debt_issue"] = numeric_column(df, "DEBT_ISSUE")
    df["debt_repay"] = numeric_column(df, "DEBT_REPAY")

    df["avg_assets"] = (df["at"] + df["at"].shift(lag)) / 2
    df["avg_equity"] = (df["seq"] + df["seq"].shift(lag)) / 2
    df["avg_inventory"] = (df["invt"] + df["invt"].shift(lag)) / 2
    df["avg_receivables"] = (df["rect"] + df["rect"].shift(lag)) / 2
    df["avg_payables"] = (df["ap"] + df["ap"].shift(lag)) / 2

    df["gpm"] = df["gross_profit"] / df["sale"]
    df["opm"] = df["oiadp"] / df["sale"]
    df["operating_profit_margin"] = df["opm"]
    df["ebitda_margin"] = df["oibdp"] / df["sale"]
    df["npm"] = df["ni"] / df["sale"]
    df["net_margin"] = df["npm"]
    df["fcf_margin"] = df["fcf"] / df["sale"]
    df["rnd_margin"] = df["xrd"] / df["sale"]
    df["rnd_to_sales"] = df["xrd"] / df["sale"]
    df["tax_rate"] = df["tax_expense"] / df["pbt"]
    df.loc[(df["tax_rate"] < 0) | (df["tax_rate"] > 1), "tax_rate"] = math.nan
    df["nopat"] = df["oiadp"] * (1 - df["tax_rate"])

    df["avg_parent_equity"] = (df["ceq"] + df["ceq"].shift(lag)) / 2
    df["roe"] = df["ni_parent"] / df["avg_parent_equity"]
    df["roa"] = df["ni"] / df["avg_assets"]
    df["iroe"] = (
        df["ni_parent"] + df["xrd"].fillna(0) * (1 - df["tax_rate"].fillna(0))
    ) / df["avg_parent_equity"]
    df["debt"] = df["dltt"].fillna(0) + df["dlc"].fillna(0)
    df["net_debt"] = df["debt"] - df["che"].fillna(0)
    df["invested_capital_financial"] = df["seq"] + df["debt"] - df["che"].fillna(0)
    df["invested_capital_operational"] = (
        df["rect"].fillna(0)
        + df["invt"].fillna(0)
        - df["ap"].fillna(0)
        + df["ppent"].fillna(0)
        + numeric_column(df, "INTANGIBLE_ASSETS", 0).fillna(0)
    )
    df["avg_ic_financial"] = (df["invested_capital_financial"] + df["invested_capital_financial"].shift(lag)) / 2
    df["avg_ic_operational"] = (df["invested_capital_operational"] + df["invested_capital_operational"].shift(lag)) / 2
    df["roic_financial"] = df["nopat"] / df["avg_ic_financial"]
    df["roic_operational"] = df["nopat"] / df["avg_ic_operational"]

    df["asset_turnover"] = df["sale"] / df["avg_assets"]
    df["total_asset_turnover"] = df["asset_turnover"]
    df["receivables_turnover"] = df["sale"] / df["avg_receivables"]
    df["inventory_turnover"] = df["cogs"] / df["avg_inventory"]
    df["inv_days"] = df["avg_inventory"] / df["cogs"] * 365
    df["ar_days"] = df["avg_receivables"] / df["sale"] * 365
    df["ap_days"] = df["avg_payables"] / df["cogs"] * 365
    df["ccc"] = df["inv_days"] + df["ar_days"] - df["ap_days"]
    df["working_capital"] = df["act"] - df["lct"]
    df["wc_to_sales_pct"] = df["working_capital"] / df["sale"] * 100
    df["working_capital_turnover"] = df["sale"] / df["working_capital"]
    df["fcff"] = (
        df["nopat"]
        + df["dp"].fillna(0)
        - df["capx"].fillna(0)
        - (df["working_capital"] - df["working_capital"].shift(lag)).fillna(0)
    )
    df["fcfe"] = (
        df["fcf"]
        + df["debt_issue"].fillna(0)
        - df["debt_repay"].fillna(0)
        + df["sstk"].fillna(0)
        - df["prstkc"].fillna(0)
    )

    df["sales_yoy_pct"] = yoy_pct(df["sale"], periods=lag)
    df["op_yoy_pct"] = yoy_pct(df["oiadp"], periods=lag)
    df["sales_growth_1y"] = growth_pct(df["sale"], periods=lag)
    df["sales_growth_3y"] = growth_pct(df["sale"], periods=lag * 3)
    df["sales_growth_5y"] = growth_pct(df["sale"], periods=lag * 5)
    df["sales_cagr_3y"] = cagr_pct(df["sale"], years=3, periods_per_year=lag)
    df["net_income_growth_1y"] = growth_pct(df["ni"], periods=lag)
    df["net_income_growth_3y"] = growth_pct(df["ni"], periods=lag * 3)
    df["net_income_growth_5y"] = growth_pct(df["ni"], periods=lag * 5)
    df["operating_income_growth_1y"] = growth_pct(df["oiadp"], periods=lag)
    df["operating_income_growth_3y"] = growth_pct(df["oiadp"], periods=lag * 3)
    df["operating_income_growth_5y"] = growth_pct(df["oiadp"], periods=lag * 5)
    df["sales_change_mil"] = (df["sale"] - df["sale"].shift(lag)) / 1_000_000
    df["op_change_mil"] = (df["oiadp"] - df["oiadp"].shift(lag)) / 1_000_000
    df["rdsr_pct"] = df["xrd"] / df["sale"] * 100
    df["eps"] = first_value_frame(df, "BASIC_EPS", "DILUTED_EPS")
    df["eps"] = df["eps"].fillna(df["ni_parent"] / numeric_column(df, "shares"))
    df["eps_yoy_pct"] = yoy_pct(df["eps"], periods=lag)
    df["asset_yoy_pct"] = yoy_pct(df["at"], periods=lag)
    df["cfo_yoy_pct"] = yoy_pct(df["oancf"], periods=lag)
    df["fcf_yoy_pct"] = yoy_pct(df["fcf"], periods=lag)
    df["ffo_yoy_pct"] = yoy_pct(df["ffo"], periods=lag)

    df["net_debt_to_ebitda"] = df["net_debt"] / df["oibdp"]
    df["fc_to_ndr"] = df["fcf"] / df["net_debt"]
    df["icr_times"] = df["oancf"] / df["xint"].abs()
    df["interest_coverage"] = df["oiadp"] / df["xint"].abs()
    df["current_ratio"] = df["act"] / df["lct"]
    df["debt_to_equity"] = df["debt"] / df["seq"]
    df["cash_to_debt"] = df["che"] / df["debt"]

    df["retained_earnings"] = first_value_frame(
        df,
        "RETAINED_EARNINGS",
        "RETAINED_EARNINGS_FALLBACK",
    )
    df["altman_z_score"] = (
        1.2 * ((df["act"] - df["lct"]) / df["at"])
        + 1.4 * (df["retained_earnings"] / df["at"])
        + 3.3 * (df["oiadp"] / df["at"])
        + 1.0 * (df["sale"] / df["at"])
    )
    df["beneish_m_score"] = calculate_beneish_m_score(df, periods=lag)
    df["f_score"] = calculate_piotroski_f_score(df, periods=lag)

    df = convert_ratio_columns_to_percent(df, PERCENT_RATIO_FACTOR_COLUMNS)
    return df


def calculate_beneish_m_score(df, periods=1):
    dsri = (df["rect"] / df["sale"]) / (df["rect"].shift(periods) / df["sale"].shift(periods))
    gmi = ((df["sale"].shift(periods) - df["cogs"].shift(periods)) / df["sale"].shift(periods)) / (
        (df["sale"] - df["cogs"]) / df["sale"]
    )
    aqi = (1 - (df["act"] + df["ppent"]) / df["at"]) / (
        1 - (df["act"].shift(periods) + df["ppent"].shift(periods)) / df["at"].shift(periods)
    )
    sgi = df["sale"] / df["sale"].shift(periods)
    depi = (df["dp"].shift(periods) / (df["ppent"].shift(periods) + df["dp"].shift(periods))) / (
        df["dp"] / (df["ppent"] + df["dp"])
    )
    sgna = numeric_column(df, "SGNA")
    sgai = (sgna / df["sale"]) / (sgna.shift(periods) / df["sale"].shift(periods))
    lvgi = ((df["dltt"] + df["dlc"]) / df["at"]) / (
        (df["dltt"].shift(periods) + df["dlc"].shift(periods)) / df["at"].shift(periods)
    )
    tata = (df["ni"] - df["oancf"]) / df["at"]

    return (
        -4.84
        + 0.92 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )


def calculate_piotroski_f_score(df, periods=1):
    score = pd.Series(0, index=df.index, dtype="int64")
    score += (df["roa"] > 0).fillna(False).astype(int)
    score += (df["oancf"] > 0).fillna(False).astype(int)
    score += (df["roa"] > df["roa"].shift(periods)).fillna(False).astype(int)
    score += (df["oancf"] > df["ni"]).fillna(False).astype(int)
    score += (df["debt_to_equity"] < df["debt_to_equity"].shift(periods)).fillna(False).astype(int)
    score += (df["current_ratio"] > df["current_ratio"].shift(periods)).fillna(False).astype(int)
    score += (df["sstk"].fillna(0) <= 0).astype(int)
    score += (df["gpm"] > df["gpm"].shift(periods)).fillna(False).astype(int)
    score += (df["asset_turnover"] > df["asset_turnover"].shift(periods)).fillna(False).astype(int)
    return score


def add_dividend_factors(daily_df, stock_code):
    df = daily_df.copy()
    dividend_events = silver_dividend_asof_events(stock_code)
    if not dividend_events.empty:
        events = dividend_events.copy()
        events["report_date"] = pd.to_datetime(events["report_date"], errors="coerce")
        events = events.dropna(subset=["report_date"]).sort_values("report_date")
        if not events.empty:
            df = pd.merge_asof(
                df.sort_values("trade_date"),
                events[
                    [
                        "report_date",
                        "annual_dividend_per_share",
                        "payout_ratio",
                        "total_dividend_amount",
                    ]
                ].sort_values("report_date"),
                left_on="trade_date",
                right_on="report_date",
                direction="backward",
            )
            df["dvpsx"] = pd.to_numeric(df["annual_dividend_per_share"], errors="coerce")
            df["dvpsp"] = math.nan
            df["sharehold_div_yield"] = df["dvpsx"] / df["close"] * 100
            df["tdpr"] = pd.to_numeric(df["payout_ratio"], errors="coerce") * 100
            df.loc[df["tdpr"] < 0, "tdpr"] = math.nan
            df.loc[
                (df["sharehold_div_yield"] < 0) | (df["sharehold_div_yield"] > 100),
                "sharehold_div_yield",
            ] = math.nan
            df["total_dividend_amount"] = pd.to_numeric(
                df["total_dividend_amount"],
                errors="coerce",
            )
            return df.drop(
                columns=[
                    "report_date",
                    "annual_dividend_per_share",
                    "payout_ratio",
                ],
                errors="ignore",
            )

    df["dvpsx"] = math.nan
    df["dvpsp"] = math.nan
    df["sharehold_div_yield"] = math.nan
    df["tdpr"] = math.nan
    df["total_dividend_amount"] = math.nan

    return df


def max_drawdown(returns):
    wealth = returns.fillna(0).add(1).cumprod()
    drawdown = wealth / wealth.cummax() - 1
    return drawdown.min()


def add_price_momentum_factors(daily_df):
    df = daily_df.sort_values("trade_date").copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = numeric_column(df, "high").fillna(close)
    low = numeric_column(df, "low").fillna(close)
    volume = numeric_column(df, "volume")
    ret = close.pct_change()

    for window in [5, 20, 50, 150, 200]:
        df[f"na_{window}"] = close.rolling(window, min_periods=1).mean()
    for window in [50, 120, 150, 200]:
        df[f"ma_{window}"] = close.rolling(window, min_periods=1).mean()

    add_technical_indicator_factors(df, close, high, low, volume)

    df["tr_12_1"] = close.shift(21) / close.shift(252) - 1
    df["tr_6_1"] = close.shift(21) / close.shift(126) - 1
    df["tr_3_1"] = close.shift(21) / close.shift(63) - 1
    df["ret_1m"] = close / close.shift(21) - 1
    df["high52w_gap_pct"] = (close / close.rolling(252, min_periods=20).max() - 1) * 100
    df["vol_12_1_ann"] = ret.shift(21).rolling(231, min_periods=60).std() * math.sqrt(252)
    df["risk_adj_mom"] = df["tr_12_1"] / df["vol_12_1_ann"]
    df["mdd1yr_12_1_pct"] = (
        ret.shift(21).rolling(231, min_periods=60).apply(max_drawdown, raw=False) * 100
    )
    df["adturn_pct_12_1"] = (volume / df["shares"] * 100).shift(21).rolling(231, min_periods=60).mean()

    return df


def add_technical_indicator_factors(df, close, high=None, low=None, volume=None):
    close = pd.to_numeric(close, errors="coerce")
    high = pd.to_numeric(high if high is not None else df.get("high", close), errors="coerce").fillna(close)
    low = pd.to_numeric(low if low is not None else df.get("low", close), errors="coerce").fillna(close)
    if volume is None:
        volume = df["volume"] if "volume" in df.columns else pd.Series(0, index=df.index, dtype="float64")
    volume = pd.to_numeric(volume, errors="coerce")

    df["rsi_14"] = calculate_rsi(close, window=14)

    ema_12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    df["macd"] = ema_12 - ema_26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["bb_middle"] = close.rolling(20, min_periods=20).mean()
    bb_std = close.rolling(20, min_periods=20).std(ddof=0)
    df["bb_upper"] = df["bb_middle"] + 2 * bb_std
    df["bb_lower"] = df["bb_middle"] - 2 * bb_std
    bb_band_range = df["bb_upper"] - df["bb_lower"]
    df["bb_width_pct"] = bb_band_range / df["bb_middle"] * 100
    df["bb_percent_b"] = (close - df["bb_lower"]) / bb_band_range

    df["ati"] = calculate_ati(high, low, close, volume)
    df["williams_r_14"] = calculate_williams_r(high, low, close, window=14)
    df["cmf_20"] = calculate_cmf(high, low, close, volume, window=20)
    df["mfi_14"] = calculate_mfi(high, low, close, volume, window=14)

    return df


def calculate_rsi(close, window=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return rsi


def calculate_money_flow_multiplier(high, low, close):
    price_range = high - low
    multiplier = ((close - low) - (high - close)) / price_range.replace(0, math.nan)
    return multiplier.fillna(0)


def calculate_accumulation_distribution_line(high, low, close, volume):
    money_flow_volume = calculate_money_flow_multiplier(high, low, close) * volume.fillna(0)
    return money_flow_volume.cumsum()


def calculate_ati(high, low, close, volume, fast_span=3, slow_span=10):
    ad_line = calculate_accumulation_distribution_line(high, low, close, volume)
    fast_ema = ad_line.ewm(span=fast_span, adjust=False, min_periods=fast_span).mean()
    slow_ema = ad_line.ewm(span=slow_span, adjust=False, min_periods=slow_span).mean()
    return fast_ema - slow_ema


def calculate_williams_r(high, low, close, window=14):
    highest_high = high.rolling(window, min_periods=window).max()
    lowest_low = low.rolling(window, min_periods=window).min()
    price_range = (highest_high - lowest_low).replace(0, math.nan)
    return -100 * (highest_high - close) / price_range


def calculate_cmf(high, low, close, volume, window=20):
    money_flow_volume = calculate_money_flow_multiplier(high, low, close) * volume.fillna(0)
    volume_sum = volume.rolling(window, min_periods=window).sum().replace(0, math.nan)
    return money_flow_volume.rolling(window, min_periods=window).sum() / volume_sum


def calculate_mfi(high, low, close, volume, window=14):
    typical_price = (high + low + close) / 3
    raw_money_flow = typical_price * volume
    typical_delta = typical_price.diff()
    positive_flow = raw_money_flow.where(typical_delta > 0, 0)
    negative_flow = raw_money_flow.where(typical_delta < 0, 0)
    positive_sum = positive_flow.rolling(window, min_periods=window).sum()
    negative_sum = negative_flow.rolling(window, min_periods=window).sum()

    money_flow_ratio = positive_sum / negative_sum.replace(0, math.nan)
    mfi = 100 - (100 / (1 + money_flow_ratio))
    mfi = mfi.where(negative_sum != 0, 100)
    mfi = mfi.where(~((positive_sum == 0) & (negative_sum == 0)), 50)
    return mfi


def add_daily_market_valuation_factors(daily_df):
    df = daily_df.copy()

    close = numeric_column(df, "close")
    volume = numeric_column(df, "volume")
    shares = numeric_column(df, "shares")
    market_cap = numeric_column(df, "market_cap")
    ni_parent = numeric_column(df, "ni_parent")
    ceq = numeric_column(df, "ceq")
    sale = numeric_column(df, "sale")
    oancf = numeric_column(df, "oancf")
    ppent = numeric_column(df, "ppent")
    che = numeric_column(df, "che")
    debt = numeric_column(df, "debt")
    xrd = numeric_column(df, "xrd")
    oibdp = numeric_column(df, "oibdp")
    nopat = numeric_column(df, "nopat")
    net_debt = numeric_column(df, "net_debt")
    oiadp = numeric_column(df, "oiadp")
    at = numeric_column(df, "at")
    lct = numeric_column(df, "lct")
    interest_coverage = numeric_column(df, "interest_coverage")
    tdpr = numeric_column(df, "tdpr")
    tdpr = tdpr.where(tdpr >= 0)
    eps_yoy_pct = numeric_column(df, "eps_yoy_pct")
    prstkc = numeric_column(df, "prstkc")
    sstk = numeric_column(df, "sstk")
    sharehold_div_yield = numeric_column(df, "sharehold_div_yield")
    sharehold_div_yield = sharehold_div_yield.where(
        (sharehold_div_yield >= 0) & (sharehold_div_yield <= 100)
    )

    df["mcap_mil"] = market_cap / 1_000_000
    df["trading_value"] = close * volume
    df["csho"] = shares
    df["eps"] = numeric_column(df, "eps").fillna(ni_parent / shares)
    df["bps"] = (ceq / shares).fillna(0)
    df["sps"] = (sale / shares).fillna(0)
    df["cps"] = (oancf / shares).fillna(0)
    df["fcff"] = numeric_column(df, "fcff").fillna(0)
    df["fcfe"] = numeric_column(df, "fcfe").fillna(0)

    if "altman_z_score" in df.columns:
        df["altman_z_score"] = df["altman_z_score"] + 0.6 * (
            market_cap / (numeric_column(df, "TOTAL_LIABILITIES"))
        )

    eps_for_ratio = df["eps"].replace(0, math.nan)
    df["epr"] = eps_for_ratio / close
    df["bpr"] = df["bps"] / close
    df["tpr"] = ppent / market_cap
    df["spr"] = df["sps"] / close
    df["cpr"] = df["cps"] / close
    df["fcfpr"] = df["fcfe"] / market_cap
    df["npr"] = (che - debt) / market_cap
    df["rpr"] = xrd / market_cap
    df["rnd_to_market_cap"] = xrd / market_cap * 100
    df["enterprise_value"] = market_cap + debt.fillna(0) - che.fillna(0)
    df["ebitda_to_ev"] = oibdp / df["enterprise_value"]
    df["ev_to_ebitda"] = df["enterprise_value"] / oibdp
    df["ev_to_nopat"] = df["enterprise_value"] / nopat
    df["net_debt_to_ocf"] = net_debt / oancf

    df["per"] = close / eps_for_ratio
    df["pbr"] = close / df["bps"]
    df["pcr"] = close / df["cps"]
    df["psr"] = close / df["sps"]
    df["roce"] = oiadp / (at - lct)
    df["total_interest_coverage"] = interest_coverage
    df["debt_ratio"] = debt / at
    df["dividend_yield"] = sharehold_div_yield
    df["payout_ratio"] = tdpr
    df["peg"] = df["per"] / eps_yoy_pct
    df.loc[eps_yoy_pct <= 0, "peg"] = math.nan
    df["sharehold_net_buyback_yield"] = (
        (prstkc.fillna(0) - sstk.fillna(0)) / market_cap * 100
    )
    df["sharehold_return"] = sharehold_div_yield.fillna(0) + df["sharehold_net_buyback_yield"].fillna(0)

    return df


def create_stock_factor_dataframe(
    stock_code,
    start_date=None,
    end_date=None,
    price_path=None,
    shares_path=None,
    financial_basis="annual",
    cumulative_statement_types=None,
    report_metadata_path=REPORT_METADATA_PATH,
):
    stock_code = normalize_stock_code(stock_code)
    output_start_date = pd.Timestamp(start_date) if start_date is not None else None
    output_end_date = pd.Timestamp(end_date) if end_date is not None else None
    price_df = read_stock_prices(stock_code, price_path)
    shares_df = read_stock_shares(stock_code, shares_path)
    if financial_basis == "quarterly":
        financial_df = read_quarterly_financials(
            stock_code,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
        )
    elif financial_basis == "ttm":
        financial_df = read_ttm_financials(
            stock_code,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
        )
    elif financial_basis == "annual":
        financial_df = read_annual_financials(stock_code, report_metadata_path=report_metadata_path)
    else:
        raise ValueError("financial_basis must be 'annual', 'ttm', or 'quarterly'")

    if price_df.empty:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Seoul")).date())
    price_df = price_df.loc[price_df["trade_date"] <= today].copy()
    if output_end_date is not None:
        price_df = price_df.loc[price_df["trade_date"] <= output_end_date].copy()

    daily_df = price_df.sort_values("trade_date").copy()

    if not shares_df.empty:
        daily_df = pd.merge_asof(
            daily_df.sort_values("trade_date"),
            shares_df[["trade_date", "shares", "market_cap"]].sort_values("trade_date"),
            on="trade_date",
            direction="backward",
        )
    else:
        daily_df["shares"] = math.nan
        daily_df["market_cap"] = math.nan

    if not financial_df.empty:
        financial_df = financial_df.copy()
        financial_df["financial_period"] = pd.to_datetime(financial_df["financial_period"], errors="coerce")
        if "report_date" not in financial_df.columns:
            financial_df["report_date"] = financial_df["financial_period"]
        financial_df["report_date"] = pd.to_datetime(financial_df["report_date"], errors="coerce")
        financial_df["report_date"] = financial_df["report_date"].fillna(financial_df["financial_period"])
        daily_df = pd.merge_asof(
            daily_df.sort_values("trade_date"),
            financial_df.sort_values("report_date"),
            left_on="trade_date",
            right_on="report_date",
            direction="backward",
            suffixes=("", "_fin"),
        )
    else:
        daily_df["financial_period"] = pd.NaT
        daily_df["report_date"] = pd.NaT

    daily_df = daily_df.drop(columns=["security_id_fin", "stock_code_fin"], errors="ignore")
    daily_df["stock_code"] = stock_code
    daily_df = add_dividend_factors(daily_df, stock_code)
    daily_df = add_daily_market_valuation_factors(daily_df)
    daily_df = add_price_momentum_factors(daily_df)
    daily_df["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    daily_df["currency"] = "KRW"

    if output_start_date is not None:
        daily_df = daily_df.loc[daily_df["trade_date"] >= output_start_date].copy()
    if output_end_date is not None:
        daily_df = daily_df.loc[daily_df["trade_date"] <= output_end_date].copy()

    daily_df = daily_df.replace([math.inf, -math.inf], math.nan)
    return order_factor_columns(daily_df)


def preferred_factor_columns():
    return [
        "at",
        "seq",
        "ceq",
        "ppent",
        "act",
        "lct",
        "invt",
        "rect",
        "ap",
        "dltt",
        "dlc",
        "che",
        "retained_earnings",
        "sale",
        "ni",
        "ni_parent",
        "oiadp",
        "oibdp",
        "cogs",
        "dp",
        "xrd",
        "xint",
        "oancf",
        "capx",
        "fcf",
        "fcff",
        "fcfe",
        "ffo",
        "dvpsp",
        "dvpsx",
        "sstk",
        "prstkc",
        "eps",
        "bps",
        "sps",
        "cps",
        "csho",
        "mcap_mil",
        "rnd_to_market_cap",
        "gpm",
        "opm",
        "operating_profit_margin",
        "ebitda_margin",
        "npm",
        "net_margin",
        "fcf_margin",
        "rnd_margin",
        "tax_rate",
        "nopat",
        "roe",
        "avg_parent_equity",
        "roa",
        "iroe",
        "roic_financial",
        "roic_operational",
        "asset_turnover",
        "total_asset_turnover",
        "receivables_turnover",
        "inventory_turnover",
        "inv_days",
        "ar_days",
        "ap_days",
        "ccc",
        "working_capital",
        "wc_to_sales_pct",
        "working_capital_turnover",
        "sales_yoy_pct",
        "op_yoy_pct",
        "sales_growth_1y",
        "sales_growth_3y",
        "sales_growth_5y",
        "sales_cagr_3y",
        "net_income_growth_1y",
        "net_income_growth_3y",
        "net_income_growth_5y",
        "operating_income_growth_1y",
        "operating_income_growth_3y",
        "operating_income_growth_5y",
        "sales_change_mil",
        "op_change_mil",
        "rdsr_pct",
        "rnd_to_sales",
        "eps_yoy_pct",
        "asset_yoy_pct",
        "cfo_yoy_pct",
        "fcf_yoy_pct",
        "ffo_yoy_pct",
        "peg",
        "epr",
        "bpr",
        "tpr",
        "spr",
        "cpr",
        "fcfpr",
        "npr",
        "rpr",
        "ebitda_to_ev",
        "ev_to_ebitda",
        "ev_to_nopat",
        "na_5",
        "na_20",
        "na_50",
        "na_150",
        "na_200",
        "ma_50",
        "ma_120",
        "ma_150",
        "ma_200",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "bb_width_pct",
        "bb_percent_b",
        "ati",
        "williams_r_14",
        "cmf_20",
        "mfi_14",
        "tr_12_1",
        "tr_6_1",
        "tr_3_1",
        "ret_1m",
        "high52w_gap_pct",
        "risk_adj_mom",
        "vol_12_1_ann",
        "mdd1yr_12_1_pct",
        "adturn_pct_12_1",
        "net_debt_to_ebitda",
        "net_debt_to_ocf",
        "fc_to_ndr",
        "icr_times",
        "interest_coverage",
        "current_ratio",
        "debt_to_equity",
        "cash_to_debt",
        "sharehold_div_yield",
        "sharehold_net_buyback_yield",
        "sharehold_return",
        "tdpr",
        "per",
        "pbr",
        "pcr",
        "psr",
        "roce",
        "total_interest_coverage",
        "debt_ratio",
        "dividend_yield",
        "payout_ratio",
        "altman_z_score",
        "beneish_m_score",
        "f_score",
    ]


def order_factor_columns(df):
    preferred = BASE_COLUMNS + preferred_factor_columns()
    existing = [column for column in preferred if column in df.columns]
    remaining = [column for column in df.columns if column not in existing]
    return df[existing + remaining]


def create_all_stock_factor_dataframe(stock_codes=None, **kwargs):
    if stock_codes is None:
        from engine.extractors.market_universe import kospi_kosdaq_corp_list

        corps_list = kospi_kosdaq_corp_list()
        stock_codes = sorted(corps_list["stock_code"].dropna().map(normalize_stock_code).unique())

    frames = []
    keep_empty_columns = set(BASE_COLUMNS)
    for stock_code in stock_codes:
        factor_df = create_stock_factor_dataframe(stock_code, **kwargs)
        if not factor_df.empty:
            factor_df = factor_df.loc[
                :,
                [
                    column
                    for column in factor_df.columns
                    if column in keep_empty_columns or not factor_df[column].isna().all()
                ],
            ]
            frames.append(factor_df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True).sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def factor_columns(df):
    return [
        column
        for column in preferred_factor_columns()
        if column in df.columns
    ]


def calculate_factor_coverage(df, columns=None):
    if df.empty:
        return {
            "row_count": 0,
            "factor_count": 0,
            "total_cells": 0,
            "covered_cells": 0,
            "missing_cells": 0,
            "coverage_ratio": 0.0,
            "coverage_pct": 0.0,
            "factor_coverage": pd.DataFrame(
                columns=[
                    "factor",
                    "row_count",
                    "covered_count",
                    "missing_count",
                    "coverage_ratio",
                    "coverage_pct",
                ]
            ),
        }

    target_columns = columns if columns is not None else factor_columns(df)
    target_columns = [column for column in target_columns if column in df.columns]

    if not target_columns:
        return {
            "row_count": len(df),
            "factor_count": 0,
            "total_cells": 0,
            "covered_cells": 0,
            "missing_cells": 0,
            "coverage_ratio": 0.0,
            "coverage_pct": 0.0,
            "factor_coverage": pd.DataFrame(),
        }

    coverage_rows = []
    row_count = len(df)

    for column in target_columns:
        covered_count = int(df[column].notna().sum())
        missing_count = row_count - covered_count
        coverage_ratio = covered_count / row_count if row_count else 0.0
        coverage_rows.append(
            {
                "factor": column,
                "row_count": row_count,
                "covered_count": covered_count,
                "missing_count": missing_count,
                "coverage_ratio": coverage_ratio,
                "coverage_pct": coverage_ratio * 100,
            }
        )

    factor_coverage_df = pd.DataFrame(coverage_rows).sort_values(
        ["coverage_ratio", "factor"],
        ascending=[True, True],
    ).reset_index(drop=True)

    total_cells = row_count * len(target_columns)
    covered_cells = int(df[target_columns].notna().sum().sum())
    missing_cells = total_cells - covered_cells
    coverage_ratio = covered_cells / total_cells if total_cells else 0.0

    return {
        "row_count": row_count,
        "factor_count": len(target_columns),
        "total_cells": total_cells,
        "covered_cells": covered_cells,
        "missing_cells": missing_cells,
        "coverage_ratio": coverage_ratio,
        "coverage_pct": coverage_ratio * 100,
        "factor_coverage": factor_coverage_df,
    }


def create_stock_factor_coverage(stock_code, **kwargs):
    factor_df = create_stock_factor_dataframe(stock_code, **kwargs)
    return calculate_factor_coverage(factor_df)


def create_all_stock_factor_coverage(stock_codes=None, **kwargs):
    factor_df = create_all_stock_factor_dataframe(stock_codes=stock_codes, **kwargs)
    return calculate_factor_coverage(factor_df)
