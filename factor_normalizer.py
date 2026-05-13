import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from dividend_normalizer import (
    calculate_payout_ratio_with_fallback,
    calculate_total_dividend_amount,
    calculate_total_dividend_per_share_with_fallback,
)


PROJECT_ROOT = Path(__file__).resolve().parent
FINANCIAL_DIR = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
PRICE_PATH = PROJECT_ROOT / "data-lake" / "silver" / "price" / "normalized_price.csv"
KRX_PRICE_PATH = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "price" / "normalized_price.csv"
SHARES_PATH = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "shares" / "normalized_shares.csv"
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


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def security_id_of(stock_code):
    return f"SEC_KR_{normalize_stock_code(stock_code)}"


def resolve_price_path(path=None):
    if path is not None:
        return Path(path)
    if PRICE_PATH.exists():
        return PRICE_PATH
    return KRX_PRICE_PATH


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


def first_value_frame(df, *columns):
    result = pd.Series(math.nan, index=df.index, dtype="float64")
    for column in columns:
        if column in df.columns:
            result = result.fillna(pd.to_numeric(df[column], errors="coerce"))
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


def read_stock_shares(stock_code, path=SHARES_PATH):
    security_id = security_id_of(stock_code)
    shares_df = read_stock_csv_by_security_id(path, security_id)

    if shares_df.empty:
        return shares_df

    shares_df["trade_date"] = pd.to_datetime(shares_df["trade_date"])
    for column in ["shares", "market_cap"]:
        shares_df[column] = pd.to_numeric(shares_df[column], errors="coerce")

    return shares_df.sort_values("trade_date").reset_index(drop=True)


def parse_period_from_filename(path):
    match = re.search(r"normalized_(\d{6})_(\d{4})[._](\d{2})\.csv$", Path(path).name)
    if not match:
        return None

    return {
        "stock_code": match.group(1),
        "year": int(match.group(2)),
        "month": int(match.group(3)),
    }


def period_end_date(year, month):
    return pd.Timestamp(year=int(year), month=int(month), day=1) + pd.offsets.MonthEnd(0)


def annual_financial_files(stock_code):
    stock_code = normalize_stock_code(stock_code)
    paths = []

    for path in FINANCIAL_DIR.glob(f"normalized_{stock_code}_*.csv"):
        if ".debug" in path.name or ".validation" in path.name:
            continue
        meta = parse_period_from_filename(path)
        if meta and meta["month"] == ANNUAL_MONTH:
            paths.append(path)

    return sorted(paths, key=lambda p: parse_period_from_filename(p)["year"])


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


def read_annual_financials(stock_code):
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
                "financial_period": period_end_date(meta["year"], meta["month"]),
            }
        )
        rows.append(values)

    if not rows:
        return pd.DataFrame()

    financial_df = pd.DataFrame(rows).sort_values("financial_period").reset_index(drop=True)
    return add_annual_financial_factors(financial_df)


def yoy_pct(series):
    return (series / series.shift(1) - 1) * 100


