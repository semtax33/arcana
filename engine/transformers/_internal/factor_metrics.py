import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from engine.core.paths import (
    DATA_LAKE,
    first_existing_path,
    market_csv_name,
    parse_statement_snapshot_filename,
)
from engine.core.identifiers import security_id_of as market_security_id_of
from engine.markets.registry import market_config
from engine.transformers.dividends import silver_dividend_asof_events
from engine.transformers.filing_periods import (
    REPORT_METADATA_PATH,
    add_quarter_and_ttm_amounts,
    attach_report_metadata,
    quarterly_financial_frame,
    read_period_snapshots,
    ttm_financial_frame,
)
from engine.transformers._internal.statement_files import (
    legacy_statement_snapshot_files,
    read_statement_period_frames,
)
from engine.transformers._internal.wacc_inputs import (
    SILVER_WACC_ASSUMPTIONS_PATH,
    SILVER_WACC_BENCHMARK_WEEKLY_RETURNS_PATH,
    SILVER_COUNTRY_ERP_PATH,
    SILVER_RISK_FREE_RATE_PATH,
    calculate_rolling_beta,
    latest_country_erp,
    market_assumption,
    normalize_weekly_returns_from_prices,
    read_wacc_assumptions,
    risk_free_series_for_market,
)


FINANCIAL_DIR = DATA_LAKE.silver("dart", "normalized")
ESTIMATE_GOLD_ROOT = DATA_LAKE.root / "gold" / "estimates"
HANKYUNG_CONSENSUS_DAILY_PATH = DATA_LAKE.silver("consensus", "hankyung", "kr_hankyung_consensus_daily.csv")
HANKYUNG_TARGET_PRICE_CONSENSUS_PATH = DATA_LAKE.silver(
    "consensus",
    "hankyung",
    "kr_hankyung_target_price_consensus.csv",
)
US_CONSENSUS_FACTORS_PATH = DATA_LAKE.silver("consensus", "us", "us_consensus_factors.csv")
PRICE_PATH = DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price"))
LEGACY_PRICE_PATHS = (
    DATA_LAKE.silver("krx", "price", "kr_normalized_price.csv"),
    DATA_LAKE.silver("price", "kr_normalized_price.csv"),
)
KRX_PRICE_PATH = LEGACY_PRICE_PATHS[0]
SHARES_PATH = DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares"))
LEGACY_SHARES_PATHS = (DATA_LAKE.silver("krx", "shares", "normalized_shares.csv"),)
ANNUAL_MONTH = 12
DEFAULT_NOPAT_TAX_RATE = 0.21
INTANGIBLE_INITIAL_GROWTH_RATE = 0.10
KNOWLEDGE_CAPITAL_DEPRECIATION_RATE = 0.15
ORGANIZATION_CAPITAL_INVESTMENT_SHARE = 0.30
ORGANIZATION_CAPITAL_DEPRECIATION_RATE = 0.20
RND_INTENSIVE_SECTOR_CODES = {"35", "45", "50", "HEALTH_CARE", "INFORMATION_TECHNOLOGY", "COMMUNICATION_SERVICES"}
RND_ZERO_IMPUTE_ALLOWED_SECTOR_CODES = {
    "10",
    "15",
    "20",
    "25",
    "30",
    "40",
    "55",
    "60",
    "ENERGY",
    "MATERIALS",
    "INDUSTRIALS",
    "CONSUMER_DISCRETIONARY",
    "CONSUMER_STAPLES",
    "FINANCIALS",
    "UTILITIES",
    "REAL_ESTATE",
}


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
    "fiscal_month",
    "security_id_fin",
    "stock_code_fin",
    "ev_ebitda_quality_flag",
    "ev_nopat_quality_flag",
    "nopat_quality_flag",
    "operating_income_source",
}
PERCENT_RATIO_FACTOR_COLUMNS = {
    "fcf_margin",
    "fcf_payout_ratio",
    "fcfe_payout_ratio",
    "gpm",
    "net_margin",
    "normalized_operating_margin_5y",
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

CONSENSUS_FACTOR_COLUMNS = [
    "eps_expected_growth",
    "revenue_expected_growth",
    "operating_income_expected_growth",
    "net_income_expected_growth",
    "eps_surprise_pct",
    "revenue_surprise_pct",
    "operating_income_surprise_pct",
    "net_income_surprise_pct",
]

REAL_CONSENSUS_FACTOR_COLUMNS = [
    "real_eps_revision_1m_pct",
    "real_eps_expected_growth",
    "real_revenue_expected_growth",
    "real_operating_income_expected_growth",
    "real_net_income_expected_growth",
    "real_eps_surprise_pct",
    "real_revenue_surprise_pct",
    "real_operating_income_surprise_pct",
    "real_net_income_surprise_pct",
]

REAL_CONSENSUS_INPUT_COLUMNS = [
    "forward_per",
    "forward_roe",
]

US_CONSENSUS_FACTOR_COLUMNS = [
    "us_eps_revision_30d_pct",
    "us_eps_revision_breadth_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_dispersion_pct",
    "us_revenue_dispersion_pct",
    "us_eps_surprise_pct",
]
US_CONSENSUS_INPUT_COLUMNS = [
    "us_eps_consensus",
    "us_revenue_consensus",
    "us_operating_income_consensus",
    "us_target_price",
    "us_target_price_analyst_count",
    "us_target_price_provider",
    "us_target_price_source_regime",
    "us_eps_revision_7d_pct",
    "us_eps_revision_60d_pct",
    "us_eps_revision_90d_pct",
    "us_consensus_analyst_count",
    "us_consensus_source_regime",
    "us_consensus_horizon",
]
US_CONSENSUS_STRING_INPUT_COLUMNS = {
    "us_consensus_source_regime",
    "us_consensus_horizon",
    "us_target_price_provider",
    "us_target_price_source_regime",
}

KR_TARGET_PRICE_INPUT_COLUMNS = [
    "kr_target_price",
    "kr_target_price_analyst_count",
]

EPS_IMPLIED_OPERATING_INCOME_SURPRISE_FACTOR = "eps_implied_operating_income_surprise_pct"
KR_PRICE_TO_TARGET_PRICE_FACTOR = "kr_price_to_target_price"
US_PRICE_TO_TARGET_PRICE_FACTOR = "us_price_to_target_price"

DEFAULT_FORWARD_CONSENSUS_STALE_DAYS = 180
MIN_KR_TARGET_PRICE_ANALYSTS = 3
DEFAULT_RIM_DECAY_FACTOR = 0.8
RIM_HISTORICAL_ROE_YEARS = 3
K_RATIO_3Y_WINDOW = 252 * 3
K_RATIO_3Y_MIN_PERIODS = 252 * 2
EQUITY_DURATION_HORIZON_YEARS = 20
PVGO_NORMALIZATION_YEARS = 5
PVGO_MIN_NORMALIZATION_YEARS = 3
PVGO_GROWTH_LOOKBACK_YEARS = 3
PVGO_CAP_YEARS = 10
PVGO_MAX_GROWTH_RATE = 0.25

CONSENSUS_METRIC_SPECS = {
    "basic_eps": {
        "actual_columns": ("BASIC_EPS", "eps", "DILUTED_EPS"),
        "expected_factor": "eps_expected_growth",
        "surprise_factor": "eps_surprise_pct",
        "priority": 0,
    },
    "diluted_eps": {
        "actual_columns": ("DILUTED_EPS", "eps", "BASIC_EPS"),
        "expected_factor": "eps_expected_growth",
        "surprise_factor": "eps_surprise_pct",
        "priority": 1,
    },
    "revenue": {
        "actual_columns": ("REVENUE", "sale"),
        "expected_factor": "revenue_expected_growth",
        "surprise_factor": "revenue_surprise_pct",
        "priority": 0,
    },
    "operating_income": {
        "actual_columns": ("OPERATING_INCOME", "oiadp"),
        "expected_factor": "operating_income_expected_growth",
        "surprise_factor": "operating_income_surprise_pct",
        "priority": 0,
    },
    "net_income_parent": {
        "actual_columns": ("NET_INCOME_PARENT", "ni_parent", "NET_INCOME", "ni"),
        "expected_factor": "net_income_expected_growth",
        "surprise_factor": "net_income_surprise_pct",
        "priority": 0,
    },
    "net_income": {
        "actual_columns": ("NET_INCOME", "ni", "NET_INCOME_PARENT", "ni_parent"),
        "expected_factor": "net_income_expected_growth",
        "surprise_factor": "net_income_surprise_pct",
        "priority": 1,
    },
}

REAL_CONSENSUS_METRIC_SPECS = {
    "basic_eps": {
        "actual_columns": ("BASIC_EPS", "eps", "DILUTED_EPS"),
        "expected_factor": "real_eps_expected_growth",
        "surprise_factor": "real_eps_surprise_pct",
        "priority": 0,
    },
    "revenue": {
        "actual_columns": ("REVENUE", "sale"),
        "expected_factor": "real_revenue_expected_growth",
        "surprise_factor": "real_revenue_surprise_pct",
        "priority": 0,
    },
    "operating_income": {
        "actual_columns": ("OPERATING_INCOME", "oiadp"),
        "expected_factor": "real_operating_income_expected_growth",
        "surprise_factor": "real_operating_income_surprise_pct",
        "priority": 0,
    },
    "net_income": {
        "actual_columns": ("NET_INCOME", "ni", "NET_INCOME_PARENT", "ni_parent"),
        "expected_factor": "real_net_income_expected_growth",
        "surprise_factor": "real_net_income_surprise_pct",
        "priority": 0,
    },
}


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def security_id_of(stock_code):
    return f"SEC_KR_{normalize_stock_code(stock_code)}"


def normalize_symbol_for_market(symbol, market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return normalize_stock_code(symbol)
    return market_config(market).normalize_symbol(symbol)


def security_id_for_market(symbol, market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return security_id_of(symbol)
    return market_security_id_of(symbol, market_config(market))


def financial_dir_for_market(market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return FINANCIAL_DIR
    if market == "us":
        return DATA_LAKE.silver("sec", "normalized")
    return DATA_LAKE.silver(market, "normalized")


def price_path_for_market(market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return first_existing_path(PRICE_PATH, *LEGACY_PRICE_PATHS)
    return DATA_LAKE.silver(market, "price", market_csv_name("normalized_price", market=market))


def shares_path_for_market(market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "kr":
        return first_existing_path(SHARES_PATH, *LEGACY_SHARES_PATHS)
    return DATA_LAKE.silver(market, "shares", market_csv_name("normalized_shares", market=market))


def resolve_price_path(path=None, market="kr"):
    if path is not None:
        return Path(path)
    return price_path_for_market(market)


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


def first_positive_value_frame(df, *columns):
    result = pd.Series(math.nan, index=df.index, dtype="float64")
    for column in columns:
        if column not in df.columns:
            continue
        values = pd.to_numeric(df[column], errors="coerce").abs()
        result = result.fillna(values.where(values > 0))
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


def sector_code_frame(df):
    for column in ["sector_code", "gics_sector_code"]:
        if column in df.columns:
            return df[column].fillna("").astype(str).str.strip().str.upper()
    return pd.Series("", index=df.index, dtype="object")


def impute_missing_rnd_zero(xrd, df):
    sector_code = sector_code_frame(df)
    has_operating_context = numeric_column(df, "REVENUE").notna() | numeric_column(df, "OPERATING_INCOME").notna()
    eligible_sector = sector_code.isin(RND_ZERO_IMPUTE_ALLOWED_SECTOR_CODES) & ~sector_code.isin(RND_INTENSIVE_SECTOR_CODES)
    imputed = pd.to_numeric(xrd, errors="coerce").isna() & has_operating_context & eligible_sector
    return pd.to_numeric(xrd, errors="coerce").mask(imputed, 0.0), imputed


def tax_rate_for_nopat(actual_tax_rate, operating_income, default_rate=DEFAULT_NOPAT_TAX_RATE):
    actual = pd.to_numeric(actual_tax_rate, errors="coerce")
    operating_income = pd.to_numeric(operating_income, errors="coerce")
    valid_actual = actual.where((actual >= 0) & (actual <= 1))
    historical_median = valid_actual.expanding(min_periods=1).median().shift(1)
    no_history_fallback = pd.Series(default_rate, index=valid_actual.index, dtype="float64")
    no_history_fallback = no_history_fallback.where(operating_income >= 0, 0.0)
    fallback = historical_median.fillna(no_history_fallback)
    return valid_actual.fillna(fallback).where(operating_income.notna())


def perpetual_intangible_capital(
    investment,
    *,
    annual_depreciation_rate,
    periods_per_year=1,
    annualized_flows=False,
    initial_growth_rate=INTANGIBLE_INITIAL_GROWTH_RATE,
):
    """Return point-in-time intangible capital and matching-period amortization.

    The stock follows the perpetual-inventory method. The first observable
    stock is initialized as investment / (growth + depreciation), following
    the standard empirical intangible-capital convention. TTM observations
    arrive quarterly but contain annualized flows, so only one quarter of the
    reported investment enters each quarterly stock update while amortization
    is returned on a TTM-comparable annual basis.

    Missing investment resets the state instead of silently assuming zero.
    This matters for R&D-intensive issuers where an absent disclosure is not
    evidence of no R&D.
    """

    periods = max(int(periods_per_year), 1)
    depreciation = float(annual_depreciation_rate)
    growth = float(initial_growth_rate)
    if not 0 < depreciation < 1:
        raise ValueError("annual_depreciation_rate must satisfy 0 < value < 1")
    if growth < 0:
        raise ValueError("initial_growth_rate must be non-negative")

    values = pd.to_numeric(investment, errors="coerce")
    values = values.where(values >= 0)
    period_depreciation = 1 - (1 - depreciation) ** (1 / periods)
    capital = pd.Series(math.nan, index=values.index, dtype="float64")
    amortization = pd.Series(math.nan, index=values.index, dtype="float64")
    previous_capital = math.nan

    for position, raw_investment in enumerate(values.to_numpy()):
        if pd.isna(raw_investment):
            previous_capital = math.nan
            continue

        annual_investment = (
            float(raw_investment)
            if annualized_flows or periods == 1
            else float(raw_investment) * periods
        )
        period_investment = (
            float(raw_investment) / periods
            if annualized_flows
            else float(raw_investment)
        )
        if not math.isfinite(previous_capital):
            previous_capital = annual_investment / (growth + depreciation)

        current_amortization = previous_capital * (
            depreciation if annualized_flows else period_depreciation
        )
        current_capital = (
            previous_capital * (1 - period_depreciation) + period_investment
        )
        capital.iloc[position] = current_capital
        amortization.iloc[position] = current_amortization
        previous_capital = current_capital

    return capital, amortization


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


def positive_denominator(series):
    values = pd.to_numeric(series, errors="coerce")
    return values.where(values > 0)


def nonzero_denominator(series):
    values = pd.to_numeric(series, errors="coerce")
    return values.where(values != 0)


def finite_numeric_series(series):
    values = pd.to_numeric(series, errors="coerce")
    finite = pd.Series([math.isfinite(value) for value in values], index=values.index)
    return values.where(values.notna() & finite)


def safe_series_div(numerator, denominator):
    return pd.to_numeric(numerator, errors="coerce") / positive_denominator(denominator)


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


def _drop_unnamed_columns(df):
    return df.drop(
        columns=[column for column in df.columns if str(column).startswith("Unnamed")],
        errors="ignore",
    )


class FactorMarketDataCache:
    def __init__(
        self,
        market="kr",
        price_path=None,
        shares_path=None,
        dividend_path=None,
        start_date=None,
        end_date=None,
        start_warmup_days=366 * 11,
        wacc_risk_free_path=None,
        wacc_erp_path=None,
        wacc_assumptions_path=None,
        wacc_benchmark_path=None,
    ):
        self.market = str(market or "kr").strip().lower()
        self.price_path = resolve_price_path(price_path, market=self.market)
        self.shares_path = shares_path_for_market(self.market) if shares_path is None else Path(shares_path)
        self.dividend_path = dividend_path_for_market(self.market) if dividend_path is None else Path(dividend_path)
        self.wacc_risk_free_path = Path(wacc_risk_free_path) if wacc_risk_free_path is not None else SILVER_RISK_FREE_RATE_PATH
        self.wacc_erp_path = Path(wacc_erp_path) if wacc_erp_path is not None else SILVER_COUNTRY_ERP_PATH
        self.wacc_assumptions_path = Path(wacc_assumptions_path) if wacc_assumptions_path is not None else SILVER_WACC_ASSUMPTIONS_PATH
        self.wacc_benchmark_path = Path(wacc_benchmark_path) if wacc_benchmark_path is not None else SILVER_WACC_BENCHMARK_WEEKLY_RETURNS_PATH
        self.start_date = pd.Timestamp(start_date) if start_date is not None else None
        self.end_date = pd.Timestamp(end_date) if end_date is not None else None
        self.start_warmup_days = int(start_warmup_days)
        self._price_groups = None
        self._shares_groups = None
        self._dividend_groups = None
        self._risk_free_rates = None
        self._country_erps = None
        self._wacc_assumptions = None
        self._benchmark_weekly_returns = None

    def prices(self, security_id, stock_code=None):
        price_df = self._stock_frame("price", security_id)
        if price_df.empty:
            return price_df

        price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
        for column in ["open", "high", "low", "close", "volume", "adj_close"]:
            if column in price_df.columns:
                price_df[column] = pd.to_numeric(price_df[column], errors="coerce")
        if "currency" not in price_df.columns:
            price_df["currency"] = market_config(self.market).currency
        if stock_code is not None:
            price_df["stock_code"] = stock_code

        return price_df.sort_values("trade_date").reset_index(drop=True)

    def shares(self, security_id):
        shares_df = self._stock_frame("shares", security_id)
        if shares_df.empty:
            return shares_df

        shares_df["trade_date"] = pd.to_datetime(shares_df["trade_date"])
        for column in ["shares", "market_cap"]:
            if column in shares_df.columns:
                shares_df[column] = pd.to_numeric(shares_df[column], errors="coerce")

        return shares_df.sort_values("trade_date").reset_index(drop=True)

    def dividends(self, security_id):
        dividend_df = self._stock_frame("dividend", security_id)
        if dividend_df.empty:
            return dividend_df

        dividend_df["trade_date"] = pd.to_datetime(dividend_df["trade_date"], errors="coerce")
        for column in ["dividend", "payout_ratio", "dividend_percent"]:
            if column in dividend_df.columns:
                dividend_df[column] = pd.to_numeric(dividend_df[column], errors="coerce")

        return (
            dividend_df.dropna(subset=["trade_date"])
            .sort_values("trade_date")
            .reset_index(drop=True)
        )

    def risk_free_rates(self):
        if self._risk_free_rates is None:
            self._risk_free_rates = self._read_wacc_csv(self.wacc_risk_free_path)
            if "date" in self._risk_free_rates.columns:
                self._risk_free_rates["date"] = pd.to_datetime(self._risk_free_rates["date"], errors="coerce")
        return self._risk_free_rates.copy()

    def country_erps(self):
        if self._country_erps is None:
            self._country_erps = self._read_wacc_csv(self.wacc_erp_path)
        return self._country_erps.copy()

    def wacc_assumptions(self):
        if self._wacc_assumptions is None:
            self._wacc_assumptions = read_wacc_assumptions(self.wacc_assumptions_path)
        return self._wacc_assumptions.copy()

    def benchmark_weekly_returns(self):
        if self._benchmark_weekly_returns is None:
            self._benchmark_weekly_returns = self._read_wacc_csv(self.wacc_benchmark_path)
            if "week_end_date" in self._benchmark_weekly_returns.columns:
                self._benchmark_weekly_returns["week_end_date"] = pd.to_datetime(
                    self._benchmark_weekly_returns["week_end_date"],
                    errors="coerce",
                )
        return self._benchmark_weekly_returns.copy()

    def _read_wacc_csv(self, path):
        path = Path(path)
        if not path.exists():
            return pd.DataFrame()
        return _drop_unnamed_columns(pd.read_csv(path))

    def _stock_frame(self, dataset, security_id):
        frame, groups = self._groups(dataset)
        if not groups:
            return pd.DataFrame()
        row_index = groups.get(security_id)
        if row_index is None:
            return pd.DataFrame()
        return frame.loc[row_index].copy()

    def _groups(self, dataset):
        if dataset == "price":
            if self._price_groups is None:
                self._price_groups = self._read_groups(self.price_path, filter_dates=True)
            return self._price_groups
        if dataset == "shares":
            if self._shares_groups is None:
                self._shares_groups = self._read_groups(
                    self.shares_path,
                    filter_dates=True,
                    use_start_warmup=False,
                )
            return self._shares_groups
        if self._dividend_groups is None:
            self._dividend_groups = self._read_groups(self.dividend_path, filter_dates=True)
        return self._dividend_groups

    def _read_groups(self, path, filter_dates=False, use_start_warmup=True):
        path = Path(path)
        if not path.exists():
            return pd.DataFrame(), {}

        df = _drop_unnamed_columns(pd.read_csv(path))
        if "security_id" not in df.columns:
            return pd.DataFrame(), {}

        if filter_dates and "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            if use_start_warmup and self.start_date is not None:
                warmup_start = self.start_date - pd.Timedelta(days=self.start_warmup_days)
                df = df.loc[df["trade_date"] >= warmup_start].copy()
            if self.end_date is not None:
                df = df.loc[df["trade_date"] <= self.end_date].copy()

        groups = df.groupby("security_id", sort=False).groups
        return df, groups


def read_stock_prices(stock_code, path=None, market="kr"):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    security_id = security_id_for_market(stock_code, market)
    price_df = read_stock_csv_by_security_id(resolve_price_path(path, market=market), security_id)

    if price_df.empty:
        return price_df

    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    for column in ["open", "high", "low", "close", "volume", "adj_close"]:
        if column in price_df.columns:
            price_df[column] = pd.to_numeric(price_df[column], errors="coerce")
    if "currency" not in price_df.columns:
        price_df["currency"] = market_config(market).currency
    price_df["stock_code"] = stock_code

    return price_df.sort_values("trade_date").reset_index(drop=True)


def read_stock_shares(stock_code, path=None, market="kr"):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    security_id = security_id_for_market(stock_code, market)
    shares_df = read_stock_csv_by_security_id(
        shares_path_for_market(market) if path is None else path,
        security_id,
    )

    if shares_df.empty:
        return shares_df

    shares_df["trade_date"] = pd.to_datetime(shares_df["trade_date"])
    for column in ["shares", "market_cap"]:
        shares_df[column] = pd.to_numeric(shares_df[column], errors="coerce")

    return shares_df.sort_values("trade_date").reset_index(drop=True)


def dividend_path_for_market(market="kr"):
    market = str(market or "kr").strip().lower()
    if market == "us":
        return DATA_LAKE.silver(
            "us",
            "dividend",
            market_csv_name("dividend_normalized", market="us"),
        )
    return DATA_LAKE.silver("dart", "dividend", "dividend_normalized.csv")


def read_stock_dividends(stock_code, path=None, market="kr"):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    security_id = security_id_for_market(stock_code, market)
    dividend_df = read_stock_csv_by_security_id(
        dividend_path_for_market(market) if path is None else path,
        security_id,
    )

    if dividend_df.empty:
        return dividend_df

    dividend_df["trade_date"] = pd.to_datetime(dividend_df["trade_date"], errors="coerce")
    for column in ["dividend", "payout_ratio", "dividend_percent"]:
        if column in dividend_df.columns:
            dividend_df[column] = pd.to_numeric(dividend_df[column], errors="coerce")

    return (
        dividend_df.dropna(subset=["trade_date"])
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


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


def annual_financial_files(stock_code, financial_dir=None, market="kr"):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    financial_dir = Path(financial_dir) if financial_dir is not None else financial_dir_for_market(market)
    return legacy_statement_snapshot_files(
        stock_code,
        financial_dir,
        market=market,
        months={ANNUAL_MONTH},
    )


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
            [r"retained\s+earnings", r"accumulated\s+earnings", r"earned\s+surplus"],
            statement_types=["BS"],
        ),
        "LONG_TERM_DEBT_FALLBACK": extract_amount_by_name(
            df,
            [r"long[-\s]?term\s+debt", r"non[-\s]?current\s+borrowings", r"bonds\s+payable"],
            statement_types=["BS"],
            absolute=True,
        ),
        "INTEREST_EXPENSE_FALLBACK": extract_amount_by_name(
            df,
            [r"interest\s+expense"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
        "INTEREST_PAID_FALLBACK": extract_amount_by_name(
            df,
            [r"interest\s+paid"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
        "FINANCE_COST_FALLBACK": extract_amount_by_name(
            df,
            [r"finance\s+costs?", r"financial\s+costs?"],
            statement_types=["CF", "IS", "CIS"],
            absolute=True,
        ),
    }

def _fallback_year_range(financial_df):
    if financial_df is not None and not financial_df.empty and "fiscal_year" in financial_df.columns:
        years = pd.to_numeric(financial_df["fiscal_year"], errors="coerce").dropna()
        if not years.empty:
            current_year = datetime.now(ZoneInfo("Asia/Seoul")).year
            return int(years.min()), max(int(years.max()), current_year)

    current_year = datetime.now(ZoneInfo("Asia/Seoul")).year
    return current_year - 12, current_year


def _is_missing_cell(value):
    try:
        return bool(pd.isna(value))
    except Exception:
        return value is None


def _candidate_report_date(candidate):
    filed = pd.to_datetime(getattr(candidate, "filed", None), errors="coerce")
    if pd.notna(filed):
        return filed
    period_end = pd.to_datetime(getattr(candidate, "period_end", None), errors="coerce")
    if pd.notna(period_end):
        return period_end
    return period_end_date(candidate.fiscal_year, candidate.fiscal_month)


def _candidate_financial_period(candidate):
    period_end = pd.to_datetime(getattr(candidate, "period_end", None), errors="coerce")
    if pd.notna(period_end):
        return period_end
    return period_end_date(candidate.fiscal_year, candidate.fiscal_month)


def _edgartools_candidates_for_factor_fallback(
    stock_code,
    *,
    financial_df=None,
    edgartools_provider=None,
):
    start_year, end_year = _fallback_year_range(financial_df)
    try:
        from engine.transformers._internal.sec_filings import (
            canonical_name_map,
            extract_edgartools_candidates,
            load_sec_ticker_map,
            load_us_mapping_rules,
            normalize_cik,
        )
    except Exception as exc:
        print(f"[WARN] edgartools factor fallback skipped for {stock_code}: import failed ({type(exc).__name__})")
        return []

    try:
        rules = load_us_mapping_rules().get("edgartools_fallback_rules", [])
    except Exception as exc:
        print(f"[WARN] edgartools factor fallback skipped for {stock_code}: rule load failed ({type(exc).__name__})")
        return []
    if not rules:
        return []

    symbol = normalize_symbol_for_market(stock_code, "us")
    cik_by_symbol = {symbol: ""}
    name_by_symbol = {symbol: ""}
    ticker_map = load_sec_ticker_map()
    if not ticker_map.empty:
        matched = ticker_map.loc[ticker_map["ticker"].astype(str).str.upper().eq(symbol)]
        if not matched.empty:
            cik_by_symbol[symbol] = normalize_cik(matched["cik"].iat[0])
            name_by_symbol[symbol] = str(matched["title"].iat[0])

    try:
        canonical_names = canonical_name_map()
    except Exception:
        canonical_names = {}

    try:
        return extract_edgartools_candidates(
            symbols=[symbol],
            cik_by_symbol=cik_by_symbol,
            name_by_symbol=name_by_symbol,
            rules=rules,
            canonical_names=canonical_names,
            start_year=start_year,
            end_year=end_year,
            provider=edgartools_provider,
            log_progress=False,
        )
    except Exception as exc:
        print(f"[WARN] edgartools factor fallback skipped for {symbol}: {type(exc).__name__}: {exc}")
        return []


def fill_missing_financial_values_with_edgartools(
    financial_df,
    stock_code,
    *,
    market="kr",
    months=None,
    use_edgartools=True,
    edgartools_provider=None,
):
    market = str(market or "kr").strip().lower()
    if market != "us" or not use_edgartools:
        return financial_df

    candidates = _edgartools_candidates_for_factor_fallback(
        stock_code,
        financial_df=financial_df,
        edgartools_provider=edgartools_provider,
    )
    if not candidates:
        return financial_df

    allowed_months = {int(month) for month in months} if months is not None else None
    result = financial_df.copy() if financial_df is not None else pd.DataFrame()
    base_columns = ["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]
    for column in base_columns:
        if column not in result.columns:
            result[column] = pd.NA
    if "_fs_type_by_id" in result.columns:
        result["_fs_type_by_id"] = result["_fs_type_by_id"].astype("object")

    row_by_period = {}
    for index, row in result.iterrows():
        year = pd.to_numeric(pd.Series([row.get("fiscal_year")]), errors="coerce").iat[0]
        month = pd.to_numeric(pd.Series([row.get("fiscal_month")]), errors="coerce").iat[0]
        if pd.notna(year) and pd.notna(month):
            row_by_period[(int(year), int(month))] = index

    stock_code = normalize_symbol_for_market(stock_code, "us")
    security_id = security_id_for_market(stock_code, "us")
    for candidate in candidates:
        if allowed_months is not None and int(candidate.fiscal_month) not in allowed_months:
            continue
        key = (int(candidate.fiscal_year), int(candidate.fiscal_month))
        row_index = row_by_period.get(key)
        if row_index is None:
            row_index = len(result)
            row_by_period[key] = row_index
            result.loc[row_index, "stock_code"] = stock_code
            result.loc[row_index, "security_id"] = security_id
            result.loc[row_index, "fiscal_year"] = key[0]
            result.loc[row_index, "fiscal_month"] = key[1]
            result.loc[row_index, "financial_period"] = _candidate_financial_period(candidate)

        canonical_id = str(candidate.canonical_id)
        if canonical_id not in result.columns:
            result[canonical_id] = math.nan
        if _is_missing_cell(result.at[row_index, canonical_id]):
            result.at[row_index, canonical_id] = candidate.value

        if "report_date" not in result.columns:
            result["report_date"] = pd.NaT
        if _is_missing_cell(result.at[row_index, "report_date"]):
            result.at[row_index, "report_date"] = _candidate_report_date(candidate)
        if "rcept_no" not in result.columns:
            result["rcept_no"] = pd.NA
        if _is_missing_cell(result.at[row_index, "rcept_no"]):
            result.at[row_index, "rcept_no"] = candidate.accn
        if "source_url" not in result.columns:
            result["source_url"] = pd.NA
        if _is_missing_cell(result.at[row_index, "source_url"]) and candidate.cik and candidate.accn:
            result.at[row_index, "source_url"] = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{candidate.cik}/{candidate.accn.replace('-', '')}/"
            )

        if "_fs_type_by_id" in result.columns:
            fs_type_by_id = result.at[row_index, "_fs_type_by_id"]
            if not isinstance(fs_type_by_id, dict):
                fs_type_by_id = {}
            fs_type_by_id.setdefault(canonical_id, candidate.statement_type)
            result.at[row_index, "_fs_type_by_id"] = fs_type_by_id

    result["financial_period"] = pd.to_datetime(result["financial_period"], errors="coerce")
    return result.sort_values("financial_period").reset_index(drop=True)


def read_annual_financials(
    stock_code,
    report_metadata_path=REPORT_METADATA_PATH,
    *,
    financial_dir=None,
    market="kr",
    use_edgartools=True,
    edgartools_provider=None,
):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    rows = []

    financial_dir = Path(financial_dir) if financial_dir is not None else financial_dir_for_market(market)
    for year, month, df in read_statement_period_frames(
        stock_code,
        financial_dir,
        market=market,
        months={ANNUAL_MONTH},
    ):
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
                "stock_code": stock_code,
                "security_id": security_id_for_market(stock_code, market),
                "fiscal_year": year,
                "fiscal_month": month,
                "financial_period": period_end_date(year, month),
            }
        )
        rows.append(values)

    if rows:
        financial_df = pd.DataFrame(rows).sort_values("financial_period").reset_index(drop=True)
        financial_df = attach_report_metadata(financial_df, report_metadata_path)
    else:
        financial_df = pd.DataFrame()
    financial_df = fill_missing_financial_values_with_edgartools(
        financial_df,
        stock_code,
        market=market,
        months={ANNUAL_MONTH},
        use_edgartools=use_edgartools,
        edgartools_provider=edgartools_provider,
    )
    if financial_df.empty:
        return financial_df
    return add_annual_financial_factors(financial_df, periods_per_year=1)


def _periodized_financial_frame_with_edgartools(
    stock_code,
    *,
    financial_dir,
    cumulative_statement_types=None,
    report_metadata_path=REPORT_METADATA_PATH,
    market="us",
    use_edgartools=True,
    edgartools_provider=None,
):
    snapshot_df = read_period_snapshots(
        normalize_symbol_for_market(stock_code, market),
        financial_dir,
        report_metadata_path,
        market=market,
    )
    if snapshot_df.empty:
        snapshot_df = pd.DataFrame(columns=["_fs_type_by_id"])
    elif "_fs_type_by_id" not in snapshot_df.columns:
        snapshot_df["_fs_type_by_id"] = pd.NA

    snapshot_df = fill_missing_financial_values_with_edgartools(
        snapshot_df,
        stock_code,
        market=market,
        use_edgartools=use_edgartools,
        edgartools_provider=edgartools_provider,
    )
    if snapshot_df.empty:
        return snapshot_df
    return add_quarter_and_ttm_amounts(
        snapshot_df,
        cumulative_statement_types=cumulative_statement_types,
    )


def _financial_frame_from_periodized(periodized, *, suffix):
    if periodized.empty:
        return periodized

    base_columns = ["stock_code", "security_id", "fiscal_year", "fiscal_month", "financial_period"]
    if "report_date" in periodized.columns:
        base_columns.append("report_date")
    value_columns = {
        column[: -len(suffix)]: periodized[column]
        for column in periodized.columns
        if column.endswith(suffix)
    }
    output = periodized[base_columns].copy()
    if value_columns:
        output = pd.concat([output, pd.DataFrame(value_columns, index=periodized.index)], axis=1)
    return output


def read_ttm_financials(
    stock_code,
    cumulative_statement_types=None,
    report_metadata_path=REPORT_METADATA_PATH,
    *,
    financial_dir=None,
    market="kr",
    use_edgartools=True,
    edgartools_provider=None,
):
    financial_dir = financial_dir if financial_dir is not None else financial_dir_for_market(market)
    if str(market or "kr").strip().lower() == "us" and use_edgartools:
        periodized = _periodized_financial_frame_with_edgartools(
            stock_code,
            financial_dir=financial_dir,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            market=market,
            use_edgartools=use_edgartools,
            edgartools_provider=edgartools_provider,
        )
        financial_df = _financial_frame_from_periodized(periodized, suffix="_ttm")
    else:
        financial_df = ttm_financial_frame(
            normalize_symbol_for_market(stock_code, market),
            financial_dir,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            market=market,
        )
    if financial_df.empty:
        return financial_df
    return add_annual_financial_factors(
        financial_df,
        periods_per_year=4,
        annualized_flows=True,
    )


def read_quarterly_financials(
    stock_code,
    cumulative_statement_types=None,
    report_metadata_path=REPORT_METADATA_PATH,
    *,
    financial_dir=None,
    market="kr",
    use_edgartools=True,
    edgartools_provider=None,
):
    financial_dir = financial_dir if financial_dir is not None else financial_dir_for_market(market)
    if str(market or "kr").strip().lower() == "us" and use_edgartools:
        periodized = _periodized_financial_frame_with_edgartools(
            stock_code,
            financial_dir=financial_dir,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            market=market,
            use_edgartools=use_edgartools,
            edgartools_provider=edgartools_provider,
        )
        financial_df = _financial_frame_from_periodized(periodized, suffix="_quarter")
    else:
        financial_df = quarterly_financial_frame(
            normalize_symbol_for_market(stock_code, market),
            financial_dir,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            market=market,
        )
    if financial_df.empty:
        return financial_df
    return add_annual_financial_factors(
        financial_df,
        periods_per_year=4,
        annualized_flows=False,
    )


def yoy_pct(series, periods=1):
    return (series / series.shift(periods) - 1) * 100


def growth_pct(series, periods=1):
    previous = series.shift(periods)
    return (series - previous) / previous.abs() * 100


def profit_growth_pct(series, periods=1):
    current = pd.to_numeric(series, errors="coerce")
    previous = current.shift(periods)
    result = (current - previous) / previous.abs() * 100
    same_sign = ((current > 0) & (previous > 0)) | ((current < 0) & (previous < 0))
    return result.where(same_sign)


def cagr_pct(series, years, periods_per_year=1):
    periods = max(int(round(years * periods_per_year)), 1)
    previous = series.shift(periods)
    current = pd.to_numeric(series, errors="coerce")
    ratio = current / previous
    result = (ratio ** (1 / years) - 1) * 100
    return result.where((current > 0) & (previous > 0))


def add_annual_financial_factors(
    financial_df,
    periods_per_year=1,
    *,
    annualized_flows=False,
):
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
    cash_and_equivalents = numeric_column(df, "CASH_AND_EQUIVALENTS")
    short_term_financial_assets = numeric_column(df, "SHORT_TERM_FINANCIAL_ASSETS")
    disclosed_cash_assets = (
        cash_and_equivalents.fillna(0) + short_term_financial_assets.fillna(0)
    ).where(cash_and_equivalents.notna() | short_term_financial_assets.notna())
    df["che"] = bound_by_reference(
        disclosed_cash_assets.fillna(0),
        df["at"],
        1.5,
    )
    disclosed_cash_assets = bound_by_reference(disclosed_cash_assets, df["at"], 1.5)

    df["sale"] = numeric_column(df, "REVENUE")
    df["ni"] = numeric_column(df, "NET_INCOME")
    df["ni_parent"] = first_value_frame(df, "NET_INCOME_PARENT", "NET_INCOME")
    df["cogs"] = numeric_column(df, "COGS")
    df["gross_profit"] = first_value_frame(df, "GROSS_PROFIT")
    df["gross_profit"] = df["gross_profit"].fillna(df["sale"] - df["cogs"])
    reported_operating_income = numeric_column(df, "OPERATING_INCOME")
    derived_from_operating_expenses = finite_numeric_series(
        df["gross_profit"] - numeric_column(df, "OPERATING_EXPENSES_TOTAL")
    )
    derived_from_sgna = finite_numeric_series(df["gross_profit"] - numeric_column(df, "SGNA"))
    df["oiadp"] = reported_operating_income.fillna(derived_from_operating_expenses).fillna(derived_from_sgna)
    df["operating_income_source"] = pd.Series(pd.NA, index=df.index, dtype="object")
    df.loc[reported_operating_income.notna(), "operating_income_source"] = "reported_operating_income"
    df.loc[
        reported_operating_income.isna() & derived_from_operating_expenses.notna(),
        "operating_income_source",
    ] = "derived_operating_income"
    df.loc[
        reported_operating_income.isna()
        & derived_from_operating_expenses.isna()
        & derived_from_sgna.notna(),
        "operating_income_source",
    ] = "derived_operating_income"
    df.loc[df["oiadp"].isna(), "operating_income_source"] = "missing_operating_income"
    df["xrd"], df["xrd_imputed_zero"] = impute_missing_rnd_zero(
        numeric_column(df, "RND"),
        df,
    )
    df["xint"] = first_positive_value_frame(
        df,
        "INTEREST_EXPENSE",
        "INTEREST_EXPENSE_FALLBACK",
        "INT_PAID",
        "INTEREST_PAID_FALLBACK",
        "FINANCE_COST_FALLBACK",
    )
    interest_expense_denominator = positive_denominator(df["xint"])
    df["tax_expense"] = numeric_column(df, "TAX_EXPENSE")
    df["pbt"] = numeric_column(df, "PBT")

    cf_depreciation = numeric_column(df, "DEPRECIATION_EXPENSE")
    cf_amortization = numeric_column(df, "AMORTIZATION")
    df["dp"] = first_value_frame(df, "DNA_IS")
    cf_da = cf_depreciation.fillna(0) + cf_amortization.fillna(0)
    cf_da = cf_da.where(cf_depreciation.notna() | cf_amortization.notna())
    df["dp"] = df["dp"].fillna(cf_da)
    df["oibdp"] = first_value_frame(df, "EBITDA")
    df["oibdp"] = df["oibdp"].fillna(df["oiadp"] + df["dp"])

    df["oancf"] = numeric_column(df, "CFO")
    df["ivncf"] = numeric_column(df, "CFI")
    df["fincf"] = numeric_column(df, "CFF")
    capex_ppe = numeric_column(df, "CAPEX_PPE").abs()
    capex_intang = numeric_column(df, "CAPEX_INTANG").abs()
    df["capx"] = (capex_ppe.fillna(0) + capex_intang.fillna(0)).where(
        capex_ppe.notna() | capex_intang.notna()
    )
    df["fcf"] = df["oancf"] - df["capx"]
    df["ffo"] = df["ni"] + df["dp"].fillna(0)
    df["sstk"] = numeric_column(df, "EQ_ISSUE")
    df["prstkc"] = numeric_column(df, "BUYBACK")
    df["div_paid"] = numeric_column(df, "DIV_PAID").abs()
    df["debt_issue"] = numeric_column(df, "DEBT_ISSUE")
    df["debt_repay"] = numeric_column(df, "DEBT_REPAY")
    reported_net_borrowing = first_value_frame(df, "DEBT_NET_BORROWING")
    derived_net_borrowing = (
        df["debt_issue"].fillna(0) - df["debt_repay"].fillna(0)
    ).where(df["debt_issue"].notna() | df["debt_repay"].notna())
    strict_net_borrowing = reported_net_borrowing.fillna(derived_net_borrowing)
    df["net_borrowing"] = reported_net_borrowing
    df["net_borrowing"] = df["net_borrowing"].fillna(
        df["debt_issue"].fillna(0) - df["debt_repay"].fillna(0)
    )

    df["avg_assets"] = (df["at"] + df["at"].shift(lag)) / 2
    df["avg_equity"] = (df["seq"] + df["seq"].shift(lag)) / 2
    df["avg_inventory"] = (df["invt"] + df["invt"].shift(lag)) / 2
    df["avg_receivables"] = (df["rect"] + df["rect"].shift(lag)) / 2
    df["avg_payables"] = (df["ap"] + df["ap"].shift(lag)) / 2

    df["gpm"] = df["gross_profit"] / df["sale"]
    # Open Source Asset Pricing ``GP`` (Novy-Marx, 2013).  Keep the
    # point-in-time denominator from the same disclosed financial period.
    df["gross_profitability_pct"] = (
        df["gross_profit"] / positive_denominator(df["at"]) * 100
    )
    df["opm"] = df["oiadp"] / df["sale"]
    df["operating_profit_margin"] = df["opm"]
    df["operating_margin_growth_1y"] = growth_pct(df["opm"], periods=lag)
    df["ebitda_margin"] = df["oibdp"] / df["sale"]
    df["npm"] = df["ni"] / df["sale"]
    df["net_margin"] = df["npm"]
    df["fcf_margin"] = df["fcf"] / df["sale"]
    df["fcf_margin_growth_1y"] = growth_pct(df["fcf_margin"], periods=lag)
    df["rnd_margin"] = df["xrd"] / df["sale"]
    df["rnd_to_sales"] = df["xrd"] / df["sale"]
    df["tax_rate"] = df["tax_expense"] / df["pbt"]
    df.loc[(df["tax_rate"] < 0) | (df["tax_rate"] > 1), "tax_rate"] = math.nan
    nopat_tax_rate = tax_rate_for_nopat(df["tax_rate"], df["oiadp"])
    df["nopat"] = df["oiadp"] * (1 - nopat_tax_rate)
    df["nopat_quality_flag"] = df["operating_income_source"]

    # Internally generated knowledge and organization capital are expensed by
    # GAAP even though their benefits extend beyond the current period. Build
    # after-tax capital stocks with a perpetual-inventory model, then replace
    # current intangible investment in reported earnings with the economic
    # amortization of the existing stock. The amortization term is therefore
    # the observable maintenance-investment proxy; only net investment is
    # added back to current earning power.
    intangible_tax_rate = tax_rate_for_nopat(df["tax_rate"], df["oiadp"])
    after_tax_multiplier = 1 - intangible_tax_rate
    knowledge_investment = df["xrd"].where(df["xrd"] >= 0) * after_tax_multiplier
    sgna = numeric_column(df, "SGNA")
    sgna = sgna.where(sgna >= 0)
    organization_investment = (
        sgna
        * ORGANIZATION_CAPITAL_INVESTMENT_SHARE
        * after_tax_multiplier
    )
    knowledge_capital, knowledge_amortization = perpetual_intangible_capital(
        knowledge_investment,
        annual_depreciation_rate=KNOWLEDGE_CAPITAL_DEPRECIATION_RATE,
        periods_per_year=lag,
        annualized_flows=annualized_flows,
    )
    organization_capital, organization_amortization = perpetual_intangible_capital(
        organization_investment,
        annual_depreciation_rate=ORGANIZATION_CAPITAL_DEPRECIATION_RATE,
        periods_per_year=lag,
        annualized_flows=annualized_flows,
    )
    # R&D is the required anchor. Missing SG&A leaves an R&D-only adjustment;
    # missing R&D in an intensive sector does not get silently treated as zero.
    has_knowledge_disclosure = knowledge_investment.notna()
    df["knowledge_capital"] = knowledge_capital
    df["organization_capital"] = organization_capital
    df["intangible_capital"] = (
        knowledge_capital + organization_capital.fillna(0)
    ).where(has_knowledge_disclosure)
    df["intangible_investment"] = (
        knowledge_investment + organization_investment.fillna(0)
    ).where(has_knowledge_disclosure)
    df["intangible_amortization"] = (
        knowledge_amortization + organization_amortization.fillna(0)
    ).where(has_knowledge_disclosure)
    df["net_intangible_investment"] = (
        df["intangible_investment"] - df["intangible_amortization"]
    )
    df["intangible_adjusted_net_income"] = (
        df["ni_parent"] + df["net_intangible_investment"]
    )
    intangible_normalization_window = lag * PVGO_NORMALIZATION_YEARS
    intangible_normalization_min_periods = lag * PVGO_MIN_NORMALIZATION_YEARS
    df["normalized_intangible_adjusted_earnings_5y"] = (
        df["intangible_adjusted_net_income"]
        .rolling(
            intangible_normalization_window,
            min_periods=intangible_normalization_min_periods,
        )
        .mean()
    )

    df["avg_parent_equity"] = (df["ceq"] + df["ceq"].shift(lag)) / 2
    df["roe"] = df["ni_parent"] / df["avg_parent_equity"]
    df["roe_growth_1y"] = growth_pct(df["roe"], periods=lag)
    df["roe_growth_3y"] = growth_pct(df["roe"], periods=lag * 3)
    df["roe_growth_5y"] = growth_pct(df["roe"], periods=lag * 5)
    df["roa"] = df["ni"] / df["avg_assets"]
    df["accrual_ratio"] = (df["ni"] - df["oancf"]) / df["avg_assets"]

    # OAP ``PctTotAcc`` (Hafzalla, Lundholm and Van Winkle, 2011).
    # Requiring every cash-flow component prevents absent disclosures from
    # being silently interpreted as zero.
    total_accrual_cash_flow = (
        df["prstkc"]
        - df["sstk"]
        + df["div_paid"]
        + df["oancf"]
        + df["fincf"]
        + df["ivncf"]
    )
    absolute_net_income = df["ni"].abs().where(df["ni"].abs() > 0)
    df["percent_total_accruals_pct"] = (
        (df["ni"] - total_accrual_cash_flow) / absolute_net_income * 100
    )

    # OAP ``ChEQ``.  The source signal is current / lagged book equity;
    # expressing it as percentage growth is a monotonic, easier-to-read form.
    lagged_book_equity = df["ceq"].shift(lag)
    df["book_equity_growth_1y_pct"] = (
        (df["ceq"] / lagged_book_equity - 1) * 100
    ).where((df["ceq"] > 0) & (lagged_book_equity > 0))

    # OAP ``DelCOA``: annual investment in non-cash current operating assets,
    # scaled by average total assets.
    current_operating_assets = df["act"] - disclosed_cash_assets
    df["current_operating_assets_change_pct"] = (
        (current_operating_assets - current_operating_assets.shift(lag))
        / positive_denominator(df["avg_assets"])
        * 100
    )

    # OAP ``grcapx``.  Use reported PPE capex when available and the annual
    # change in net PPE as the documented fallback, then compare with t-2.
    annual_ppent_change = df["ppent"] - df["ppent"].shift(lag)
    comparable_capex = capex_ppe.fillna(annual_ppent_change)
    capex_two_years_ago = comparable_capex.shift(lag * 2)
    df["capex_growth_2y_pct"] = (
        (comparable_capex / capex_two_years_ago - 1) * 100
    ).where((comparable_capex >= 0) & (capex_two_years_ago > 0))

    # OAP ``NetDebtFinance``.  The original implementation drops observations
    # whose absolute net debt financing exceeds average total assets.
    df["net_debt_financing_pct"] = (
        strict_net_borrowing / positive_denominator(df["avg_assets"]) * 100
    )
    df["net_debt_financing_pct"] = df["net_debt_financing_pct"].where(
        df["net_debt_financing_pct"].abs() <= 100
    )

    # OAP ``InvGrowth``.  Inventory is a non-negative stock variable, so a
    # zero or negative lag is not a meaningful percentage-growth base.
    lagged_inventory = df["invt"].shift(lag)
    df["inventory_growth_1y_pct"] = (
        (df["invt"] / lagged_inventory - 1) * 100
    ).where((df["invt"] >= 0) & (lagged_inventory > 0))

    # OAP ``XFIN`` (Bradshaw, Richardson and Sloan, 2006): net equity plus
    # net debt financing, scaled by contemporaneous total assets.  Missing
    # equity-financing disclosures remain missing rather than being treated
    # as zero.
    df["net_external_financing_pct"] = (
        (
            df["sstk"]
            - df["div_paid"]
            - df["prstkc"]
            + df["net_borrowing"]
        )
        / positive_denominator(df["at"])
        * 100
    )
    df["intangible_adjusted_equity"] = df["ceq"] + df["intangible_capital"]
    df["avg_intangible_adjusted_equity"] = (
        df["intangible_adjusted_equity"]
        + df["intangible_adjusted_equity"].shift(lag)
    ) / 2
    positive_adjusted_equity = positive_denominator(
        df["avg_intangible_adjusted_equity"]
    )
    df["intangible_adjusted_roe_pct"] = (
        df["intangible_adjusted_net_income"] / positive_adjusted_equity * 100
    )
    # Keep the established IROE factor id, but correct its former one-period
    # R&D add-back so existing graphs receive the audited adjusted definition.
    df["iroe"] = df["intangible_adjusted_roe_pct"]
    df["debt"] = df["dltt"].fillna(0) + df["dlc"].fillna(0)
    df["avg_debt"] = ((df["debt"] + df["debt"].shift(lag)) / 2).fillna(df["debt"])
    df["net_debt"] = df["debt"] - df["che"].fillna(0)
    df["invested_capital_financial"] = df["seq"] + df["debt"] - df["che"].fillna(0)
    df["invested_capital_operational"] = (
        df["rect"].fillna(0)
        + df["invt"].fillna(0)
        - df["ap"].fillna(0)
        + df["ppent"].fillna(0)
        + numeric_column(df, "INTANGIBLE_ASSETS", 0).fillna(0)
    )
    df["avg_ic_financial"] = (
        (df["invested_capital_financial"] + df["invested_capital_financial"].shift(lag)) / 2
    ).fillna(df["invested_capital_financial"])
    df["avg_ic_operational"] = (
        (df["invested_capital_operational"] + df["invested_capital_operational"].shift(lag)) / 2
    ).fillna(df["invested_capital_operational"])
    df["roic_financial"] = df["nopat"] / df["avg_ic_financial"]
    df["roic_operational"] = df["nopat"] / df["avg_ic_operational"]
    df["roic_operational_growth_1y"] = growth_pct(df["roic_operational"], periods=lag)

    # PVGO inputs follow Arcana's P/Q/C/I separation. For the U.S. filing
    # universe P and Q are observable only as combined reported revenue
    # growth. C is represented by a cycle-normalized operating margin, and I
    # by incremental invested capital per incremental revenue. Calculate at
    # the financial-period level so daily forward-filled rows are not
    # overweighted in the normalization window.
    normalization_window = lag * PVGO_NORMALIZATION_YEARS
    normalization_min_periods = lag * PVGO_MIN_NORMALIZATION_YEARS
    normalized_operating_margin = df["opm"].rolling(
        normalization_window,
        min_periods=normalization_min_periods,
    ).mean()
    df["normalized_operating_margin_5y"] = normalized_operating_margin
    normalized_nopat_margin = (df["nopat"] / positive_denominator(df["sale"])).rolling(
        normalization_window,
        min_periods=normalization_min_periods,
    ).mean()
    df["normalized_nopat_5y"] = df["sale"] * normalized_nopat_margin
    df["normalized_earnings_5y"] = df["ni_parent"].rolling(
        normalization_window,
        min_periods=normalization_min_periods,
    ).mean()
    df["normalized_nopat_growth_3y_pct"] = cagr_pct(
        df["normalized_nopat_5y"],
        years=PVGO_GROWTH_LOOKBACK_YEARS,
        periods_per_year=lag,
    )

    incremental_sales = df["sale"] - df["sale"].shift(lag)
    incremental_invested_capital = (
        df["invested_capital_operational"]
        - df["invested_capital_operational"].shift(lag)
    )
    prior_invested_capital = positive_denominator(
        df["invested_capital_operational"].shift(lag).abs()
    )
    material_incremental_capital = incremental_invested_capital.where(
        incremental_invested_capital.abs() >= prior_invested_capital * 0.01
    )
    df["incremental_investment_rate_pct"] = (
        material_incremental_capital
        / positive_denominator(incremental_sales)
        * 100
    )
    df["roiic_pct"] = (
        (df["nopat"] - df["nopat"].shift(lag))
        / material_incremental_capital
        * 100
    )

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
    fcfe = df["fcf"] + df["net_borrowing"].fillna(0)
    shareholder_return_amount = df["div_paid"].fillna(0) + (
        df["prstkc"].fillna(0) - df["sstk"].fillna(0)
    )
    positive_shareholder_return = shareholder_return_amount.where(shareholder_return_amount > 0)
    fcf_after_dividends = df["fcf"] - df["div_paid"].fillna(0)
    diluted_shares = first_positive_value_frame(
        df,
        "DILUTED_SHARES",
        "BASIC_SHARES",
        "COMMON_SHARES_OUTSTANDING",
        "shares",
    )
    df["intangible_adjusted_eps"] = (
        df["intangible_adjusted_net_income"] / diluted_shares
    )
    df["normalized_intangible_adjusted_eps"] = (
        df["normalized_intangible_adjusted_earnings_5y"] / diluted_shares
    )
    eps = first_value_frame(df, "BASIC_EPS", "DILUTED_EPS")
    eps = eps.fillna(df["ni_parent"] / numeric_column(df, "shares"))
    retained_earnings = first_value_frame(
        df,
        "RETAINED_EARNINGS",
        "RETAINED_EARNINGS_FALLBACK",
    )
    tail_columns = pd.DataFrame(
        {
            "fcfe": fcfe,
            "fcf_payout_ratio": df["div_paid"] / positive_denominator(df["fcf"]),
            "fcf_dividend_coverage": df["fcf"] / positive_denominator(df["div_paid"]),
            "fcf_after_dividends": fcf_after_dividends,
            "fcf_after_dividends_to_sales_pct": fcf_after_dividends / df["sale"] * 100,
            "fcf_after_dividends_to_assets_pct": fcf_after_dividends / df["at"] * 100,
            "shareholder_return_fcf_coverage": df["fcf"] / positive_shareholder_return,
            "fcfe_dividend_coverage": fcfe / positive_denominator(df["div_paid"]),
            "fcfe_payout_ratio": df["div_paid"] / positive_denominator(fcfe),
            "capex_to_sales_pct": df["capx"] / df["sale"] * 100,
            "capex_to_cfo_pct": df["capx"] / positive_denominator(df["oancf"]) * 100,
            "net_debt_to_fcf": df["net_debt"] / positive_denominator(df["fcf"]),
            "interest_expense_to_fcf_pct": df["xint"].abs() / positive_denominator(df["fcf"]) * 100,
            "fcf_interest_coverage": df["fcf"] / positive_denominator(df["xint"].abs()),
            "fcf_volatility_5y": df["fcf"].rolling(lag * 5, min_periods=2).std(),
            "fcf_negative_freq_5y_pct": (df["fcf"] < 0).rolling(lag * 5, min_periods=1).mean() * 100,
            "fcf_volatility_10y": df["fcf"].rolling(lag * 10, min_periods=2).std(),
            "fcf_negative_freq_10y_pct": (df["fcf"] < 0).rolling(lag * 10, min_periods=1).mean() * 100,
            "sales_yoy_pct": yoy_pct(df["sale"], periods=lag),
            "op_yoy_pct": yoy_pct(df["oiadp"], periods=lag),
            "sales_growth_1y": growth_pct(df["sale"], periods=lag),
            "sales_growth_3y": growth_pct(df["sale"], periods=lag * 3),
            "sales_growth_5y": growth_pct(df["sale"], periods=lag * 5),
            "sales_cagr_3y": cagr_pct(df["sale"], years=3, periods_per_year=lag),
            "net_income_growth_1y": profit_growth_pct(df["ni_parent"], periods=lag),
            "net_income_growth_3y": profit_growth_pct(df["ni_parent"], periods=lag * 3),
            "net_income_growth_5y": profit_growth_pct(df["ni_parent"], periods=lag * 5),
            "operating_income_growth_1y": growth_pct(df["oiadp"], periods=lag),
            "operating_income_growth_3y": growth_pct(df["oiadp"], periods=lag * 3),
            "operating_income_growth_5y": growth_pct(df["oiadp"], periods=lag * 5),
            "sales_change_mil": (df["sale"] - df["sale"].shift(lag)) / 1_000_000,
            "op_change_mil": (df["oiadp"] - df["oiadp"].shift(lag)) / 1_000_000,
            "rdsr_pct": df["xrd"] / df["sale"] * 100,
            "eps": eps,
            "eps_yoy_pct": yoy_pct(eps, periods=lag),
            "asset_yoy_pct": yoy_pct(df["at"], periods=lag).where(
                (df["at"] > 0) & (df["at"].shift(lag) > 0)
            ),
            "cfo_yoy_pct": yoy_pct(df["oancf"], periods=lag),
            "fcf_yoy_pct": yoy_pct(df["fcf"], periods=lag),
            "ffo_yoy_pct": yoy_pct(df["ffo"], periods=lag),
            "net_debt_to_ebitda": df["net_debt"] / positive_denominator(df["oibdp"]),
            "fc_to_ndr": df["fcf"] / df["net_debt"],
            "icr_times": df["oancf"] / interest_expense_denominator,
            "interest_coverage": df["oiadp"] / interest_expense_denominator,
            "current_ratio": df["act"] / df["lct"],
            "debt_to_equity": df["debt"] / df["seq"],
            "cash_to_debt": df["che"] / df["debt"],
            "retained_earnings": retained_earnings,
            "altman_z_score": (
        1.2 * ((df["act"] - df["lct"]) / df["at"])
        + 1.4 * (retained_earnings / df["at"])
        + 3.3 * (df["oiadp"] / df["at"])
        + 1.0 * (df["sale"] / df["at"])
            ),
        },
        index=df.index,
    )
    df = pd.concat([df, tail_columns], axis=1)
    score_columns = pd.DataFrame(
        {
            "beneish_m_score": calculate_beneish_m_score(df, periods=lag),
            "f_score": calculate_piotroski_f_score(df, periods=lag),
        },
        index=df.index,
    )
    df = pd.concat([df, score_columns], axis=1)

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


DIVIDEND_FACTOR_COLUMNS = [
    "dvpsx",
    "dvpsp",
    "sharehold_div_yield",
    "tdpr",
    "total_dividend_amount",
    "forward_dividend_yield",
    "earnings_payout_ratio",
    "eps_dividend_coverage",
    "dps_yoy_pct",
    "dps_cagr_3y",
    "dps_cagr_5y",
    "dps_cagr_10y",
    "dividend_consistency_streak",
    "dividend_growth_streak",
    "dps_volatility_5y",
    "dps_volatility_10y",
    "dividend_cut",
    "dividend_change_momentum",
    "shareholder_yield",
    "special_dividend",
    "special_dividend_ratio_pct",
]


def empty_dividend_factors(df):
    result = df.copy()
    for column in DIVIDEND_FACTOR_COLUMNS:
        result[column] = math.nan
    result["dividend_fiscal_year"] = math.nan
    return result


def dividend_history_metrics(events):
    if events.empty or "bsns_year" not in events.columns:
        return pd.DataFrame()

    history = events.copy()
    history["dividend_fiscal_year"] = pd.to_numeric(history["bsns_year"], errors="coerce")
    history["annual_dividend_per_share"] = pd.to_numeric(
        history.get("annual_dividend_per_share"),
        errors="coerce",
    )
    history["total_dividend_amount"] = pd.to_numeric(
        history.get("total_dividend_amount"),
        errors="coerce",
    )
    if "report_date" in history.columns:
        history["report_date"] = pd.to_datetime(history["report_date"], errors="coerce")
    else:
        history["report_date"] = pd.NaT
    if "report_name" not in history.columns:
        history["report_name"] = ""

    history = (
        history.dropna(subset=["dividend_fiscal_year"])
        .sort_values(["dividend_fiscal_year", "report_date"])
        .groupby("dividend_fiscal_year", as_index=False)
        .agg(
            {
                "annual_dividend_per_share": "last",
                "total_dividend_amount": "last",
                "report_name": "last",
            }
        )
        .sort_values("dividend_fiscal_year")
        .reset_index(drop=True)
    )
    if history.empty:
        return history

    dps = pd.to_numeric(history["annual_dividend_per_share"], errors="coerce")
    history["dps_yoy_pct"] = growth_pct(dps, periods=1)
    history["dps_cagr_3y"] = cagr_pct(dps, years=3)
    history["dps_cagr_5y"] = cagr_pct(dps, years=5)
    history["dps_cagr_10y"] = cagr_pct(dps, years=10)
    history["dps_volatility_5y"] = dps.rolling(5, min_periods=2).std()
    history["dps_volatility_10y"] = dps.rolling(10, min_periods=2).std()
    history["dividend_cut"] = ((dps.shift(1) > 0) & (dps < dps.shift(1))).astype(float)
    history["dividend_change_momentum"] = history["dps_yoy_pct"]
    report_name = history["report_name"].fillna("").astype(str)
    history["special_dividend"] = report_name.str.contains(
        r"special|extra|one[- ]?time",
        case=False,
        regex=True,
    ).astype(float)
    history["special_dividend_ratio_pct"] = history["special_dividend"].where(
        history["special_dividend"] > 0,
        0.0,
    ) * 100

    consistency_streak = []
    growth_streak = []
    current_consistency = 0
    current_growth = 0
    previous_dps = math.nan
    for value in dps:
        if pd.notna(value) and value > 0:
            current_consistency += 1
            if pd.notna(previous_dps) and previous_dps > 0 and value > previous_dps:
                current_growth += 1
            else:
                current_growth = 0
        else:
            current_consistency = 0
            current_growth = 0
        consistency_streak.append(current_consistency)
        growth_streak.append(current_growth)
        previous_dps = value

    history["dividend_consistency_streak"] = consistency_streak
    history["dividend_growth_streak"] = growth_streak

    keep_columns = [
        "dividend_fiscal_year",
        "dps_yoy_pct",
        "dps_cagr_3y",
        "dps_cagr_5y",
        "dps_cagr_10y",
        "dividend_consistency_streak",
        "dividend_growth_streak",
        "dps_volatility_5y",
        "dps_volatility_10y",
        "dividend_cut",
        "dividend_change_momentum",
        "special_dividend",
        "special_dividend_ratio_pct",
    ]
    return history[keep_columns]


def merge_dividend_history(df, events):
    metrics = dividend_history_metrics(events)
    if metrics.empty or "dividend_fiscal_year" not in df.columns:
        for column in DIVIDEND_FACTOR_COLUMNS:
            if column not in df.columns:
                df[column] = math.nan
        return df

    result = df.merge(metrics, on="dividend_fiscal_year", how="left")
    for column in DIVIDEND_FACTOR_COLUMNS:
        if column not in result.columns:
            result[column] = math.nan
    return result


def add_kr_dividend_factors(daily_df, stock_code):
    df = daily_df.copy()
    dividend_events = silver_dividend_asof_events(stock_code)
    if not dividend_events.empty:
        events = dividend_events.copy()
        events["report_date"] = pd.to_datetime(events["report_date"], errors="coerce")
        for column in ["bsns_year", "report_name"]:
            if column not in events.columns:
                events[column] = math.nan if column == "bsns_year" else ""
        events = events.dropna(subset=["report_date"]).sort_values("report_date")
        if not events.empty:
            df = pd.merge_asof(
                df.sort_values("trade_date"),
                events[
                    [
                        "report_date",
                        "bsns_year",
                        "report_name",
                        "annual_dividend_per_share",
                        "payout_ratio",
                        "total_dividend_amount",
                    ]
                ].sort_values("report_date"),
                left_on="trade_date",
                right_on="report_date",
                direction="backward",
            )
            df["dividend_fiscal_year"] = pd.to_numeric(df["bsns_year"], errors="coerce")
            df["dvpsx"] = pd.to_numeric(df["annual_dividend_per_share"], errors="coerce")
            df["dvpsp"] = math.nan
            df["sharehold_div_yield"] = df["dvpsx"] / df["close"] * 100
            df.loc[
                (df["sharehold_div_yield"] < 0) | (df["sharehold_div_yield"] > 100),
                "sharehold_div_yield",
            ] = math.nan
            df["total_dividend_amount"] = pd.to_numeric(
                df["total_dividend_amount"],
                errors="coerce",
            )
            reported_payout_pct = pd.to_numeric(df["payout_ratio"], errors="coerce") * 100
            point_in_time_net_income = first_value_frame(df, "ni_parent", "ni")
            calculated_payout_pct = (
                df["total_dividend_amount"]
                / positive_denominator(point_in_time_net_income)
                * 100
            )
            df["tdpr"] = reported_payout_pct.combine_first(calculated_payout_pct)
            df.loc[df["tdpr"] < 0, "tdpr"] = math.nan
            df["forward_dividend_yield"] = df["dvpsp"] / df["close"] * 100
            df["earnings_payout_ratio"] = df["tdpr"]
            df["eps_dividend_coverage"] = numeric_column(df, "eps") / positive_denominator(df["dvpsx"])
            df["shareholder_yield"] = math.nan
            df = merge_dividend_history(df, events)
            return df.drop(
                columns=[
                    "report_date",
                    "bsns_year",
                    "report_name",
                    "annual_dividend_per_share",
                    "payout_ratio",
                ],
                errors="ignore",
            )

    df = empty_dividend_factors(df)

    return df


def add_us_dividend_factors(daily_df, stock_code, market_data_cache=None):
    df = daily_df.sort_values("trade_date").copy()
    if market_data_cache is not None:
        security_id = security_id_for_market(stock_code, "us")
        dividends = market_data_cache.dividends(security_id)
    else:
        dividends = read_stock_dividends(stock_code, market="us")
    if dividends.empty:
        return empty_dividend_factors(df)

    events = dividends.loc[pd.to_numeric(dividends.get("dividend"), errors="coerce") > 0].copy()
    if events.empty:
        return empty_dividend_factors(df)

    events["dividend"] = pd.to_numeric(events["dividend"], errors="coerce")
    events = events.sort_values("trade_date")
    trade_dates = pd.DatetimeIndex(df["trade_date"])
    dividend_events = events.groupby("trade_date")["dividend"].sum()
    event_index = dividend_events.index.union(trade_dates).sort_values()
    event_series = dividend_events.reindex(event_index).fillna(0)
    payout_ratio = (
        pd.to_numeric(events["payout_ratio"], errors="coerce")
        if "payout_ratio" in events.columns
        else pd.Series([math.nan] * len(events), index=events.index)
    )
    if payout_ratio.notna().any():
        payout_events = (
            events.assign(_payout_ratio=payout_ratio)
            .dropna(subset=["_payout_ratio"])
            .groupby("trade_date")["_payout_ratio"]
            .last()
        )
        payout_index = payout_events.index.union(trade_dates).sort_values()
        payout_series = payout_events.reindex(payout_index).ffill().reindex(trade_dates)
    else:
        payout_series = pd.Series([math.nan] * len(df), index=df.index)
    rolling_dps = event_series.rolling("365D", min_periods=1).sum().reindex(trade_dates)
    df["dvpsx"] = rolling_dps.to_numpy()
    df.loc[df["dvpsx"] <= 0, "dvpsx"] = math.nan
    df["dvpsp"] = math.nan
    df["sharehold_div_yield"] = df["dvpsx"] / df["close"] * 100
    df["tdpr"] = payout_series.to_numpy() * 100
    df["total_dividend_amount"] = df["dvpsx"] * numeric_column(df, "shares")
    df["dividend_fiscal_year"] = df["trade_date"].dt.year
    df["forward_dividend_yield"] = math.nan
    df["earnings_payout_ratio"] = df["tdpr"]
    df["eps_dividend_coverage"] = numeric_column(df, "eps") / positive_denominator(df["dvpsx"])
    df["dps_yoy_pct"] = yoy_pct(pd.to_numeric(df["dvpsx"], errors="coerce"), periods=252)
    df["dps_cagr_3y"] = cagr_pct(pd.to_numeric(df["dvpsx"], errors="coerce"), years=3, periods_per_year=252)
    df["dps_cagr_5y"] = cagr_pct(pd.to_numeric(df["dvpsx"], errors="coerce"), years=5, periods_per_year=252)
    df["dps_cagr_10y"] = cagr_pct(pd.to_numeric(df["dvpsx"], errors="coerce"), years=10, periods_per_year=252)
    df["dps_volatility_5y"] = pd.to_numeric(df["dvpsx"], errors="coerce").rolling(252 * 5, min_periods=2).std()
    df["dps_volatility_10y"] = pd.to_numeric(df["dvpsx"], errors="coerce").rolling(252 * 10, min_periods=2).std()
    df["dividend_cut"] = (
        (pd.to_numeric(df["dvpsx"], errors="coerce").shift(252) > 0)
        & (pd.to_numeric(df["dvpsx"], errors="coerce") < pd.to_numeric(df["dvpsx"], errors="coerce").shift(252))
    ).astype(float)
    df["dividend_change_momentum"] = df["dps_yoy_pct"]
    df["dividend_consistency_streak"] = math.nan
    df["dividend_growth_streak"] = math.nan
    df["shareholder_yield"] = math.nan
    df["special_dividend"] = math.nan
    df["special_dividend_ratio_pct"] = math.nan

    return df


def add_dividend_factors(daily_df, stock_code, market="kr", market_data_cache=None):
    market = str(market or "kr").strip().lower()
    if market == "us":
        return add_us_dividend_factors(daily_df, stock_code, market_data_cache=market_data_cache)
    return add_kr_dividend_factors(daily_df, stock_code)


def add_consensus_factors(
    daily_df,
    financial_df,
    stock_code,
    *,
    estimate_gold_root=ESTIMATE_GOLD_ROOT,
    market="kr",
):
    df = daily_df.sort_values("trade_date").copy()
    for column in CONSENSUS_FACTOR_COLUMNS:
        if column not in df.columns:
            df[column] = math.nan

    market = str(market or "kr").strip().lower()
    if market != "kr" or df.empty or financial_df is None or financial_df.empty:
        return df

    consensus_df = read_estimate_consensus_frame(stock_code, estimate_gold_root=estimate_gold_root)
    if consensus_df.empty:
        return df

    actual_df = consensus_actual_frame(financial_df)
    if actual_df.empty:
        return df

    events = build_consensus_factor_events(consensus_df, actual_df)
    if events.empty:
        return df
    return merge_consensus_factor_events(df, events)


def read_estimate_consensus_frame(stock_code, *, estimate_gold_root=ESTIMATE_GOLD_ROOT):
    stock_code = normalize_stock_code(stock_code)
    estimate_dir = Path(estimate_gold_root) / stock_code
    consensus_path = estimate_dir / "arcana_estimate_consensus.csv"
    component_path = estimate_dir / "arcana_estimate_component.csv"
    if not consensus_path.exists() or not component_path.exists():
        return pd.DataFrame()

    consensus = pd.read_csv(consensus_path, dtype={"stock_code": str, "target_period": str})
    components = pd.read_csv(
        component_path,
        dtype={"stock_code": str, "target_period": str, "source_actual_period": str},
        usecols=lambda column: column
        in {"stock_code", "target_period", "metric_id", "scenario", "source_actual_period"},
    )
    if consensus.empty or components.empty:
        return pd.DataFrame()

    consensus["stock_code"] = consensus["stock_code"].map(normalize_stock_code)
    components["stock_code"] = components["stock_code"].map(normalize_stock_code)
    consensus["metric_id"] = consensus["metric_id"].astype(str)
    components["metric_id"] = components["metric_id"].astype(str)
    consensus = consensus[consensus["metric_id"].isin(CONSENSUS_METRIC_SPECS)].copy()
    components = components[components["metric_id"].isin(CONSENSUS_METRIC_SPECS)].copy()
    if consensus.empty or components.empty:
        return pd.DataFrame()

    source_periods = (
        components.dropna(subset=["source_actual_period"])
        .sort_values(["stock_code", "target_period", "metric_id", "scenario", "source_actual_period"])
        .drop_duplicates(["stock_code", "target_period", "metric_id", "scenario"], keep="last")
    )
    keep_columns = ["stock_code", "target_period", "metric_id", "scenario", "source_actual_period"]
    result = consensus.merge(
        source_periods[keep_columns],
        on=["stock_code", "target_period", "metric_id", "scenario"],
        how="left",
    )
    result["consensus_mean"] = pd.to_numeric(result.get("consensus_mean"), errors="coerce")
    result = result.dropna(subset=["consensus_mean", "source_actual_period"])
    return result


def consensus_actual_frame(financial_df):
    if financial_df is None or financial_df.empty:
        return pd.DataFrame()
    required = {"fiscal_year", "fiscal_month", "report_date"}
    if not required.issubset(financial_df.columns):
        return pd.DataFrame()

    df = financial_df.copy()
    df["fiscal_year"] = pd.to_numeric(df["fiscal_year"], errors="coerce").astype("Int64")
    df["fiscal_month"] = pd.to_numeric(df["fiscal_month"], errors="coerce").astype("Int64")
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df = df.dropna(subset=["fiscal_year", "fiscal_month", "report_date"])
    if df.empty:
        return pd.DataFrame()

    result = pd.DataFrame(index=df.index)
    result["period"] = df["fiscal_year"].astype(int).astype(str) + "." + df["fiscal_month"].astype(int).astype(str).str.zfill(2)
    result["report_date"] = df["report_date"]
    for metric_id, spec in CONSENSUS_METRIC_SPECS.items():
        result[metric_id] = first_value_frame(df, *spec["actual_columns"])

    result = result.sort_values(["period", "report_date"]).drop_duplicates("period", keep="last")
    return result.reset_index(drop=True)


def build_consensus_factor_events(consensus_df, actual_df):
    if consensus_df.empty or actual_df.empty:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])

    period_report_date = actual_df.set_index("period")["report_date"]
    actual_by_metric = {
        metric_id: actual_df.set_index("period")[metric_id]
        for metric_id in CONSENSUS_METRIC_SPECS
        if metric_id in actual_df.columns
    }

    rows = []
    for row in consensus_df.to_dict("records"):
        metric_id = str(row.get("metric_id") or "")
        spec = CONSENSUS_METRIC_SPECS.get(metric_id)
        if not spec:
            continue

        consensus_value = _finite_float(row.get("consensus_mean"))
        if consensus_value is None:
            continue

        source_period = str(row.get("source_actual_period") or "")
        target_period = str(row.get("target_period") or "")
        source_report_date = period_report_date.get(source_period)
        target_report_date = period_report_date.get(target_period)

        source_actual = _series_lookup_float(actual_by_metric.get(metric_id), source_period)
        expected_growth = _pct_diff(consensus_value, source_actual)
        if expected_growth is not None and pd.notna(source_report_date):
            rows.append(
                {
                    "available_date": source_report_date,
                    "factor_id": spec["expected_factor"],
                    "factor_value": expected_growth,
                    "priority": spec["priority"],
                }
            )

        target_actual = _series_lookup_float(actual_by_metric.get(metric_id), target_period)
        surprise = _pct_diff(target_actual, consensus_value)
        if surprise is not None and pd.notna(target_report_date):
            if pd.isna(source_report_date) or pd.Timestamp(source_report_date) < pd.Timestamp(target_report_date):
                rows.append(
                    {
                        "available_date": target_report_date,
                        "factor_id": spec["surprise_factor"],
                        "factor_value": surprise,
                        "priority": spec["priority"],
                    }
                )

    if not rows:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])
    events = pd.DataFrame(rows)
    events["available_date"] = pd.to_datetime(events["available_date"], errors="coerce")
    events["factor_value"] = pd.to_numeric(events["factor_value"], errors="coerce")
    events = events.dropna(subset=["available_date", "factor_id", "factor_value"])
    return events.sort_values(["available_date", "factor_id", "priority"]).reset_index(drop=True)


def merge_consensus_factor_events(daily_df, events):
    return _merge_factor_events(daily_df, events, CONSENSUS_FACTOR_COLUMNS)


def add_real_consensus_factors(
    daily_df,
    financial_df,
    stock_code,
    *,
    real_consensus_daily_path=HANKYUNG_CONSENSUS_DAILY_PATH,
    market="kr",
):
    df = daily_df.sort_values("trade_date").copy()
    for column in REAL_CONSENSUS_FACTOR_COLUMNS:
        if column not in df.columns:
            df[column] = math.nan
    for column in REAL_CONSENSUS_INPUT_COLUMNS:
        if column not in df.columns:
            df[column] = math.nan

    market = str(market or "kr").strip().lower()
    if market != "kr" or df.empty:
        return df

    consensus_df = read_real_consensus_daily_frame(
        stock_code,
        real_consensus_daily_path=real_consensus_daily_path,
    )
    if consensus_df.empty:
        return df

    df = merge_forward_consensus_inputs(
        df,
        consensus_df,
        stale_days=DEFAULT_FORWARD_CONSENSUS_STALE_DAYS,
    )
    events = build_real_consensus_factor_events(consensus_df, consensus_actual_frame(financial_df))
    if events.empty:
        return df
    return merge_real_consensus_factor_events(df, events)


def add_kr_target_price_factor(
    daily_df,
    stock_code,
    *,
    target_price_consensus_path=HANKYUNG_TARGET_PRICE_CONSENSUS_PATH,
    market="kr",
):
    """Merge PIT Hankyung target prices from the first trading day after each event."""

    df = daily_df.sort_values("trade_date").copy()
    for column in KR_TARGET_PRICE_INPUT_COLUMNS:
        if column not in df.columns:
            df[column] = math.nan
    if KR_PRICE_TO_TARGET_PRICE_FACTOR not in df.columns:
        df[KR_PRICE_TO_TARGET_PRICE_FACTOR] = math.nan
    if str(market or "kr").strip().lower() != "kr" or df.empty:
        return df

    events = read_kr_target_price_consensus_frame(
        stock_code,
        target_price_consensus_path=target_price_consensus_path,
    )
    if events.empty:
        return df

    left = (
        df.reset_index(names="_kr_target_row")
        .drop(columns=KR_TARGET_PRICE_INPUT_COLUMNS, errors="ignore")
        .sort_values("trade_date")
    )
    merged = pd.merge_asof(
        left,
        events[["event_date", "target_price_mean", "analyst_count"]].sort_values(
            "event_date"
        ),
        left_on="trade_date",
        right_on="event_date",
        direction="backward",
        allow_exact_matches=False,
    )
    target_price = pd.to_numeric(merged["target_price_mean"], errors="coerce")
    analyst_count = pd.to_numeric(merged["analyst_count"], errors="coerce")
    eligible = (
        target_price.gt(0)
        & target_price.map(np.isfinite)
        & analyst_count.ge(MIN_KR_TARGET_PRICE_ANALYSTS)
    )
    eligible_target_price = target_price.where(eligible)
    df.loc[merged["_kr_target_row"], "kr_target_price"] = eligible_target_price.to_numpy()
    df.loc[
        merged["_kr_target_row"],
        "kr_target_price_analyst_count",
    ] = analyst_count.to_numpy()

    source_close = df.get("close")
    if source_close is None:
        source_close = pd.Series(math.nan, index=df.index, dtype="float64")
    close = pd.to_numeric(source_close, errors="coerce")
    target_price = pd.to_numeric(df["kr_target_price"], errors="coerce")
    df[KR_PRICE_TO_TARGET_PRICE_FACTOR] = (close / target_price).where(
        close.gt(0)
        & close.map(np.isfinite)
        & target_price.gt(0)
        & target_price.map(np.isfinite)
    )
    return df


def add_us_consensus_factors(
    daily_df,
    stock_code,
    *,
    us_consensus_factors_path=US_CONSENSUS_FACTORS_PATH,
    market="kr",
):
    """Merge provider-neutral US consensus factors as point-in-time data."""
    df = daily_df.sort_values("trade_date").copy()
    for column in [*US_CONSENSUS_FACTOR_COLUMNS, *US_CONSENSUS_INPUT_COLUMNS]:
        if column not in df.columns:
            df[column] = "" if column in US_CONSENSUS_STRING_INPUT_COLUMNS else math.nan
    if US_PRICE_TO_TARGET_PRICE_FACTOR not in df.columns:
        df[US_PRICE_TO_TARGET_PRICE_FACTOR] = math.nan
    if str(market or "kr").strip().lower() != "us" or df.empty:
        return df

    factors = read_us_consensus_factor_frame(stock_code, us_consensus_factors_path=us_consensus_factors_path)
    if factors.empty:
        return df
    target_events = _us_target_price_events(factors)
    composite = _us_consensus_composite_events(factors)
    if not composite.empty:
        merge_columns = [
            *US_CONSENSUS_FACTOR_COLUMNS,
            "us_eps_consensus",
            "us_revenue_consensus",
            "us_operating_income_consensus",
            "us_eps_revision_7d_pct",
            "us_eps_revision_60d_pct",
            "us_eps_revision_90d_pct",
            "analyst_count",
            "source_regime",
            "horizon",
        ]
        for column in merge_columns:
            if column not in composite.columns:
                composite[column] = pd.NA
        left = (
            df.reset_index(names="_us_consensus_row")
            .drop(columns=merge_columns, errors="ignore")
            .sort_values("trade_date")
        )
        right = composite[["factor_date", *merge_columns]].sort_values("factor_date")
        merged = pd.merge_asof(left, right, left_on="trade_date", right_on="factor_date", direction="backward", suffixes=("", "_us"))
        for column in US_CONSENSUS_FACTOR_COLUMNS:
            df.loc[merged["_us_consensus_row"], column] = pd.to_numeric(merged[column], errors="coerce").to_numpy()
        raw_mapping = {
            "us_eps_consensus": "us_eps_consensus",
            "us_revenue_consensus": "us_revenue_consensus",
            "us_operating_income_consensus": "us_operating_income_consensus",
            "us_eps_revision_7d_pct": "us_eps_revision_7d_pct",
            "us_eps_revision_60d_pct": "us_eps_revision_60d_pct",
            "us_eps_revision_90d_pct": "us_eps_revision_90d_pct",
            "us_consensus_analyst_count": "analyst_count",
            "us_consensus_source_regime": "source_regime",
            "us_consensus_horizon": "horizon",
        }
        for target, source in raw_mapping.items():
            values = merged[source]
            if target not in US_CONSENSUS_STRING_INPUT_COLUMNS:
                values = pd.to_numeric(values, errors="coerce")
            df.loc[merged["_us_consensus_row"], target] = values.to_numpy()

    if not target_events.empty:
        target_columns = [
            "us_target_price",
            "us_target_price_analyst_count",
            "us_target_price_provider",
            "us_target_price_source_regime",
        ]
        left = (
            df.reset_index(names="_us_target_row")
            .drop(columns=target_columns, errors="ignore")
            .sort_values("trade_date")
        )
        merged_targets = pd.merge_asof(
            left,
            target_events[["factor_date", *target_columns]].sort_values(
                "factor_date"
            ),
            left_on="trade_date",
            right_on="factor_date",
            direction="backward",
        )
        for column in target_columns:
            values = merged_targets[column]
            if column not in US_CONSENSUS_STRING_INPUT_COLUMNS:
                values = pd.to_numeric(values, errors="coerce")
            df.loc[merged_targets["_us_target_row"], column] = values.to_numpy()

    source_close = df.get("close")
    if source_close is None:
        source_close = pd.Series(math.nan, index=df.index, dtype="float64")
    close = pd.to_numeric(source_close, errors="coerce")
    target_price = pd.to_numeric(df["us_target_price"], errors="coerce")
    df[US_PRICE_TO_TARGET_PRICE_FACTOR] = (close / target_price).where(
        close.gt(0)
        & close.map(np.isfinite)
        & target_price.gt(0)
        & target_price.map(np.isfinite)
    )
    return df


def _us_consensus_composite_events(factors):
    """Coalesce each field FMP -> Alpha Vantage -> Yahoo without cross-provider revisions."""
    numeric_columns = [
        *US_CONSENSUS_FACTOR_COLUMNS,
        "us_eps_consensus",
        "us_revenue_consensus",
        "us_operating_income_consensus",
        "us_eps_revision_7d_pct",
        "us_eps_revision_60d_pct",
        "us_eps_revision_90d_pct",
        "analyst_count",
    ]
    working = factors.copy()
    for column in numeric_columns:
        if column not in working.columns:
            working[column] = math.nan
        working[column] = pd.to_numeric(working[column], errors="coerce")
    for column in ("provider", "source_regime", "horizon", "raw_path"):
        if column not in working.columns:
            working[column] = ""
        working[column] = working[column].fillna("").astype(str)

    regimes = working["source_regime"]
    horizons = working["horizon"]
    is_fmp_current = regimes.eq("FMP_CURRENT") & horizons.eq("FY1")
    is_alpha_historical = regimes.eq("ALPHA_VANTAGE_HISTORICAL") & horizons.eq("FQ1")
    is_yahoo_current = regimes.eq("YAHOO_CURRENT") & horizons.eq("FY1")
    non_target_columns = [
        *US_CONSENSUS_FACTOR_COLUMNS,
        "us_eps_consensus",
        "us_revenue_consensus",
        "us_operating_income_consensus",
        "us_eps_revision_7d_pct",
        "us_eps_revision_60d_pct",
        "us_eps_revision_90d_pct",
    ]
    has_consensus_value = working[non_target_columns].notna().any(axis=1)
    working = working.loc[
        has_consensus_value
        & (is_fmp_current | is_alpha_historical | is_yahoo_current)
    ].copy()
    if working.empty:
        return pd.DataFrame()

    current_mask = working["source_regime"].isin({"FMP_CURRENT", "YAHOO_CURRENT"})
    current_start = working.loc[current_mask, "factor_date"].min()
    if pd.notna(current_start):
        working = working.loc[
            ~working["source_regime"].eq("ALPHA_VANTAGE_HISTORICAL")
            | working["factor_date"].lt(current_start)
        ].copy()
    working = _collapse_us_provider_rows(working, numeric_columns)
    if working.empty:
        return pd.DataFrame()

    event_dates = sorted(working["factor_date"].dropna().unique())
    provider_order = ("FMP", "ALPHA_VANTAGE", "YAHOO_FINANCE")
    rows = []
    for factor_date in event_dates:
        available = working.loc[working["factor_date"].le(factor_date)]
        latest = {}
        for provider in provider_order:
            provider_rows = available.loc[available["provider"].eq(provider)]
            if provider == "ALPHA_VANTAGE" and pd.notna(current_start) and pd.Timestamp(
                factor_date
            ) >= pd.Timestamp(current_start):
                provider_rows = provider_rows.loc[
                    ~provider_rows["source_regime"].eq(
                        "ALPHA_VANTAGE_HISTORICAL"
                    )
                ]
            if provider_rows.empty:
                continue
            latest[provider] = provider_rows.sort_values(
                ["factor_date", "raw_path"]
            ).iloc[-1]
        if not latest:
            continue
        output = {"factor_date": pd.Timestamp(factor_date)}
        source_row = None
        for column in US_CONSENSUS_FACTOR_COLUMNS:
            value, row = _first_eligible_us_value(
                latest,
                provider_order,
                column,
                require_analysts=True,
            )
            output[column] = value
            if column == "us_eps_revision_30d_pct" and row is not None:
                source_row = row
        for column in (
            "us_eps_consensus",
            "us_revenue_consensus",
            "us_operating_income_consensus",
            "us_eps_revision_7d_pct",
            "us_eps_revision_60d_pct",
            "us_eps_revision_90d_pct",
        ):
            value, row = _first_eligible_us_value(
                latest,
                provider_order,
                column,
                require_analysts=False,
            )
            output[column] = value
            if source_row is None and column == "us_eps_consensus" and row is not None:
                source_row = row
        if source_row is not None:
            output["analyst_count"] = pd.to_numeric(
                source_row.get("analyst_count"), errors="coerce"
            )
            output["source_regime"] = str(source_row.get("source_regime", ""))
            output["horizon"] = str(source_row.get("horizon", ""))
        else:
            output["analyst_count"] = math.nan
            output["source_regime"] = ""
            output["horizon"] = ""
        rows.append(output)
    return pd.DataFrame(rows).sort_values("factor_date").reset_index(drop=True)


def _collapse_us_provider_rows(factors, numeric_columns):
    rows = []
    group_columns = ["factor_date", "provider", "source_regime", "horizon"]
    for keys, group in factors.sort_values(["factor_date", "raw_path"]).groupby(
        group_columns,
        sort=True,
        dropna=False,
    ):
        row = dict(zip(group_columns, keys))
        for column in numeric_columns:
            values = pd.to_numeric(group[column], errors="coerce").dropna()
            row[column] = values.iloc[-1] if not values.empty else math.nan
        row["raw_path"] = str(group["raw_path"].iloc[-1])
        rows.append(row)
    return pd.DataFrame(rows)


def _first_eligible_us_value(
    latest,
    provider_order,
    column,
    *,
    require_analysts,
):
    candidates = []
    for provider in provider_order:
        row = latest.get(provider)
        if row is None:
            continue
        value = pd.to_numeric(row.get(column), errors="coerce")
        if pd.isna(value) or not np.isfinite(value):
            continue
        analysts = pd.to_numeric(row.get("analyst_count"), errors="coerce")
        if require_analysts and (pd.isna(analysts) or analysts < 3):
            continue
        candidates.append(
            (
                pd.Timestamp(row.get("factor_date")),
                -provider_order.index(provider),
                float(value),
                row,
            )
        )
    if candidates:
        _, _, value, row = max(candidates, key=lambda candidate: candidate[:2])
        return value, row
    return math.nan, None


def _us_target_price_events(factors):
    """Choose PIT targets in strict Finnworlds, FMP, Alpha, Yahoo order."""
    horizons = factors["horizon"].fillna("").astype(str)
    regimes = factors["source_regime"].fillna("").astype(str)
    providers = factors["provider"].fillna("").astype(str)
    is_finnworlds = providers.eq("FINNWORLDS") & regimes.isin(
        {
            "FINNWORLDS_OFFICIAL_CURRENT",
            "FINNWORLDS_OFFICIAL_EXPIRED",
            "FINNWORLDS_PIT_HISTORICAL",
        }
    )
    is_fmp = providers.eq("FMP") & regimes.eq("FMP_CURRENT")
    is_alpha = providers.eq("ALPHA_VANTAGE") & regimes.eq("ALPHA_VANTAGE_CURRENT")
    is_yahoo = providers.eq("YAHOO_FINANCE") & regimes.eq("YAHOO_CURRENT")
    events = factors.loc[
        horizons.eq("FY1")
        & (is_finnworlds | is_fmp | is_alpha | is_yahoo)
    ].copy()
    if events.empty:
        return pd.DataFrame(
            columns=[
                "factor_date",
                "us_target_price",
                "us_target_price_analyst_count",
                "us_target_price_provider",
                "us_target_price_source_regime",
            ]
        )

    events["us_target_price"] = pd.to_numeric(
        events.get("us_target_price"),
        errors="coerce",
    )
    events["analyst_count"] = pd.to_numeric(
        events.get("analyst_count"),
        errors="coerce",
    )
    events["_target_eligible"] = (
        events["us_target_price"].gt(0)
        & events["us_target_price"].map(np.isfinite)
        & events["analyst_count"].ge(3)
    )
    if "raw_path" not in events.columns:
        events["raw_path"] = ""
    provider_order = (
        "FINNWORLDS",
        "FMP",
        "ALPHA_VANTAGE",
        "YAHOO_FINANCE",
    )
    rows = []
    for factor_date in sorted(events["factor_date"].dropna().unique()):
        available = events.loc[events["factor_date"].le(factor_date)]
        chosen = None
        for provider in provider_order:
            provider_rows = available.loc[available["provider"].eq(provider)]
            if provider_rows.empty:
                continue
            if provider == "FINNWORLDS":
                official_rows = provider_rows.loc[
                    provider_rows["source_regime"].isin(
                        {
                            "FINNWORLDS_OFFICIAL_CURRENT",
                            "FINNWORLDS_OFFICIAL_EXPIRED",
                        }
                    )
                ]
                official = (
                    official_rows.sort_values(["factor_date", "raw_path"]).iloc[-1]
                    if not official_rows.empty
                    else None
                )
                if official is not None and bool(official["_target_eligible"]):
                    chosen = official
                else:
                    pit_rows = provider_rows.loc[
                        provider_rows["source_regime"].eq(
                            "FINNWORLDS_PIT_HISTORICAL"
                        )
                    ]
                    pit = (
                        pit_rows.sort_values(["factor_date", "raw_path"]).iloc[-1]
                        if not pit_rows.empty
                        else None
                    )
                    if pit is not None and bool(pit["_target_eligible"]):
                        chosen = pit
            else:
                latest = provider_rows.sort_values(
                    ["factor_date", "raw_path"]
                ).iloc[-1]
                if bool(latest["_target_eligible"]):
                    chosen = latest
            if chosen is not None:
                break
        rows.append(
            {
                "factor_date": pd.Timestamp(factor_date),
                "us_target_price": (
                    float(chosen["us_target_price"])
                    if chosen is not None
                    else math.nan
                ),
                "us_target_price_analyst_count": (
                    float(chosen["analyst_count"])
                    if chosen is not None
                    else math.nan
                ),
                "us_target_price_provider": (
                    str(chosen["provider"]) if chosen is not None else ""
                ),
                "us_target_price_source_regime": (
                    str(chosen["source_regime"]) if chosen is not None else ""
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("factor_date").reset_index(drop=True)


def _us_analyst_consensus_is_eligible(df):
    analyst_count = pd.to_numeric(
        numeric_column(df, "us_consensus_analyst_count"),
        errors="coerce",
    )
    horizon = (
        df["us_consensus_horizon"].fillna("").astype(str).str.upper()
        if "us_consensus_horizon" in df.columns
        else pd.Series("", index=df.index, dtype="object")
    )
    return (analyst_count >= 3) & (horizon.eq("") | horizon.eq("FY1"))


def _us_current_operating_income(df):
    return finite_numeric_series(first_value_frame(df, "oiadp", "OPERATING_INCOME"))


def _us_current_net_income(df):
    return finite_numeric_series(first_value_frame(df, "ni_parent", "ni", "NET_INCOME"))


def _us_diluted_shares(df):
    return finite_numeric_series(
        first_value_frame(
            df,
            "DILUTED_SHARES",
            "BASIC_SHARES",
            "COMMON_SHARES_OUTSTANDING",
            "shares",
        )
    )


def _eps_implied_operating_income(df):
    """Infer FY1 operating income by holding the disclosed OI/NI relation constant."""
    consensus_eps = finite_numeric_series(numeric_column(df, "us_eps_consensus"))
    operating_income = _us_current_operating_income(df)
    net_income = _us_current_net_income(df)
    diluted_shares = _us_diluted_shares(df)
    implied_net_income = consensus_eps * diluted_shares
    implied_operating_income = operating_income * implied_net_income / net_income
    valid = (
        _us_analyst_consensus_is_eligible(df)
        & (consensus_eps > 0)
        & (diluted_shares > 0)
        & (net_income > 0)
        & (operating_income > 0)
        & np.isfinite(implied_operating_income)
    )
    return implied_operating_income.where(valid)


def add_eps_implied_operating_income_surprise_factor(daily_df, *, market="kr"):
    """Add a US FY1 EPS-consensus-implied operating-income surprise factor.

    The estimate keeps the latest disclosed operating-income-to-net-income
    relation and diluted-share count constant, then compares the implied FY1
    operating income against the disclosed operating income.
    """
    df = daily_df.copy()
    df[EPS_IMPLIED_OPERATING_INCOME_SURPRISE_FACTOR] = math.nan
    if str(market or "kr").strip().lower() != "us" or df.empty:
        return df

    operating_income = _us_current_operating_income(df)
    implied_operating_income = _eps_implied_operating_income(df)
    surprise = (implied_operating_income - operating_income) / operating_income.abs() * 100
    df[EPS_IMPLIED_OPERATING_INCOME_SURPRISE_FACTOR] = finite_numeric_series(surprise)
    return df


def read_us_consensus_factor_frame(stock_code, *, us_consensus_factors_path=US_CONSENSUS_FACTORS_PATH):
    path = Path(us_consensus_factors_path)
    frame = _cached_us_consensus_factor_frame(str(path))
    if frame.empty:
        return pd.DataFrame()
    symbol = normalize_symbol_for_market(stock_code, market="us")
    result = frame.loc[frame["symbol"].astype(str).map(lambda value: normalize_symbol_for_market(value, market="us")) == symbol].copy()
    if result.empty:
        return result
    result["factor_date"] = pd.to_datetime(result["factor_date"], errors="coerce")
    return result.dropna(subset=["factor_date"])


@lru_cache(maxsize=4)
def _cached_us_consensus_factor_frame(path_text):
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"symbol": str, "horizon": str, "provider": str, "source_regime": str})


def read_real_consensus_daily_frame(stock_code, *, real_consensus_daily_path=HANKYUNG_CONSENSUS_DAILY_PATH):
    path = Path(real_consensus_daily_path)
    frame = _cached_real_consensus_daily_frame(str(path))
    if frame.empty:
        return pd.DataFrame()
    stock_code = normalize_stock_code(stock_code)
    result = frame.loc[frame["stock_code"].astype(str).map(normalize_stock_code) == stock_code].copy()
    if result.empty:
        return result
    result["stock_code"] = result["stock_code"].map(normalize_stock_code)
    result["as_of_date"] = pd.to_datetime(result["as_of_date"], errors="coerce")
    result["target_period"] = result["target_period"].astype(str)
    result["metric_id"] = result["metric_id"].astype(str)
    result["consensus_mean"] = pd.to_numeric(result["consensus_mean"], errors="coerce")
    return result.dropna(subset=["as_of_date", "target_period", "metric_id", "consensus_mean"])


@lru_cache(maxsize=4)
def _cached_real_consensus_daily_frame(path_text):
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"stock_code": str, "target_period": str, "metric_id": str})


def read_kr_target_price_consensus_frame(
    stock_code,
    *,
    target_price_consensus_path=HANKYUNG_TARGET_PRICE_CONSENSUS_PATH,
):
    path = Path(target_price_consensus_path)
    frame = _cached_kr_target_price_consensus_frame(str(path))
    required_columns = {
        "stock_code",
        "event_date",
        "target_price_mean",
        "analyst_count",
    }
    if frame.empty or not required_columns.issubset(frame.columns):
        return pd.DataFrame()
    stock_code = normalize_stock_code(stock_code)
    result = frame.loc[
        frame["stock_code"].astype(str).map(normalize_stock_code) == stock_code
    ].copy()
    if result.empty:
        return result
    result["stock_code"] = result["stock_code"].map(normalize_stock_code)
    result["event_date"] = pd.to_datetime(result["event_date"], errors="coerce")
    result["target_price_mean"] = pd.to_numeric(
        result["target_price_mean"],
        errors="coerce",
    )
    result["analyst_count"] = pd.to_numeric(result["analyst_count"], errors="coerce")
    return result.dropna(subset=["event_date"]).sort_values("event_date")


@lru_cache(maxsize=4)
def _cached_kr_target_price_consensus_frame(path_text):
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"stock_code": str})