def add_annual_financial_factors(financial_df):
    df = financial_df.copy()

    df["at"] = numeric_column(df, "TOTAL_ASSETS")
    df["seq"] = numeric_column(df, "TOTAL_EQUITY")
    df["ceq"] = first_value_frame(df, "EAOP", "TOTAL_EQUITY")
    df["ppent"] = numeric_column(df, "PPE")
    df["act"] = numeric_column(df, "CURRENT_ASSETS")
    df["lct"] = numeric_column(df, "CURRENT_LIABILITIES")
    df["invt"] = numeric_column(df, "INVENTORIES")
    df["rect"] = first_value_frame(df, "TRADE_RECEIVABLES", "TRADE_AND_OTHER_RECEIVABLES", "OTHER_RECEIVABLES")
    df["ap"] = first_value_frame(df, "TRADE_PAYABLES", "TRADE_AND_OTHER_PAYABLES", "OTHER_PAYABLES")
    df["dltt"] = first_value_frame(df, "LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK")
    df["dlc"] = numeric_column(df, "SHORT_TERM_DEBT")
    df["che"] = numeric_column(df, "CASH_AND_EQUIVALENTS", 0).fillna(0) + numeric_column(df, "SHORT_TERM_FINANCIAL_ASSETS", 0).fillna(0)

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

    df["avg_assets"] = (df["at"] + df["at"].shift(1)) / 2
    df["avg_equity"] = (df["seq"] + df["seq"].shift(1)) / 2
    df["avg_inventory"] = (df["invt"] + df["invt"].shift(1)) / 2
    df["avg_receivables"] = (df["rect"] + df["rect"].shift(1)) / 2
    df["avg_payables"] = (df["ap"] + df["ap"].shift(1)) / 2

    df["gpm"] = df["gross_profit"] / df["sale"]
    df["opm"] = df["oiadp"] / df["sale"]
    df["ebitda_margin"] = df["oibdp"] / df["sale"]
    df["npm"] = df["ni"] / df["sale"]
    df["tax_rate"] = df["tax_expense"] / df["pbt"]
    df.loc[(df["tax_rate"] < 0) | (df["tax_rate"] > 1), "tax_rate"] = math.nan
    df["nopat"] = df["oiadp"] * (1 - df["tax_rate"])

    df["avg_parent_equity"] = (df["ceq"] + df["ceq"].shift(1)) / 2
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
    df["avg_ic_financial"] = (df["invested_capital_financial"] + df["invested_capital_financial"].shift(1)) / 2
    df["avg_ic_operational"] = (df["invested_capital_operational"] + df["invested_capital_operational"].shift(1)) / 2
    df["roic_financial"] = df["nopat"] / df["avg_ic_financial"]
    df["roic_operational"] = df["nopat"] / df["avg_ic_operational"]

    df["asset_turnover"] = df["sale"] / df["avg_assets"]
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
        - (df["working_capital"] - df["working_capital"].shift(1)).fillna(0)
    )
    df["fcfe"] = (
        df["fcf"]
        + df["debt_issue"].fillna(0)
        - df["debt_repay"].fillna(0)
        + df["sstk"].fillna(0)
        - df["prstkc"].fillna(0)
    )

    df["sales_yoy_pct"] = yoy_pct(df["sale"])
    df["op_yoy_pct"] = yoy_pct(df["oiadp"])
    df["sales_change_mil"] = (df["sale"] - df["sale"].shift(1)) / 1_000_000
    df["op_change_mil"] = (df["oiadp"] - df["oiadp"].shift(1)) / 1_000_000
    df["rdsr_pct"] = df["xrd"] / df["sale"] * 100
    df["eps"] = first_value_frame(df, "BASIC_EPS", "DILUTED_EPS")
    df["eps"] = df["eps"].fillna(df["ni_parent"] / numeric_column(df, "shares"))
    df["eps_yoy_pct"] = yoy_pct(df["eps"])
    df["asset_yoy_pct"] = yoy_pct(df["at"])
    df["cfo_yoy_pct"] = yoy_pct(df["oancf"])
    df["fcf_yoy_pct"] = yoy_pct(df["fcf"])
    df["ffo_yoy_pct"] = yoy_pct(df["ffo"])

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
    df["beneish_m_score"] = calculate_beneish_m_score(df)
    df["f_score"] = calculate_piotroski_f_score(df)

    return df