def merge_forward_consensus_inputs(
    daily_df,
    consensus_df,
    *,
    stale_days=DEFAULT_FORWARD_CONSENSUS_STALE_DAYS,
):
    """Point-in-time merge the nearest non-expired annual forward estimate."""

    df = daily_df.sort_values("trade_date").copy()
    if df.empty or consensus_df is None or consensus_df.empty:
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["_forward_row_id"] = np.arange(len(df))
    rows = consensus_df.copy()
    rows["as_of_date"] = pd.to_datetime(rows["as_of_date"], errors="coerce")
    rows["target_end_date"] = rows["target_period"].map(_target_period_end_date)
    rows["consensus_mean"] = pd.to_numeric(rows["consensus_mean"], errors="coerce")
    rows = rows.dropna(
        subset=["as_of_date", "target_end_date", "metric_id", "consensus_mean"]
    )

    for metric_id in REAL_CONSENSUS_INPUT_COLUMNS:
        df[metric_id] = math.nan
        metric_rows = rows.loc[rows["metric_id"].astype(str) == metric_id].copy()
        if metric_rows.empty:
            continue

        candidates = []
        for target_end_date, target_rows in metric_rows.groupby("target_end_date", sort=True):
            target_rows = (
                target_rows.sort_values("as_of_date")
                .drop_duplicates("as_of_date", keep="last")
            )
            merged = pd.merge_asof(
                df[["_forward_row_id", "trade_date"]].sort_values("trade_date"),
                target_rows[["as_of_date", "consensus_mean"]].sort_values("as_of_date"),
                left_on="trade_date",
                right_on="as_of_date",
                direction="backward",
            )
            age_days = (merged["trade_date"] - merged["as_of_date"]).dt.days
            valid = (
                merged["consensus_mean"].notna()
                & (pd.Timestamp(target_end_date) >= merged["trade_date"])
                & age_days.between(0, int(stale_days), inclusive="both")
            )
            if not valid.any():
                continue
            candidate = merged.loc[
                valid,
                ["_forward_row_id", "consensus_mean"],
            ].copy()
            candidate["target_end_date"] = pd.Timestamp(target_end_date)
            candidates.append(candidate)

        if not candidates:
            continue
        selected = (
            pd.concat(candidates, ignore_index=True)
            .sort_values(["_forward_row_id", "target_end_date"])
            .drop_duplicates("_forward_row_id", keep="first")
            .set_index("_forward_row_id")["consensus_mean"]
        )
        df[metric_id] = df["_forward_row_id"].map(selected)

    return df.drop(columns=["_forward_row_id"])