def calculate_beneish_m_score(df):
    dsri = (df["rect"] / df["sale"]) / (df["rect"].shift(1) / df["sale"].shift(1))
    gmi = ((df["sale"].shift(1) - df["cogs"].shift(1)) / df["sale"].shift(1)) / (
        (df["sale"] - df["cogs"]) / df["sale"]
    )
    aqi = (1 - (df["act"] + df["ppent"]) / df["at"]) / (
        1 - (df["act"].shift(1) + df["ppent"].shift(1)) / df["at"].shift(1)
    )
    sgi = df["sale"] / df["sale"].shift(1)
    depi = (df["dp"].shift(1) / (df["ppent"].shift(1) + df["dp"].shift(1))) / (
        df["dp"] / (df["ppent"] + df["dp"])
    )
    sgna = numeric_column(df, "SGNA")
    sgai = (sgna / df["sale"]) / (sgna.shift(1) / df["sale"].shift(1))
    lvgi = ((df["dltt"] + df["dlc"]) / df["at"]) / (
        (df["dltt"].shift(1) + df["dlc"].shift(1)) / df["at"].shift(1)
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


def calculate_piotroski_f_score(df):
    score = pd.Series(0, index=df.index, dtype="int64")
    score += (df["roa"] > 0).fillna(False).astype(int)
    score += (df["oancf"] > 0).fillna(False).astype(int)
    score += (df["roa"] > df["roa"].shift(1)).fillna(False).astype(int)
    score += (df["oancf"] > df["ni"]).fillna(False).astype(int)
    score += (df["debt_to_equity"] < df["debt_to_equity"].shift(1)).fillna(False).astype(int)
    score += (df["current_ratio"] > df["current_ratio"].shift(1)).fillna(False).astype(int)
    score += (df["sstk"].fillna(0) <= 0).astype(int)
    score += (df["gpm"] > df["gpm"].shift(1)).fillna(False).astype(int)
    score += (df["asset_turnover"] > df["asset_turnover"].shift(1)).fillna(False).astype(int)
    return score


def add_dividend_factors(daily_df, stock_code):
    df = daily_df.copy()
    years = sorted(df["trade_date"].dt.year.unique())
    dividend_by_year = {
        year: calculate_total_dividend_per_share_with_fallback(stock_code, year)
        for year in years
    }
    payout_by_year = {
        year: calculate_payout_ratio_with_fallback(stock_code, year)
        for year in years
    }
    total_dividend_amount_by_year = {
        year: calculate_total_dividend_amount(stock_code, year)
        for year in years
    }

    df["dvpsx"] = df["trade_date"].dt.year.map(dividend_by_year)
    df["dvpsp"] = math.nan
    df["sharehold_div_yield"] = df["dvpsx"] / df["close"] * 100
    df["tdpr"] = df["trade_date"].dt.year.map(payout_by_year)
    df["total_dividend_amount"] = df["trade_date"].dt.year.map(total_dividend_amount_by_year)

    return df


def max_drawdown(returns):
    wealth = returns.fillna(0).add(1).cumprod()
    drawdown = wealth / wealth.cummax() - 1
    return drawdown.min()


def add_price_momentum_factors(daily_df):
    df = daily_df.sort_values("trade_date").copy()
    close = df["close"]
    ret = close.pct_change()

    for window in [5, 20, 50, 150, 200]:
        df[f"na_{window}"] = close.rolling(window, min_periods=1).mean()

    df["tr_12_1"] = close.shift(21) / close.shift(252) - 1
    df["tr_6_1"] = close.shift(21) / close.shift(126) - 1
    df["tr_3_1"] = close.shift(21) / close.shift(63) - 1
    df["ret_1m"] = close / close.shift(21) - 1
    df["high52w_gap_pct"] = (close / close.rolling(252, min_periods=20).max() - 1) * 100
    df["vol_12_1_ann"] = ret.shift(21).rolling(231, min_periods=60).std() * math.sqrt(252)
    df["risk_adj_mom"] = df["tr_12_1"] / df["vol_12_1_ann"]
    cumulative = (1 + ret.shift(21)).rolling(231, min_periods=60)
    df["mdd1yr_12_1_pct"] = cumulative.apply(max_drawdown, raw=False) * 100
    df["adturn_pct_12_1"] = (
        (df["volume"] / df["shares"] * 100).shift(21).rolling(231, min_periods=60).mean()
    )

    return df


def add_daily_market_valuation_factors(daily_df):
    df = daily_df.copy()
    df["mcap_mil"] = df["market_cap"] / 1_000_000
    df["trading_value"] = df["close"] * df["volume"]
    df["csho"] = df["shares"]
    df["eps"] = numeric_column(df, "eps").fillna(df["ni_parent"] / df["shares"])
    df["bps"] = (df["ceq"] / df["shares"]).fillna(0)
    df["sps"] = (df["sale"] / df["shares"]).fillna(0)
    df["cps"] = (df["oancf"] / df["shares"]).fillna(0)
    df["fcff"] = numeric_column(df, "fcff").fillna(0)
    df["fcfe"] = numeric_column(df, "fcfe").fillna(0)

    if "altman_z_score" in df.columns:
        df["altman_z_score"] = df["altman_z_score"] + 0.6 * (
            df["market_cap"] / (numeric_column(df, "TOTAL_LIABILITIES"))
        )

    eps_for_ratio = df["eps"].replace(0, math.nan)
    df["epr"] = eps_for_ratio / df["close"]
    df["bpr"] = df["bps"] / df["close"]
    df["tpr"] = df["ppent"] / df["market_cap"]
    df["spr"] = df["sps"] / df["close"]
    df["cpr"] = df["cps"] / df["close"]
    df["fcfpr"] = df["fcfe"] / df["market_cap"]
    df["npr"] = (df["che"] - df["debt"]) / df["market_cap"]
    df["rpr"] = df["xrd"] / df["market_cap"]
    df["enterprise_value"] = df["market_cap"] + df["debt"].fillna(0) - df["che"].fillna(0)
    df["ebitda_to_ev"] = df["oibdp"] / df["enterprise_value"]
    df["ev_to_ebitda"] = df["enterprise_value"] / df["oibdp"]
    df["ev_to_nopat"] = df["enterprise_value"] / df["nopat"]
    df["net_debt_to_ocf"] = df["net_debt"] / df["oancf"]

    df["per"] = df["close"] / eps_for_ratio
    df["pbr"] = df["close"] / df["bps"]
    df["pcr"] = df["close"] / df["cps"]
    df["psr"] = df["close"] / df["sps"]
    df["roce"] = df["oiadp"] / (df["at"] - df["lct"])
    df["total_interest_coverage"] = df["interest_coverage"]
    df["debt_ratio"] = df["debt"] / df["at"]
    df["dividend_yield"] = df["sharehold_div_yield"]
    df["payout_ratio"] = df["tdpr"]
    df["peg"] = df["per"] / df["eps_yoy_pct"]
    df.loc[df["eps_yoy_pct"] <= 0, "peg"] = math.nan
    df["sharehold_net_buyback_yield"] = (
        (df["prstkc"].fillna(0) - df["sstk"].fillna(0)) / df["market_cap"] * 100
    )
    df["sharehold_return"] = df["sharehold_div_yield"].fillna(0) + df["sharehold_net_buyback_yield"].fillna(0)

    return df


def create_stock_factor_dataframe(
    stock_code,
    start_date=None,
    end_date=None,
    price_path=None,
    shares_path=SHARES_PATH,
):
    stock_code = normalize_stock_code(stock_code)
    output_start_date = pd.Timestamp(start_date) if start_date is not None else None
    output_end_date = pd.Timestamp(end_date) if end_date is not None else None
    price_df = read_stock_prices(stock_code, price_path)
    shares_df = read_stock_shares(stock_code, shares_path)
    financial_df = read_annual_financials(stock_code)

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
        daily_df = pd.merge_asof(
            daily_df.sort_values("trade_date"),
            financial_df.sort_values("financial_period"),
            left_on="trade_date",
            right_on="financial_period",
            direction="backward",
            suffixes=("", "_fin"),
        )
    else:
        daily_df["financial_period"] = pd.NaT

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
        "gpm",
        "opm",
        "ebitda_margin",
        "npm",
        "tax_rate",
        "nopat",
        "roe",
        "avg_parent_equity",
        "roa",
        "iroe",
        "roic_financial",
        "roic_operational",
        "asset_turnover",
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
        "sales_change_mil",
        "op_change_mil",
        "rdsr_pct",
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
        from company import kospi_kosdaq_corp_list

        corps_list = kospi_kosdaq_corp_list()
        stock_codes = sorted(corps_list["stock_code"].dropna().map(normalize_stock_code).unique())

    frames = []
    for stock_code in stock_codes:
        factor_df = create_stock_factor_dataframe(stock_code, **kwargs)
        if not factor_df.empty:
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