def _target_period_end_date(value):
    text = str(value or "").strip()
    match = re.match(r"^(?P<year>\d{4})[.:-]?(?P<month>\d{2})$", text)
    if not match:
        return pd.NaT
    year = int(match.group("year"))
    month = int(match.group("month"))
    if month < 1 or month > 12:
        return pd.NaT
    return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)


def build_real_consensus_factor_events(consensus_df, actual_df):
    if consensus_df.empty:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])

    rows = []
    rows.extend(build_real_eps_revision_events(consensus_df).to_dict("records"))
    if not actual_df.empty:
        rows.extend(build_real_expected_growth_events(consensus_df, actual_df).to_dict("records"))
        rows.extend(build_real_surprise_events(consensus_df, actual_df).to_dict("records"))

    if not rows:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])
    events = pd.DataFrame(rows)
    events["available_date"] = pd.to_datetime(events["available_date"], errors="coerce")
    events["factor_value"] = pd.to_numeric(events["factor_value"], errors="coerce")
    events = events.dropna(subset=["available_date", "factor_id", "factor_value"])
    return events.sort_values(["available_date", "factor_id", "priority"]).reset_index(drop=True)


def build_real_eps_revision_events(consensus_df):
    eps_rows = consensus_df.loc[consensus_df["metric_id"].astype(str) == "basic_eps"].copy()
    if eps_rows.empty:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])
    eps_rows = eps_rows.sort_values(["target_period", "as_of_date"])
    rows = []
    for _, group in eps_rows.groupby("target_period", dropna=False):
        group = group.sort_values("as_of_date").reset_index(drop=True)
        for row in group.to_dict("records"):
            current_value = _finite_float(row.get("consensus_mean"))
            if current_value is None:
                continue
            lookup_date = pd.Timestamp(row["as_of_date"]) - pd.DateOffset(months=1)
            prior = group.loc[group["as_of_date"] <= lookup_date]
            if prior.empty:
                continue
            prior_value = _finite_float(prior.iloc[-1].get("consensus_mean"))
            revision = _pct_diff(current_value, prior_value)
            if revision is None:
                continue
            rows.append(
                {
                    "available_date": row["as_of_date"],
                    "factor_id": "real_eps_revision_1m_pct",
                    "factor_value": revision,
                    "priority": 0,
                }
            )
    return pd.DataFrame(rows, columns=["available_date", "factor_id", "factor_value", "priority"])


def build_real_expected_growth_events(consensus_df, actual_df):
    if consensus_df.empty or actual_df.empty:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])

    actual_by_metric = {
        metric_id: actual_df.set_index("period")[metric_id]
        for metric_id in REAL_CONSENSUS_METRIC_SPECS
        if metric_id in actual_df.columns
    }
    rows = []
    for row in consensus_df.to_dict("records"):
        metric_id = str(row.get("metric_id") or "")
        spec = REAL_CONSENSUS_METRIC_SPECS.get(metric_id)
        if not spec:
            continue
        consensus_value = _finite_float(row.get("consensus_mean"))
        source_period = _previous_target_period(row.get("target_period"))
        source_actual = _series_lookup_float(actual_by_metric.get(metric_id), source_period)
        expected_growth = _pct_diff(consensus_value, source_actual)
        if expected_growth is None:
            continue
        rows.append(
            {
                "available_date": row.get("as_of_date"),
                "factor_id": spec["expected_factor"],
                "factor_value": expected_growth,
                "priority": spec["priority"],
            }
        )
    return pd.DataFrame(rows, columns=["available_date", "factor_id", "factor_value", "priority"])


def build_real_surprise_events(consensus_df, actual_df):
    if consensus_df.empty or actual_df.empty:
        return pd.DataFrame(columns=["available_date", "factor_id", "factor_value", "priority"])

    period_report_date = actual_df.set_index("period")["report_date"]
    actual_by_metric = {
        metric_id: actual_df.set_index("period")[metric_id]
        for metric_id in REAL_CONSENSUS_METRIC_SPECS
        if metric_id in actual_df.columns
    }
    rows = []
    for (target_period, metric_id), group in consensus_df.groupby(["target_period", "metric_id"], dropna=False):
        spec = REAL_CONSENSUS_METRIC_SPECS.get(str(metric_id))
        if not spec:
            continue
        target_report_date = period_report_date.get(str(target_period))
        if pd.isna(target_report_date):
            continue
        target_actual = _series_lookup_float(actual_by_metric.get(str(metric_id)), str(target_period))
        if target_actual is None:
            continue
        available_consensus = group.loc[group["as_of_date"] <= pd.Timestamp(target_report_date)].sort_values("as_of_date")
        if available_consensus.empty:
            continue
        consensus_value = _finite_float(available_consensus.iloc[-1].get("consensus_mean"))
        surprise = _pct_diff(target_actual, consensus_value)
        if surprise is None:
            continue
        rows.append(
            {
                "available_date": target_report_date,
                "factor_id": spec["surprise_factor"],
                "factor_value": surprise,
                "priority": spec["priority"],
            }
        )
    return pd.DataFrame(rows, columns=["available_date", "factor_id", "factor_value", "priority"])


def merge_real_consensus_factor_events(daily_df, events):
    return _merge_factor_events(daily_df, events, REAL_CONSENSUS_FACTOR_COLUMNS)


def _merge_factor_events(daily_df, events, columns):
    df = daily_df.sort_values("trade_date").copy()
    if events.empty:
        return df

    event_values = (
        events.sort_values(["available_date", "factor_id", "priority"])
        .drop_duplicates(["available_date", "factor_id"], keep="first")
        .pivot(index="available_date", columns="factor_id", values="factor_value")
        .sort_index()
        .ffill()
    )
    for column in columns:
        if column not in event_values.columns:
            event_values[column] = math.nan
    event_values = event_values[columns].reset_index()

    merged = pd.merge_asof(
        df.sort_values("trade_date"),
        event_values.sort_values("available_date"),
        left_on="trade_date",
        right_on="available_date",
        direction="backward",
        suffixes=("", "_consensus"),
    )
    for column in columns:
        consensus_column = f"{column}_consensus"
        if consensus_column in merged.columns:
            merged[column] = merged[consensus_column]
    return merged.drop(columns=["available_date", *[f"{column}_consensus" for column in columns]], errors="ignore")


def _previous_target_period(target_period):
    text = str(target_period or "").strip()
    match = re.match(r"^(?P<year>\d{4})[.:-]?(?P<month>\d{2})$", text)
    if not match:
        return ""
    return f"{int(match.group('year')) - 1}.{match.group('month')}"


def _finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _series_lookup_float(series, key):
    if series is None:
        return None
    try:
        value = series.get(key)
    except AttributeError:
        return None
    return _finite_float(value)


def _pct_diff(current, reference):
    current_value = _finite_float(current)
    reference_value = _finite_float(reference)
    if current_value is None or reference_value is None or reference_value == 0:
        return None
    return (current_value - reference_value) / abs(reference_value) * 100


def max_drawdown(returns):
    wealth = returns.fillna(0).add(1).cumprod()
    drawdown = wealth / wealth.cummax() - 1
    return drawdown.min()


def calculate_k_ratio(
    prices,
    *,
    window=K_RATIO_3Y_WINDOW,
    min_periods=K_RATIO_3Y_MIN_PERIODS,
):
    """Calculate Kestner's 2003 K-Ratio with an n-scaled slope error."""

    price = pd.to_numeric(prices, errors="coerce")
    log_vami = np.log(price.where(price > 0))
    x = pd.Series(np.arange(len(log_vami), dtype="float64"), index=log_vami.index)
    valid_x = x.where(log_vami.notna())

    rolling = log_vami.rolling(window, min_periods=min_periods)
    n = rolling.count()
    sum_y = rolling.sum()
    sum_y2 = log_vami.pow(2).rolling(window, min_periods=min_periods).sum()
    sum_x = valid_x.rolling(window, min_periods=min_periods).sum()
    sum_x2 = valid_x.pow(2).rolling(window, min_periods=min_periods).sum()
    sum_xy = (valid_x * log_vami).rolling(window, min_periods=min_periods).sum()

    sxx = sum_x2 - sum_x.pow(2) / n
    syy = sum_y2 - sum_y.pow(2) / n
    sxy = sum_xy - sum_x * sum_y / n
    slope = sxy / sxx
    sse = (syy - sxy.pow(2) / sxx).clip(lower=0)
    slope_standard_error = np.sqrt((sse / (n - 2)) / sxx)
    result = slope / (n * slope_standard_error)

    valid = (
        (n >= int(min_periods))
        & (n > 2)
        & (sxx > 0)
        & (slope_standard_error > 0)
        & np.isfinite(result)
    )
    return result.where(valid)


def add_price_momentum_factors(daily_df):
    df = daily_df.sort_values("trade_date").copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    adjusted_close = numeric_column(df, "adj_close")
    k_ratio_price = adjusted_close if (adjusted_close > 0).any() else close
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
    df["k_ratio_3y"] = calculate_k_ratio(k_ratio_price)
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
    fcf = numeric_column(df, "fcf")
    fcfe = numeric_column(df, "fcfe")
    sale_for_fcf = numeric_column(df, "sale")
    total_dividend_amount = numeric_column(df, "total_dividend_amount")
    div_paid = numeric_column(df, "div_paid")
    cash_dividends = total_dividend_amount.where(total_dividend_amount.notna(), div_paid)
    cash_dividends = cash_dividends.where(cash_dividends > 0)

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
    df["fcf_yield"] = fcf / positive_denominator(market_cap) * 100
    df["npr"] = (che - debt) / market_cap
    df["rpr"] = xrd / market_cap
    df["rnd_to_market_cap"] = xrd / market_cap * 100
    ev_input_missing = market_cap.isna() | (debt.isna() & che.isna())
    df["enterprise_value"] = market_cap + debt.fillna(0) - che.fillna(0)
    valid_ev = df["enterprise_value"].where(df["enterprise_value"] > 0)
    valid_oibdp = nonzero_denominator(oibdp)
    df["ev_ebitda_quality_flag"] = pd.Series(pd.NA, index=df.index, dtype="object")
    df.loc[ev_input_missing, "ev_ebitda_quality_flag"] = "missing_enterprise_value_inputs"
    df.loc[oibdp.isna(), "ev_ebitda_quality_flag"] = "missing_ebitda"
    df.loc[df["enterprise_value"].notna() & (df["enterprise_value"] <= 0), "ev_ebitda_quality_flag"] = "non_positive_enterprise_value"
    df.loc[oibdp == 0, "ev_ebitda_quality_flag"] = "zero_ebitda"
    df.loc[oibdp < 0, "ev_ebitda_quality_flag"] = "negative_ebitda"
    df["ev_nopat_quality_flag"] = pd.Series(pd.NA, index=df.index, dtype="object")
    df.loc[ev_input_missing, "ev_nopat_quality_flag"] = "missing_enterprise_value_inputs"
    df.loc[nopat.isna(), "ev_nopat_quality_flag"] = "missing_nopat"
    df.loc[df["enterprise_value"].notna() & (df["enterprise_value"] <= 0), "ev_nopat_quality_flag"] = "non_positive_enterprise_value"
    df.loc[nopat == 0, "ev_nopat_quality_flag"] = "zero_nopat"
    df.loc[nopat < 0, "ev_nopat_quality_flag"] = "negative_nopat"
    df["ebitda_to_ev"] = oibdp / valid_ev
    df["fcf_to_ev_yield"] = fcf / positive_denominator(df["enterprise_value"]) * 100
    df["ev_to_ebitda"] = valid_ev / valid_oibdp
    df["ev_to_nopat"] = valid_ev / positive_denominator(nopat)
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
    df["earnings_payout_ratio"] = tdpr
    df["peg"] = df["per"] / eps_yoy_pct
    df.loc[eps_yoy_pct <= 0, "peg"] = math.nan
    df["sharehold_net_buyback_yield"] = (
        (prstkc.fillna(0) - sstk.fillna(0)) / market_cap * 100
    )
    df["sharehold_return"] = sharehold_div_yield.fillna(0) + df["sharehold_net_buyback_yield"].fillna(0)
    df["shareholder_yield"] = df["sharehold_return"]
    net_buyback_amount = prstkc.fillna(0) - sstk.fillna(0)
    shareholder_return_amount = cash_dividends.fillna(0) + net_buyback_amount
    positive_shareholder_return = shareholder_return_amount.where(shareholder_return_amount > 0)
    df["fcf_payout_ratio"] = cash_dividends / positive_denominator(fcf) * 100
    df["fcf_dividend_coverage"] = fcf / positive_denominator(cash_dividends)
    df["fcf_after_dividends"] = fcf - cash_dividends.fillna(0)
    df["fcf_after_dividends_to_sales_pct"] = df["fcf_after_dividends"] / sale_for_fcf * 100
    df["fcf_after_dividends_to_assets_pct"] = df["fcf_after_dividends"] / at * 100
    df["fcf_after_dividends_to_market_cap_pct"] = (
        df["fcf_after_dividends"] / positive_denominator(market_cap) * 100
    )
    df["shareholder_return_fcf_coverage"] = fcf / positive_shareholder_return
    df["fcfe_dividend_coverage"] = fcfe / positive_denominator(cash_dividends)
    df["fcfe_payout_ratio"] = cash_dividends / positive_denominator(fcfe) * 100
    df["fcf_yield_dividend_yield_spread"] = df["fcf_yield"] - df["dividend_yield"]
    df["forward_dividend_yield"] = numeric_column(df, "dvpsp") / positive_denominator(close) * 100
    df["eps_dividend_coverage"] = df["eps"] / positive_denominator(numeric_column(df, "dvpsx"))

    return df


def add_wacc_factors(
    daily_df,
    *,
    market="kr",
    stock_code=None,
    market_data_cache=None,
    wacc_risk_free_path=None,
    wacc_erp_path=None,
    wacc_assumptions_path=None,
    wacc_benchmark_path=None,
):
    df = daily_df.copy()
    market = str(market or "kr").strip().lower()
    assumptions = (
        market_data_cache.wacc_assumptions()
        if market_data_cache is not None
        else read_wacc_assumptions(wacc_assumptions_path or SILVER_WACC_ASSUMPTIONS_PATH)
    )
    risk_free = (
        market_data_cache.risk_free_rates()
        if market_data_cache is not None
        else _read_optional_wacc_csv(wacc_risk_free_path or SILVER_RISK_FREE_RATE_PATH)
    )
    country_erps = (
        market_data_cache.country_erps()
        if market_data_cache is not None
        else _read_optional_wacc_csv(wacc_erp_path or SILVER_COUNTRY_ERP_PATH)
    )
    benchmark_weekly = (
        market_data_cache.benchmark_weekly_returns()
        if market_data_cache is not None
        else _read_optional_wacc_csv(wacc_benchmark_path or SILVER_WACC_BENCHMARK_WEEKLY_RETURNS_PATH)
    )

    default_beta = market_assumption(assumptions, market, "default_beta")
    df["beta"] = _daily_beta_series(
        df,
        market=market,
        default_beta=default_beta,
        benchmark_weekly_returns=benchmark_weekly,
    )
    risk_free_rate = risk_free_series_for_market(
        risk_free,
        market,
        df.index,
        df["trade_date"] if "trade_date" in df.columns else pd.Series(pd.NaT, index=df.index),
        assumptions,
    )
    equity_risk_premium = latest_country_erp(country_erps, market, assumptions)
    credit_spread = market_assumption(assumptions, market, "credit_spread")

    market_cap = positive_denominator(numeric_column(df, "market_cap"))
    debt = numeric_column(df, "debt").fillna(0).clip(lower=0)
    total_capital = market_cap + debt
    df["wacc_equity_weight"] = market_cap / positive_denominator(total_capital) * 100
    df["wacc_debt_weight"] = debt / positive_denominator(total_capital) * 100

    df["cost_of_equity"] = risk_free_rate + pd.to_numeric(df["beta"], errors="coerce") * equity_risk_premium
    tax_rate = _tax_rate_ratio(numeric_column(df, "tax_rate"))
    avg_debt = numeric_column(df, "avg_debt")
    avg_debt = avg_debt.where(avg_debt > 0, debt.where(debt > 0))
    observed_debt_cost = numeric_column(df, "xint").abs() / positive_denominator(avg_debt) * 100
    observed_debt_cost = observed_debt_cost.where((observed_debt_cost > 0) & (observed_debt_cost <= 100))
    fallback_debt_cost = risk_free_rate + credit_spread
    df["cost_of_debt_pre_tax"] = observed_debt_cost.fillna(fallback_debt_cost)
    df["cost_of_debt_after_tax"] = df["cost_of_debt_pre_tax"] * (1 - tax_rate.fillna(DEFAULT_NOPAT_TAX_RATE))
    df["wacc"] = (
        df["wacc_equity_weight"] / 100 * df["cost_of_equity"]
        + df["wacc_debt_weight"] / 100 * df["cost_of_debt_after_tax"]
    )
    df["roic_wacc_spread"] = numeric_column(df, "roic_operational") - df["wacc"]
    df["economic_profit"] = (
        df["roic_wacc_spread"]
        / 100
        * positive_denominator(numeric_column(df, "avg_ic_operational"))
    )
    df["economic_profit_yield"] = (
        df["economic_profit"]
        / positive_denominator(numeric_column(df, "enterprise_value"))
        * 100
    )
    df["delta_economic_profit"] = df["economic_profit"] - df["economic_profit"].shift(252)
    df["roic_wacc_spread_growth_1y"] = growth_pct(df["roic_wacc_spread"], periods=252)
    return df


def add_pvgo_factors(
    daily_df,
    *,
    cap_years=PVGO_CAP_YEARS,
    max_growth_rate=PVGO_MAX_GROWTH_RATE,
):
    """Add market-implied and P/Q/C/I-justified PVGO factors.

    ``pvgo_pct`` reproduces the enterprise-value method used in the attached
    Mauboussin/Callahan paper. ``equity_pvgo_pct`` is the separate equity
    formulation intended for financial companies. ``pvgo_gap_pct`` compares
    market PVGO with a finite-CAP value of future investments derived from the
    Arcana P/Q/C/I inputs.
    """

    cap_years = int(cap_years)
    if cap_years < 1:
        raise ValueError("cap_years must be at least 1")
    max_growth_rate = float(max_growth_rate)
    if not 0 < max_growth_rate < 1:
        raise ValueError("max_growth_rate must satisfy 0 < value < 1")

    df = daily_df.copy()
    market_cap = positive_denominator(numeric_column(df, "market_cap"))
    enterprise_value = positive_denominator(numeric_column(df, "enterprise_value"))
    nopat = numeric_column(df, "nopat")
    normalized_nopat = numeric_column(df, "normalized_nopat_5y")
    normalized_earnings = numeric_column(df, "normalized_earnings_5y")
    sales = positive_denominator(numeric_column(df, "sale"))
    wacc = numeric_column(df, "wacc") / 100
    cost_of_equity = numeric_column(df, "cost_of_equity") / 100
    valid_wacc = wacc.where((wacc > 0) & (wacc < 1))
    valid_cost_of_equity = cost_of_equity.where(
        (cost_of_equity > 0) & (cost_of_equity < 1)
    )
    net_debt = numeric_column(df, "net_debt").fillna(
        enterprise_value - market_cap
    )

    steady_state_ev = nopat / valid_wacc
    steady_state_equity = steady_state_ev - net_debt
    market_pvgo = market_cap - steady_state_equity
    df["pvgo_pct"] = market_pvgo / market_cap * 100
    df["pvgo_ev_pct"] = (enterprise_value - steady_state_ev) / enterprise_value * 100
    df["pvgo_expectation_factor"] = -df["pvgo_pct"]

    normalized_steady_state_ev = normalized_nopat / valid_wacc
    normalized_steady_state_equity = normalized_steady_state_ev - net_debt
    normalized_market_pvgo = market_cap - normalized_steady_state_equity
    df["normalized_pvgo_pct"] = normalized_market_pvgo / market_cap * 100

    # Debt is an operating input rather than ordinary financing for banks and
    # similar firms. Keep the equity formulation separate so the FactorLab
    # graph can route or exclude Financials explicitly.
    steady_state_financial_equity = normalized_earnings / valid_cost_of_equity
    df["equity_pvgo_pct"] = (
        (market_cap - steady_state_financial_equity) / market_cap * 100
    )

    roiic = numeric_column(df, "roiic_pct") / 100
    df["roiic_wacc_spread"] = numeric_column(df, "roiic_pct") - numeric_column(
        df,
        "wacc",
    )
    growth_rate = (
        numeric_column(df, "normalized_nopat_growth_3y_pct") / 100
    ).clip(lower=0, upper=max_growth_rate)
    investment_rate = (
        numeric_column(df, "incremental_investment_rate_pct") / 100
    ).clip(lower=0, upper=5)
    justified_pvgo = pd.Series(0.0, index=df.index, dtype="float64")
    valid_justified = (
        normalized_nopat.gt(0)
        & sales.gt(0)
        & growth_rate.gt(0)
        & investment_rate.notna()
        & roiic.gt(valid_wacc)
    )
    for year in range(1, cap_years + 1):
        growth_multiplier = (1 + growth_rate).pow(year - 1)
        incremental_nopat = normalized_nopat * growth_multiplier * growth_rate
        incremental_sales = sales * growth_multiplier * growth_rate
        required_investment = incremental_sales * investment_rate
        opportunity_npv = incremental_nopat / valid_wacc - required_investment
        justified_pvgo = justified_pvgo + opportunity_npv.clip(lower=0) / (
            1 + valid_wacc
        ).pow(year)
    justified_pvgo = justified_pvgo.where(valid_justified)
    df["justified_pvgo_pct"] = justified_pvgo / market_cap * 100
    df["pvgo_gap_pct"] = df["justified_pvgo_pct"] - df["pvgo_pct"]

    positive_steady_state_ev = steady_state_ev.where(steady_state_ev > 0)
    steady_state_growth = growth_pct(positive_steady_state_ev, periods=252)
    market_cap_growth = growth_pct(market_cap, periods=252)
    df["pvgo_compression_pct"] = steady_state_growth - market_cap_growth
    df["pvgo_change_1y_pctp"] = df["pvgo_pct"] - df["pvgo_pct"].shift(252)

    # Equity PVGO based on intangible-adjusted EPS. At the aggregate level
    # P - EPS/r is exactly market_cap - adjusted_net_income/r, while avoiding
    # split and diluted-versus-outstanding share-count mismatches.
    adjusted_earnings = numeric_column(df, "intangible_adjusted_net_income")
    normalized_adjusted_earnings = numeric_column(
        df,
        "normalized_intangible_adjusted_earnings_5y",
    )
    adjusted_steady_state_equity = adjusted_earnings / valid_cost_of_equity
    normalized_adjusted_steady_state_equity = (
        normalized_adjusted_earnings / valid_cost_of_equity
    )
    df["intangible_adjusted_pvgo_pct"] = (
        (market_cap - adjusted_steady_state_equity) / market_cap * 100
    )
    df["normalized_intangible_adjusted_pvgo_pct"] = (
        (market_cap - normalized_adjusted_steady_state_equity) / market_cap * 100
    )
    df["intangible_adjusted_roe_spread_pct"] = (
        numeric_column(df, "intangible_adjusted_roe_pct")
        - numeric_column(df, "cost_of_equity")
    )
    df["intangible_adjusted_pvgo_gap_pct"] = (
        df["justified_pvgo_pct"]
        - df["normalized_intangible_adjusted_pvgo_pct"]
    )
    positive_adjusted_steady_state_equity = (
        normalized_adjusted_steady_state_equity.where(
            normalized_adjusted_steady_state_equity > 0
        )
    )
    df["intangible_adjusted_pvgo_compression_pct"] = (
        growth_pct(positive_adjusted_steady_state_equity, periods=252)
        - market_cap_growth
    )
    df["intangible_adjusted_pvgo_change_1y_pctp"] = (
        df["normalized_intangible_adjusted_pvgo_pct"]
        - df["normalized_intangible_adjusted_pvgo_pct"].shift(252)
    )
    return df


def add_equity_valuation_factors(
    daily_df,
    *,
    rim_decay_factor=DEFAULT_RIM_DECAY_FACTOR,
    market="kr",
):
    decay = float(rim_decay_factor)
    if not 0 <= decay < 1:
        raise ValueError("rim_decay_factor must satisfy 0 <= value < 1")

    df = daily_df.copy()
    forward_per = pd.to_numeric(numeric_column(df, "forward_per"), errors="coerce")
    forward_roe = (
        pd.to_numeric(numeric_column(df, "forward_roe"), errors="coerce") / 100
    )
    forward_roe = forward_roe.where(np.isfinite(forward_roe))
    historical_roe = pd.to_numeric(
        numeric_column(df, "historical_roe_3y_avg"),
        errors="coerce",
    ) / 100
    historical_roe = historical_roe.where(np.isfinite(historical_roe))
    if str(market or "kr").strip().lower() == "us":
        current_roe = pd.to_numeric(numeric_column(df, "roe"), errors="coerce") / 100
        current_roe = current_roe.where(np.isfinite(current_roe))
        operating_income = _us_current_operating_income(df)
        analyst_operating_income = finite_numeric_series(
            first_value_frame(
                df,
                "us_operating_income_consensus",
                "operating_income_consensus",
            )
        )
        analyst_roe = current_roe * analyst_operating_income / operating_income
        analyst_roe = analyst_roe.where(
            _us_analyst_consensus_is_eligible(df)
            & (operating_income > 0)
            & (analyst_operating_income > 0)
            & np.isfinite(analyst_roe)
        )
        eps_implied_surprise = finite_numeric_series(
            numeric_column(df, EPS_IMPLIED_OPERATING_INCOME_SURPRISE_FACTOR)
        )
        eps_implied_roe = current_roe * (1 + eps_implied_surprise / 100)
        eps_implied_roe = eps_implied_roe.where(
            (operating_income > 0)
            & eps_implied_surprise.notna()
            & np.isfinite(eps_implied_roe)
        )
        rim_roe = analyst_roe.fillna(eps_implied_roe).fillna(historical_roe)
    else:
        rim_roe = forward_roe.fillna(historical_roe)
    required_return = pd.to_numeric(numeric_column(df, "cost_of_equity"), errors="coerce") / 100
    price = positive_denominator(numeric_column(df, "close"))
    book_value_per_share = positive_denominator(numeric_column(df, "bps"))

    implied_growth = required_return - 1 / forward_per
    eps_first_year = 1 / forward_per
    duration_pv = pd.Series(0.0, index=df.index, dtype="float64")
    duration_weighted_pv = pd.Series(0.0, index=df.index, dtype="float64")
    for year in range(1, EQUITY_DURATION_HORIZON_YEARS):
        cash_flow = eps_first_year * (1 + implied_growth).pow(year - 1)
        present_value = cash_flow / (1 + required_return).pow(year)
        duration_pv = duration_pv + present_value
        duration_weighted_pv = duration_weighted_pv + year * present_value

    terminal_present_value = 1 - duration_pv
    modified_duration = (
        duration_weighted_pv
        + EQUITY_DURATION_HORIZON_YEARS * terminal_present_value
    ) / (1 + required_return)
    duration_valid = (
        (forward_per > 0)
        & (required_return > 0)
        & (required_return < 1)
        & (implied_growth > -1)
        & (terminal_present_value >= 0)
        & (modified_duration > 0)
        & (modified_duration <= EQUITY_DURATION_HORIZON_YEARS)
        & np.isfinite(modified_duration)
    )
    df["equity_duration_20y"] = modified_duration.where(duration_valid)

    rim_denominator = 1 - decay + required_return
    rim_target_value = book_value_per_share + (
        book_value_per_share
        * (rim_roe - required_return)
        * decay
        / rim_denominator
    )
    rim_upside = rim_target_value / price - 1
    rim_valid = (
        book_value_per_share.notna()
        & price.notna()
        & rim_roe.notna()
        & (required_return > 0)
        & (required_return < 1)
        & (rim_denominator > 0)
        & (rim_target_value > 0)
        & np.isfinite(rim_upside)
    )
    df["rim_upside_potential"] = rim_upside.where(rim_valid)
    return df


def add_rim_historical_roe_fallback(
    daily_df,
    financial_df,
    *,
    required_years=RIM_HISTORICAL_ROE_YEARS,
    periods_per_year=1,
):
    """Merge a point-in-time average of consecutive disclosed ROE observations."""

    required_years = int(required_years)
    if required_years < 1:
        raise ValueError("required_years must be at least 1")
    periods_per_year = int(periods_per_year)
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be at least 1")
    required_periods = required_years * periods_per_year

    df = daily_df.sort_values("trade_date").copy()
    df["historical_roe_3y_avg"] = math.nan
    if df.empty or financial_df is None or financial_df.empty:
        return df

    history = financial_df.copy()
    if "financial_period" not in history.columns or "roe" not in history.columns:
        return df
    history["financial_period"] = pd.to_datetime(
        history["financial_period"],
        errors="coerce",
    )
    if "report_date" in history.columns:
        history["report_date"] = pd.to_datetime(history["report_date"], errors="coerce")
    else:
        history["report_date"] = history["financial_period"]
    history["report_date"] = history["report_date"].fillna(history["financial_period"])
    if "fiscal_year" in history.columns:
        history["fiscal_year"] = pd.to_numeric(
            history["fiscal_year"],
            errors="coerce",
        ).fillna(history["financial_period"].dt.year)
    else:
        history["fiscal_year"] = history["financial_period"].dt.year
    if "fiscal_month" in history.columns:
        history["fiscal_month"] = pd.to_numeric(
            history["fiscal_month"],
            errors="coerce",
        ).fillna(history["financial_period"].dt.month)
    else:
        history["fiscal_month"] = history["financial_period"].dt.month
    history["roe"] = pd.to_numeric(history["roe"], errors="coerce")
    history["roe"] = history["roe"].where(np.isfinite(history["roe"]))
    history = history.dropna(
        subset=["financial_period", "report_date", "fiscal_year", "fiscal_month"]
    )
    if history.empty:
        return df

    history["fiscal_year"] = history["fiscal_year"].astype(int)
    history["fiscal_month"] = history["fiscal_month"].astype(int)
    history = (
        history.sort_values(["financial_period", "report_date"])
        .drop_duplicates(["fiscal_year", "fiscal_month"], keep="last")
        .reset_index(drop=True)
    )
    if periods_per_year == 1:
        history = (
            history.sort_values(["fiscal_year", "report_date"])
            .drop_duplicates("fiscal_year", keep="last")
            .reset_index(drop=True)
        )
        period_sequence = history["fiscal_year"]
    else:
        quarter = (history["fiscal_month"] - 1) // 3 + 1
        period_sequence = history["fiscal_year"] * periods_per_year + quarter

    rolling_mean = history["roe"].rolling(
        required_periods,
        min_periods=required_periods,
    ).mean()
    consecutive_periods = pd.Series(True, index=history.index)
    for offset in range(1, required_periods):
        consecutive_periods &= period_sequence.eq(period_sequence.shift(offset) + offset)
    history["historical_roe_3y_avg"] = rolling_mean.where(consecutive_periods)

    events = history.dropna(subset=["historical_roe_3y_avg"]).copy()
    if events.empty:
        return df
    events = (
        events.sort_values(["report_date", "financial_period"])
        .drop_duplicates("report_date", keep="last")
    )

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["_rim_roe_row_id"] = np.arange(len(df))
    merged = pd.merge_asof(
        df[["_rim_roe_row_id", "trade_date"]].sort_values("trade_date"),
        events[["report_date", "historical_roe_3y_avg"]].sort_values("report_date"),
        left_on="trade_date",
        right_on="report_date",
        direction="backward",
    )
    values = merged.set_index("_rim_roe_row_id")["historical_roe_3y_avg"]
    df["historical_roe_3y_avg"] = df["_rim_roe_row_id"].map(values)
    return df.drop(columns=["_rim_roe_row_id"])


def _read_optional_wacc_csv(path):
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    return _drop_unnamed_columns(pd.read_csv(path))


def _tax_rate_ratio(series):
    rate = pd.to_numeric(series, errors="coerce")
    return rate.where(rate <= 1, rate / 100).where(lambda value: (value >= 0) & (value <= 1))


def _daily_beta_series(df, *, market, default_beta, benchmark_weekly_returns):
    result = pd.Series(default_beta, index=df.index, dtype="float64")
    if df.empty or benchmark_weekly_returns is None or benchmark_weekly_returns.empty:
        return result
    price_columns = [column for column in ["security_id", "trade_date", "adj_close", "close"] if column in df.columns]
    if "trade_date" not in price_columns or "security_id" not in price_columns:
        return result
    stock_weekly = normalize_weekly_returns_from_prices(df[price_columns])
    benchmark_weekly = _select_benchmark_weekly_returns(benchmark_weekly_returns, market)
    beta_history = calculate_rolling_beta(stock_weekly, benchmark_weekly)
    if beta_history.empty:
        return result
    left = pd.DataFrame({"trade_date": pd.to_datetime(df["trade_date"], errors="coerce")}, index=df.index)
    beta_history = beta_history.copy()
    beta_history["week_end_date"] = pd.to_datetime(beta_history["week_end_date"], errors="coerce")
    merged = pd.merge_asof(
        left.sort_values("trade_date"),
        beta_history[["week_end_date", "beta"]].sort_values("week_end_date"),
        left_on="trade_date",
        right_on="week_end_date",
        direction="backward",
    )
    merged.index = left.sort_values("trade_date").index
    return pd.to_numeric(merged["beta"].reindex(df.index), errors="coerce").fillna(default_beta)


def _select_benchmark_weekly_returns(frame, market):
    rows = frame.copy()
    if "market" in rows.columns:
        rows = rows.loc[rows["market"].astype(str).str.lower() == str(market).lower()].copy()
    if rows.empty:
        return pd.DataFrame()
    if "benchmark_id" in rows.columns:
        preference = ["KOSPI200", "KOSPI", "KOSDAQ"] if str(market).lower() == "kr" else ["US_SP500", "SP500", "^GSPC"]
        ranks = {benchmark_id: index for index, benchmark_id in enumerate(preference)}
        rows["_rank"] = rows["benchmark_id"].astype(str).str.upper().map(ranks).fillna(len(ranks))
        rows = rows.sort_values(["_rank", "week_end_date"])
        rows = rows.loc[rows["_rank"] == rows["_rank"].min()].copy()
    return rows


def create_stock_factor_dataframe(
    stock_code,
    start_date=None,
    end_date=None,
    price_path=None,
    shares_path=None,
    dividend_path=None,
    financial_basis="annual",
    cumulative_statement_types=None,
    report_metadata_path=REPORT_METADATA_PATH,
    market="kr",
    financial_dir=None,
    market_data_cache=None,
    use_edgartools=True,
    edgartools_provider=None,
    wacc_risk_free_path=None,
    wacc_erp_path=None,
    wacc_assumptions_path=None,
    wacc_benchmark_path=None,
    wacc_online_backfill=False,
    estimate_gold_root=ESTIMATE_GOLD_ROOT,
    real_consensus_daily_path=HANKYUNG_CONSENSUS_DAILY_PATH,
    target_price_consensus_path=HANKYUNG_TARGET_PRICE_CONSENSUS_PATH,
    us_consensus_factors_path=US_CONSENSUS_FACTORS_PATH,
    rim_decay_factor=DEFAULT_RIM_DECAY_FACTOR,
):
    market = str(market or "kr").strip().lower()
    stock_code = normalize_symbol_for_market(stock_code, market)
    if financial_dir is None:
        financial_dir = financial_dir_for_market(market)
    if report_metadata_path == REPORT_METADATA_PATH and market == "us":
        report_metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
    output_start_date = pd.Timestamp(start_date) if start_date is not None else None
    output_end_date = pd.Timestamp(end_date) if end_date is not None else None
    security_id = security_id_for_market(stock_code, market)
    if market_data_cache is not None:
        price_df = market_data_cache.prices(security_id, stock_code=stock_code)
    else:
        price_df = read_stock_prices(stock_code, price_path, market=market)

    if price_df.empty:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Seoul")).date())
    price_df = price_df.loc[price_df["trade_date"] <= today].copy()
    if output_end_date is not None:
        price_df = price_df.loc[price_df["trade_date"] <= output_end_date].copy()
    if price_df.empty:
        return pd.DataFrame()

    if market_data_cache is not None:
        shares_df = market_data_cache.shares(security_id)
    else:
        shares_df = read_stock_shares(stock_code, shares_path, market=market)

    if financial_basis == "quarterly":
        financial_df = read_quarterly_financials(
            stock_code,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            financial_dir=financial_dir,
            market=market,
            use_edgartools=use_edgartools,
            edgartools_provider=edgartools_provider,
        )
    elif financial_basis == "ttm":
        financial_df = read_ttm_financials(
            stock_code,
            cumulative_statement_types=cumulative_statement_types,
            report_metadata_path=report_metadata_path,
            financial_dir=financial_dir,
            market=market,
            use_edgartools=use_edgartools,
            edgartools_provider=edgartools_provider,
        )
    elif financial_basis == "annual":
        financial_df = read_annual_financials(
            stock_code,
            report_metadata_path=report_metadata_path,
            financial_dir=financial_dir,
            market=market,
            use_edgartools=use_edgartools,
            edgartools_provider=edgartools_provider,
        )
    else:
        raise ValueError("financial_basis must be 'annual', 'ttm', or 'quarterly'")

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

    if "market_cap" in daily_df.columns and "shares" in daily_df.columns:
        derived_market_cap = pd.to_numeric(daily_df["close"], errors="coerce") * pd.to_numeric(
            daily_df["shares"],
            errors="coerce",
        )
        daily_df["market_cap"] = pd.to_numeric(daily_df["market_cap"], errors="coerce").fillna(
            derived_market_cap,
        )

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
    daily_df = add_dividend_factors(
        daily_df,
        stock_code,
        market=market,
        market_data_cache=market_data_cache,
    )
    daily_df = add_daily_market_valuation_factors(daily_df)
    daily_df = add_consensus_factors(
        daily_df,
        financial_df,
        stock_code,
        estimate_gold_root=estimate_gold_root,
        market=market,
    )
    daily_df = add_real_consensus_factors(
        daily_df,
        financial_df,
        stock_code,
        real_consensus_daily_path=real_consensus_daily_path,
        market=market,
    )
    daily_df = add_kr_target_price_factor(
        daily_df,
        stock_code,
        target_price_consensus_path=target_price_consensus_path,
        market=market,
    )
    daily_df = add_us_consensus_factors(
        daily_df,
        stock_code,
        us_consensus_factors_path=us_consensus_factors_path,
        market=market,
    )
    daily_df = add_eps_implied_operating_income_surprise_factor(
        daily_df,
        market=market,
    )
    if market in {"kr", "us"} and financial_basis == "annual":
        daily_df = add_rim_historical_roe_fallback(daily_df, financial_df)
    elif market == "us" and financial_basis == "ttm":
        daily_df = add_rim_historical_roe_fallback(
            daily_df,
            financial_df,
            periods_per_year=4,
        )
    daily_df = add_wacc_factors(
        daily_df,
        market=market,
        stock_code=stock_code,
        market_data_cache=market_data_cache,
        wacc_risk_free_path=wacc_risk_free_path,
        wacc_erp_path=wacc_erp_path,
        wacc_assumptions_path=wacc_assumptions_path,
        wacc_benchmark_path=wacc_benchmark_path,
    )
    daily_df = add_pvgo_factors(daily_df)
    daily_df = add_equity_valuation_factors(
        daily_df,
        rim_decay_factor=rim_decay_factor,
        market=market,
    )
    daily_df = add_price_momentum_factors(daily_df)
    daily_df["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    daily_df["currency"] = market_config(market).currency

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
        "knowledge_capital",
        "organization_capital",
        "intangible_capital",
        "intangible_investment",
        "intangible_amortization",
        "net_intangible_investment",
        "intangible_adjusted_net_income",
        "xint",
        "oancf",
        "capx",
        "fcf",
        "fcff",
        "fcfe",
        "ffo",
        "div_paid",
        "dvpsp",
        "dvpsx",
        "sstk",
        "prstkc",
        "net_borrowing",
        "eps",
        "bps",
        "sps",
        "cps",
        "csho",
        "mcap_mil",
        "enterprise_value",
        "beta",
        "cost_of_equity",
        "equity_duration_20y",
        "cost_of_debt_pre_tax",
        "cost_of_debt_after_tax",
        "wacc_equity_weight",
        "wacc_debt_weight",
        "wacc",
        "rnd_to_market_cap",
        "gross_profitability_pct",
        "percent_total_accruals_pct",
        "book_equity_growth_1y_pct",
        "current_operating_assets_change_pct",
        "capex_growth_2y_pct",
        "net_debt_financing_pct",
        "gpm",
        "opm",
        "operating_profit_margin",
        "ebitda_margin",
        "npm",
        "net_margin",
        "fcf_margin",
        "fcf_margin_growth_1y",
        "fcf_payout_ratio",
        "fcf_dividend_coverage",
        "fcf_after_dividends",
        "fcf_after_dividends_to_sales_pct",
        "fcf_after_dividends_to_assets_pct",
        "fcf_after_dividends_to_market_cap_pct",
        "shareholder_return_fcf_coverage",
        "fcfe_dividend_coverage",
        "fcfe_payout_ratio",
        "fcf_yield",
        "fcf_to_ev_yield",
        "fcf_yield_dividend_yield_spread",
        "fcf_volatility_5y",
        "fcf_negative_freq_5y_pct",
        "fcf_volatility_10y",
        "fcf_negative_freq_10y_pct",
        "capex_to_sales_pct",
        "capex_to_cfo_pct",
        "net_debt_to_fcf",
        "interest_expense_to_fcf_pct",
        "fcf_interest_coverage",
        "rnd_margin",
        "tax_rate",
        "nopat",
        "normalized_operating_margin_5y",
        "normalized_nopat_5y",
        "normalized_earnings_5y",
        "normalized_intangible_adjusted_earnings_5y",
        "normalized_nopat_growth_3y_pct",
        "roe",
        "avg_parent_equity",
        "roe_growth_1y",
        "roe_growth_3y",
        "roe_growth_5y",
        "roa",
        "accrual_ratio",
        "inventory_growth_1y_pct",
        "net_external_financing_pct",
        "iroe",
        "intangible_adjusted_equity",
        "avg_intangible_adjusted_equity",
        "intangible_adjusted_roe_pct",
        "intangible_adjusted_roe_spread_pct",
        "roic_financial",
        "roic_operational",
        "roic_operational_growth_1y",
        "incremental_investment_rate_pct",
        "roiic_pct",
        "roiic_wacc_spread",
        "roic_wacc_spread",
        "economic_profit",
        "economic_profit_yield",
        "delta_economic_profit",
        "roic_wacc_spread_growth_1y",
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
        "operating_margin_growth_1y",
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
        "eps_expected_growth",
        "revenue_expected_growth",
        "operating_income_expected_growth",
        "net_income_expected_growth",
        "eps_surprise_pct",
        "revenue_surprise_pct",
        "operating_income_surprise_pct",
        "net_income_surprise_pct",
        "real_eps_revision_1m_pct",
        "real_eps_expected_growth",
        "real_revenue_expected_growth",
        "real_operating_income_expected_growth",
        "real_net_income_expected_growth",
        "real_eps_surprise_pct",
        "real_revenue_surprise_pct",
        "real_operating_income_surprise_pct",
        "real_net_income_surprise_pct",
        KR_PRICE_TO_TARGET_PRICE_FACTOR,
        "us_eps_consensus",
        "us_revenue_consensus",
        "us_eps_revision_7d_pct",
        "us_eps_revision_30d_pct",
        "us_eps_revision_60d_pct",
        "us_eps_revision_90d_pct",
        "us_eps_revision_breadth_30d_pct",
        "us_eps_revision_acceleration_30d_pct",
        "us_eps_dispersion_pct",
        "us_revenue_dispersion_pct",
        "us_eps_surprise_pct",
        US_PRICE_TO_TARGET_PRICE_FACTOR,
        EPS_IMPLIED_OPERATING_INCOME_SURPRISE_FACTOR,
        "us_consensus_analyst_count",
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
        "pvgo_pct",
        "pvgo_ev_pct",
        "pvgo_expectation_factor",
        "normalized_pvgo_pct",
        "equity_pvgo_pct",
        "justified_pvgo_pct",
        "pvgo_gap_pct",
        "pvgo_compression_pct",
        "pvgo_change_1y_pctp",
        "intangible_adjusted_eps",
        "normalized_intangible_adjusted_eps",
        "intangible_adjusted_pvgo_pct",
        "normalized_intangible_adjusted_pvgo_pct",
        "intangible_adjusted_pvgo_gap_pct",
        "intangible_adjusted_pvgo_compression_pct",
        "intangible_adjusted_pvgo_change_1y_pctp",
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
        "k_ratio_3y",
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
        "shareholder_yield",
        "tdpr",
        "forward_dividend_yield",
        "earnings_payout_ratio",
        "eps_dividend_coverage",
        "dps_yoy_pct",
        "dps_cagr_3y",
        "dps_cagr_5y",
        "dps_cagr_10y",
        "dividend_consistency_streak",
        "dividend_growth_streak",
        "dps_volatility_5y",
        "dps_volatility_10y",
        "dividend_cut",
        "dividend_change_momentum",
        "special_dividend",
        "special_dividend_ratio_pct",
        "per",
        "rim_upside_potential",
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
        valid = coverage_valid_series(df[column])
        covered_count = int(valid.sum())
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
    covered_cells = int(sum(coverage_valid_series(df[column]).sum() for column in target_columns))
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


def coverage_valid_series(series):
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.notna() & pd.Series(
        [math.isfinite(value) for value in numeric],
        index=series.index,
    )


def create_stock_factor_coverage(stock_code, **kwargs):
    factor_df = create_stock_factor_dataframe(stock_code, **kwargs)
    return calculate_factor_coverage(factor_df)


def create_all_stock_factor_coverage(stock_codes=None, **kwargs):
    factor_df = create_all_stock_factor_dataframe(stock_codes=stock_codes, **kwargs)
    return calculate_factor_coverage(factor_df)
