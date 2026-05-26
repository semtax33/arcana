from __future__ import annotations

import argparse
from datetime import datetime
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE, parse_statement_snapshot_filename, parse_statement_symbol_filename
from engine.transformers.factors import (
    FactorMarketDataCache,
    create_stock_factor_dataframe,
    factor_columns,
    normalize_stock_code,
    preferred_factor_columns,
    resolve_price_path,
)


FACT_DAILY_FACTOR_COLUMNS = [
    "security_id",
    "trade_date",
    "factor_id",
    "financial_basis",
    "factor_value",
    "fiscal_year",
    "financial_period",
    "currency",
    "updated_at",
]

FACTOR_CATALOG_COLUMNS = [
    "factor_id",
    "factor_name",
    "factor_type",
    "factor_group",
    "unit",
    "value_direction",
    "description",
    "is_active",
    "created_at",
    "updated_at",
]

TECHNICAL_FACTORS = {
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
}

NEUTRAL_TECHNICAL_FACTORS = {
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
}

VALUATION_FACTORS = {
    "mcap_mil",
    "rnd_to_market_cap",
    "epr",
    "bpr",
    "tpr",
    "spr",
    "cpr",
    "fcfpr",
    "fcf_yield",
    "fcf_to_ev_yield",
    "fcf_yield_dividend_yield_spread",
    "npr",
    "rpr",
    "ebitda_to_ev",
    "ev_to_ebitda",
    "ev_to_nopat",
    "per",
    "pbr",
    "pcr",
    "psr",
    "peg",
}

QUALITY_FACTORS = {
    "fcf_margin",
    "fcf_dividend_coverage",
    "fcf_after_dividends",
    "fcf_after_dividends_to_sales_pct",
    "fcf_after_dividends_to_assets_pct",
    "fcf_after_dividends_to_market_cap_pct",
    "fcfe_dividend_coverage",
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
    "roa",
    "iroe",
    "roic_financial",
    "roic_operational",
    "asset_turnover",
    "total_asset_turnover",
    "receivables_turnover",
    "inventory_turnover",
    "working_capital_turnover",
    "roce",
    "f_score",
    "beneish_m_score",
}

GROWTH_FACTORS = {
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
    "eps_yoy_pct",
    "asset_yoy_pct",
    "cfo_yoy_pct",
    "fcf_yoy_pct",
    "ffo_yoy_pct",
}

RISK_FACTORS = {
    "net_debt_to_ebitda",
    "net_debt_to_ocf",
    "net_debt_to_fcf",
    "interest_expense_to_fcf_pct",
    "fcf_interest_coverage",
    "fcf_volatility_5y",
    "fcf_negative_freq_5y_pct",
    "fcf_volatility_10y",
    "fcf_negative_freq_10y_pct",
    "capex_to_sales_pct",
    "capex_to_cfo_pct",
    "fc_to_ndr",
    "icr_times",
    "interest_coverage",
    "current_ratio",
    "debt_to_equity",
    "cash_to_debt",
    "total_interest_coverage",
    "debt_ratio",
    "altman_z_score",
}

SHAREHOLDER_FACTORS = {
    "sharehold_div_yield",
    "sharehold_net_buyback_yield",
    "sharehold_return",
    "shareholder_yield",
    "shareholder_return_fcf_coverage",
    "tdpr",
    "dividend_yield",
    "forward_dividend_yield",
    "payout_ratio",
    "earnings_payout_ratio",
    "fcf_payout_ratio",
    "fcfe_payout_ratio",
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
    "dvpsp",
    "dvpsx",
}

FUNDAMENTAL_AMOUNT_FACTORS = {
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
    "div_paid",
    "fcf_after_dividends",
    "sstk",
    "prstkc",
    "net_borrowing",
    "nopat",
    "working_capital",
}

FACTOR_NAME_OVERRIDES = {
    "rnd_margin": "R&D Margin",
    "fcf_margin": "FCF Margin",
    "fcf_payout_ratio": "FCF Payout Ratio",
    "fcf_dividend_coverage": "FCF Dividend Coverage",
    "fcf_after_dividends": "FCF After Dividends",
    "fcf_yield": "FCF Yield",
    "fcf_to_ev_yield": "FCF / EV Yield",
    "fcf_yield_dividend_yield_spread": "FCF Yield - Dividend Yield",
    "fcfe_dividend_coverage": "FCFE Dividend Coverage",
    "fcfe_payout_ratio": "FCFE Payout Ratio",
    "capex_to_sales_pct": "Capex / Sales",
    "capex_to_cfo_pct": "Capex / CFO",
    "net_debt_to_fcf": "Net Debt / FCF",
    "fcf_interest_coverage": "FCF Interest Coverage",
    "eps_dividend_coverage": "EPS Dividend Coverage",
    "shareholder_yield": "Shareholder Yield",
    "sales_cagr_3y": "Sales CAGR 3Y",
    "rnd_to_sales": "R&D / Sales",
    "operating_profit_margin": "Operating Profit Margin",
    "net_income_growth_1y": "Net Income Growth 1Y",
    "net_income_growth_3y": "Net Income Growth 3Y",
    "net_income_growth_5y": "Net Income Growth 5Y",
    "operating_income_growth_1y": "Operating Income Growth 1Y",
    "operating_income_growth_3y": "Operating Income Growth 3Y",
    "operating_income_growth_5y": "Operating Income Growth 5Y",
    "sales_growth_1y": "Sales Growth 1Y",
    "sales_growth_3y": "Sales Growth 3Y",
    "sales_growth_5y": "Sales Growth 5Y",
    "net_margin": "Net Margin",
    "total_asset_turnover": "Total Asset Turnover",
    "rnd_to_market_cap": "R&D / Market Cap",
}

LOWER_IS_BETTER = {
    "per",
    "pbr",
    "pcr",
    "psr",
    "peg",
    "ev_to_ebitda",
    "ev_to_nopat",
    "inv_days",
    "ar_days",
    "ap_days",
    "ccc",
    "vol_12_1_ann",
    "mdd1yr_12_1_pct",
    "net_debt_to_ebitda",
    "net_debt_to_ocf",
    "net_debt_to_fcf",
    "interest_expense_to_fcf_pct",
    "fcf_volatility_5y",
    "fcf_negative_freq_5y_pct",
    "fcf_volatility_10y",
    "fcf_negative_freq_10y_pct",
    "capex_to_sales_pct",
    "capex_to_cfo_pct",
    "fcf_payout_ratio",
    "fcfe_payout_ratio",
    "dps_volatility_5y",
    "dps_volatility_10y",
    "dividend_cut",
    "special_dividend_ratio_pct",
    "debt_to_equity",
    "debt_ratio",
    "beneish_m_score",
}

HIGHER_IS_BETTER = (
    VALUATION_FACTORS
    | QUALITY_FACTORS
    | GROWTH_FACTORS
    | RISK_FACTORS
    | SHAREHOLDER_FACTORS
    | (TECHNICAL_FACTORS - NEUTRAL_TECHNICAL_FACTORS)
    | {
        "eps",
        "bps",
        "sps",
        "cps",
        "csho",
        "fc_to_ndr",
        "icr_times",
        "interest_coverage",
        "current_ratio",
        "cash_to_debt",
        "altman_z_score",
    }
) - LOWER_IS_BETTER


def empty_daily_factor_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=FACT_DAILY_FACTOR_COLUMNS)


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = df.copy()
    for column in columns:
        if column not in result.columns:
            result[column] = pd.NA
    return result


def _as_clickhouse_date(series: pd.Series) -> pd.Series:
    dates = pd.to_datetime(series, errors="coerce").dt.date.astype("object")
    return dates.where(pd.notna(dates), None)


def prepare_daily_factor_rows(
    wide_df: pd.DataFrame,
    *,
    financial_basis: str = "annual",
    factor_ids: list[str] | None = None,
    sort_rows: bool = True,
) -> pd.DataFrame:
    if wide_df.empty:
        return empty_daily_factor_rows()

    required_columns = {"security_id", "trade_date"}
    missing_columns = required_columns - set(wide_df.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"wide_df is missing required columns: {missing}")

    id_columns = [
        "security_id",
        "trade_date",
        "fiscal_year",
        "financial_period",
        "currency",
        "updated_at",
    ]
    wide_df = _ensure_columns(wide_df, id_columns)

    value_columns = factor_ids if factor_ids is not None else factor_columns(wide_df)
    value_columns = [column for column in value_columns if column in wide_df.columns]
    if not value_columns:
        return empty_daily_factor_rows()

    long_parts = []
    id_frame = wide_df[id_columns]
    for factor_id in value_columns:
        factor_value = pd.to_numeric(wide_df[factor_id], errors="coerce")
        valid_mask = factor_value.notna() & factor_value.map(math.isfinite)
        if not valid_mask.any():
            continue

        part = id_frame.loc[valid_mask].copy()
        part["factor_id"] = factor_id
        part["factor_value"] = factor_value.loc[valid_mask].to_numpy()
        long_parts.append(part)

    if not long_parts:
        return empty_daily_factor_rows()

    long_df = pd.concat(long_parts, ignore_index=True)
    long_df["financial_basis"] = financial_basis
    long_df["trade_date"] = _as_clickhouse_date(long_df["trade_date"])
    long_df["financial_period"] = _as_clickhouse_date(long_df["financial_period"])
    fiscal_year = pd.to_numeric(long_df["fiscal_year"], errors="coerce").astype("Int64")
    long_df["fiscal_year"] = fiscal_year.astype("object").where(fiscal_year.notna(), None)
    long_df["currency"] = long_df["currency"].fillna("KRW")
    long_df["updated_at"] = pd.to_datetime(long_df["updated_at"], errors="coerce")
    long_df["updated_at"] = long_df["updated_at"].fillna(
        datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    )

    long_df = long_df[FACT_DAILY_FACTOR_COLUMNS]
    if sort_rows:
        long_df = long_df.sort_values(
            ["trade_date", "factor_id", "financial_basis", "security_id"]
        )
    return long_df.reset_index(drop=True)


def create_daily_factor_rows(
    stock_codes: list[str] | None = None,
    *,
    financial_basis: str = "annual",
    start_date: str | None = None,
    end_date: str | None = None,
    market: str = "kr",
    reader_mode: str = "cached",
    **kwargs,
) -> pd.DataFrame:
    reader_mode = str(reader_mode or "cached").strip().lower()
    if reader_mode not in {"cached", "csv"}:
        raise ValueError("reader_mode must be 'cached' or 'csv'")

    market_data_cache = kwargs.pop("market_data_cache", None)
    if reader_mode == "cached" and market_data_cache is None:
        market_data_cache = FactorMarketDataCache(
            market=market,
            price_path=kwargs.get("price_path"),
            shares_path=kwargs.get("shares_path"),
            dividend_path=kwargs.get("dividend_path"),
            start_date=start_date,
            end_date=end_date,
        )

    frames = []
    for stock_code in _resolve_stock_codes(stock_codes, market=market):
        wide_df = create_stock_factor_dataframe(
            stock_code,
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
            market=market,
            market_data_cache=market_data_cache,
            **kwargs,
        )
        factor_df = prepare_daily_factor_rows(
            wide_df,
            financial_basis=financial_basis,
        )
        if not factor_df.empty:
            frames.append(factor_df)

    if not frames:
        return empty_daily_factor_rows()

    return pd.concat(frames, ignore_index=True).sort_values(
        ["trade_date", "factor_id", "financial_basis", "security_id"]
    ).reset_index(drop=True)


def _resolve_stock_codes(stock_codes: list[str] | None, market: str = "kr") -> list[str]:
    market = str(market or "kr").strip().lower()
    if stock_codes is not None:
        if market == "kr":
            return [normalize_stock_code(stock_code) for stock_code in stock_codes]
        return [str(stock_code).strip().upper() for stock_code in stock_codes]

    if market != "kr":
        financial_dir = DATA_LAKE.silver("sec", "normalized") if market == "us" else DATA_LAKE.silver(market, "normalized")
        symbols = set()
        if financial_dir.exists():
            for path in financial_dir.glob(f"{market}_normalized_*.csv"):
                if ".debug" in path.name:
                    continue
                symbol_meta = parse_statement_symbol_filename(path)
                if symbol_meta is not None:
                    symbols.add(str(symbol_meta["stock_code"]))
                    continue
                snapshot_meta = parse_statement_snapshot_filename(path)
                if snapshot_meta is not None:
                    symbols.add(str(snapshot_meta["stock_code"]))
        return sorted(symbols)

    from engine.extractors.market_universe import kospi_kosdaq_corp_list

    corps_list = kospi_kosdaq_corp_list()
    return sorted(corps_list["stock_code"].dropna().map(normalize_stock_code).unique())


def _insert_daily_factor_rows_by_partition(client, factor_df: pd.DataFrame) -> int:
    if factor_df.empty:
        return 0

    inserted_count = 0
    factor_df = factor_df.copy()
    factor_df["_partition"] = pd.to_datetime(factor_df["trade_date"]).dt.strftime("%Y%m")
    for partition, chunk in factor_df.groupby("_partition", sort=True):
        chunk = chunk.drop(columns=["_partition"]).copy()
        client.insert_df(
            "fact_daily_factors",
            chunk,
            column_names=list(chunk.columns),
        )
        inserted_count += len(chunk)
        print(f"inserted partition={partition}, rows={len(chunk):,}", flush=True)

    return inserted_count


def _flush_daily_factor_batch(
    client,
    batch_frames: list[pd.DataFrame],
    *,
    batch_index: int,
) -> int:
    if not batch_frames:
        return 0

    batch_df = pd.concat(batch_frames, ignore_index=True)
    print(
        f"flushing batch={batch_index}, "
        f"stocks={len(batch_frames):,}, rows={len(batch_df):,}",
        flush=True,
    )
    return _insert_daily_factor_rows_by_partition(client, batch_df)


def _validate_required_price_data(
    *,
    market: str,
    reader_mode: str,
    market_data_cache,
    price_path,
) -> None:
    resolved_price_path = None
    if market_data_cache is not None and hasattr(market_data_cache, "price_path"):
        resolved_price_path = Path(market_data_cache.price_path)
    elif price_path is not None:
        resolved_price_path = Path(price_path)
    elif reader_mode in {"cached", "csv"}:
        resolved_price_path = resolve_price_path(market=market)

    if resolved_price_path is None or resolved_price_path.exists():
        return

    raise FileNotFoundError(
        "price data is required before loading daily factors: "
        f"market={market}, expected_path={resolved_price_path}. "
        "Run the market data loader first or pass --price-path to an existing normalized price CSV."
    )


def _should_log_progress(stock_index: int, total_stocks: int, progress_interval: int) -> bool:
    if stock_index in {1, total_stocks}:
        return True
    return progress_interval > 0 and stock_index % progress_interval == 0


def create_factor_catalog_dataframe(factor_ids: list[str] | None = None) -> pd.DataFrame:
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    factor_ids = factor_ids if factor_ids is not None else preferred_factor_columns()
    rows = []

    for factor_id in factor_ids:
        rows.append(
            {
                "factor_id": factor_id,
                "factor_name": FACTOR_NAME_OVERRIDES.get(
                    factor_id,
                    factor_id.replace("_", " ").upper(),
                ),
                "factor_type": infer_factor_type(factor_id),
                "factor_group": infer_factor_group(factor_id),
                "unit": infer_factor_unit(factor_id),
                "value_direction": infer_value_direction(factor_id),
                "description": "",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )

    return pd.DataFrame(rows, columns=FACTOR_CATALOG_COLUMNS)


def infer_factor_type(factor_id: str) -> str:
    if factor_id in TECHNICAL_FACTORS:
        return "technical"
    if factor_id in VALUATION_FACTORS:
        return "valuation"
    if factor_id in SHAREHOLDER_FACTORS:
        return "shareholder"
    if factor_id in RISK_FACTORS:
        return "risk"
    if factor_id in GROWTH_FACTORS:
        return "growth"
    if factor_id in QUALITY_FACTORS:
        return "quality"
    return "fundamental"


def infer_factor_group(factor_id: str) -> str:
    factor_type = infer_factor_type(factor_id)
    if factor_id in {"na_5", "na_20", "na_50", "na_150", "na_200", "ma_50", "ma_120", "ma_150", "ma_200"}:
        return "trend"
    if factor_id in {"rsi_14", "macd", "macd_signal", "macd_hist", "williams_r_14", "mfi_14"}:
        return "momentum"
    if factor_id in {"ati", "cmf_20"}:
        return "volume"
    if factor_id.startswith("bb_"):
        return "volatility"
    if factor_id in {"vol_12_1_ann", "mdd1yr_12_1_pct"}:
        return "risk"
    if factor_id == "adturn_pct_12_1":
        return "liquidity"
    if factor_id in FUNDAMENTAL_AMOUNT_FACTORS:
        return "amount"
    return factor_type


def infer_factor_unit(factor_id: str) -> str:
    if factor_id.endswith("_pct") or factor_id in {
        "fcf_margin",
        "gpm",
        "net_margin",
        "opm",
        "operating_profit_margin",
        "ebitda_margin",
        "npm",
        "rnd_margin",
        "rnd_to_market_cap",
        "rnd_to_sales",
        "tax_rate",
        "roe",
        "roic_financial",
        "roic_operational",
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
        "sharehold_div_yield",
        "sharehold_net_buyback_yield",
        "sharehold_return",
        "shareholder_yield",
        "dividend_yield",
        "forward_dividend_yield",
        "payout_ratio",
        "earnings_payout_ratio",
        "fcf_payout_ratio",
        "fcfe_payout_ratio",
        "fcf_yield",
        "fcf_to_ev_yield",
        "fcf_yield_dividend_yield_spread",
        "fcf_after_dividends_to_sales_pct",
        "fcf_after_dividends_to_assets_pct",
        "fcf_after_dividends_to_market_cap_pct",
        "fcf_negative_freq_5y_pct",
        "fcf_negative_freq_10y_pct",
        "capex_to_sales_pct",
        "capex_to_cfo_pct",
        "interest_expense_to_fcf_pct",
        "dps_yoy_pct",
        "dps_cagr_3y",
        "dps_cagr_5y",
        "dps_cagr_10y",
        "dividend_change_momentum",
        "special_dividend_ratio_pct",
        "tdpr",
        "rsi_14",
        "williams_r_14",
        "mfi_14",
    }:
        return "percent"
    if factor_id == "ati":
        return "shares"
    if factor_id.endswith("_score") or factor_id == "f_score":
        return "score"
    if factor_id.endswith("_times") or factor_id in {
        "per",
        "pbr",
        "pcr",
        "psr",
        "peg",
        "asset_turnover",
        "total_asset_turnover",
        "receivables_turnover",
        "inventory_turnover",
        "working_capital_turnover",
        "fcf_dividend_coverage",
        "shareholder_return_fcf_coverage",
        "fcfe_dividend_coverage",
        "eps_dividend_coverage",
        "net_debt_to_fcf",
        "fcf_interest_coverage",
    }:
        return "times"
    if factor_id in {"dividend_consistency_streak", "dividend_growth_streak"}:
        return "years"
    if factor_id in {"dividend_cut", "special_dividend"}:
        return "flag"
    if factor_id in {"shares", "csho"}:
        return "shares"
    if factor_id in {"inv_days", "ar_days", "ap_days", "ccc"}:
        return "days"
    if factor_id in {"bb_upper", "bb_middle", "bb_lower", "macd", "macd_signal", "macd_hist"}:
        return "krw"
    if factor_id.startswith(("na_", "ma_")) or factor_id in FUNDAMENTAL_AMOUNT_FACTORS or factor_id in {
        "eps",
        "bps",
        "sps",
        "cps",
        "dvpsp",
        "dvpsx",
        "mcap_mil",
        "sales_change_mil",
        "op_change_mil",
        "div_paid",
        "net_borrowing",
        "fcf_after_dividends",
        "fcf_volatility_5y",
        "fcf_volatility_10y",
        "dps_volatility_5y",
        "dps_volatility_10y",
    }:
        return "krw"
    return "ratio"


def infer_value_direction(factor_id: str) -> str:
    if factor_id in HIGHER_IS_BETTER:
        return "HIGHER_BETTER"
    if factor_id in LOWER_IS_BETTER:
        return "LOWER_BETTER"
    return "NEUTRAL"


def insert_factor_catalog(client=None, *, factor_ids: list[str] | None = None) -> int:
    owns_client = client is None
    client = client or get_clickhouse_client()
    catalog_df = create_factor_catalog_dataframe(factor_ids)
    if catalog_df.empty:
        return 0
    client.insert_df(
        "factor_catalog",
        catalog_df,
        column_names=list(catalog_df.columns),
    )
    if owns_client:
        client.close()
    return len(catalog_df)


def insert_daily_factors(
    stock_codes: list[str] | None = None,
    *,
    financial_basis: str = "annual",
    start_date: str | None = None,
    end_date: str | None = None,
    market: str = "kr",
    insert_catalog: bool = True,
    dry_run: bool = False,
    client=None,
    insert_batch_size: int = 25,
    insert_max_rows: int = 2_000_000,
    progress_interval: int = 25,
    reader_mode: str = "cached",
    **kwargs,
) -> pd.DataFrame:
    reader_mode = str(reader_mode or "cached").strip().lower()
    if reader_mode not in {"cached", "csv"}:
        raise ValueError("reader_mode must be 'cached' or 'csv'")

    market_data_cache = kwargs.pop("market_data_cache", None)
    if reader_mode == "cached" and market_data_cache is None:
        market_data_cache = FactorMarketDataCache(
            market=market,
            price_path=kwargs.get("price_path"),
            shares_path=kwargs.get("shares_path"),
            dividend_path=kwargs.get("dividend_path"),
            start_date=start_date,
            end_date=end_date,
        )

    _validate_required_price_data(
        market=market,
        reader_mode=reader_mode,
        market_data_cache=market_data_cache,
        price_path=kwargs.get("price_path"),
    )

    if dry_run:
        factor_df = create_daily_factor_rows(
            stock_codes=stock_codes,
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
            market=market,
            reader_mode=reader_mode,
            market_data_cache=market_data_cache,
            **kwargs,
        )
        return factor_df

    owns_client = client is None
    client = client or get_clickhouse_client()

    inserted_count = 0
    seen_factor_ids: set[str] = set()
    batch_frames: list[pd.DataFrame] = []
    batch_rows = 0
    batch_index = 1
    try:
        if insert_catalog:
            insert_factor_catalog(client, factor_ids=preferred_factor_columns())

        resolved_stock_codes = _resolve_stock_codes(stock_codes, market=market)
        total_stocks = len(resolved_stock_codes)
        print(
            "loading daily factors "
            f"market={market}, stocks={total_stocks:,}, financial_basis={financial_basis}, "
            f"start_date={start_date or '-'}, end_date={end_date or '-'}, "
            f"insert_batch_size={insert_batch_size:,}, insert_max_rows={insert_max_rows:,}",
            flush=True,
        )
        for stock_index, stock_code in enumerate(resolved_stock_codes, start=1):
            if _should_log_progress(stock_index, total_stocks, progress_interval):
                print(
                    f"processing stock={stock_code} ({stock_index:,}/{total_stocks:,}), "
                    f"current_batch_stocks={len(batch_frames):,}, current_batch_rows={batch_rows:,}, "
                    f"inserted_rows={inserted_count:,}",
                    flush=True,
                )
            wide_df = create_stock_factor_dataframe(
                stock_code,
                financial_basis=financial_basis,
                start_date=start_date,
                end_date=end_date,
                market=market,
                market_data_cache=market_data_cache,
                **kwargs,
            )
            factor_df = prepare_daily_factor_rows(
                wide_df,
                financial_basis=financial_basis,
                sort_rows=False,
            )
            if factor_df.empty:
                if _should_log_progress(stock_index, total_stocks, progress_interval):
                    print(
                        f"skipped stock={stock_code} ({stock_index:,}/{total_stocks:,}), no factor rows",
                        flush=True,
                    )
                continue

            seen_factor_ids.update(factor_df["factor_id"].unique())
            batch_frames.append(factor_df)
            batch_rows += len(factor_df)
            if _should_log_progress(stock_index, total_stocks, progress_interval):
                print(
                    f"prepared stock={stock_code} ({stock_index:,}/{total_stocks:,}), "
                    f"rows={len(factor_df):,}, current_batch_rows={batch_rows:,}",
                    flush=True,
                )

            should_flush = (
                len(batch_frames) >= insert_batch_size
                or batch_rows >= insert_max_rows
                or stock_index == len(resolved_stock_codes)
            )
            if should_flush:
                inserted_count += _flush_daily_factor_batch(
                    client,
                    batch_frames,
                    batch_index=batch_index,
                )
                batch_frames = []
                batch_rows = 0
                batch_index += 1

        if batch_frames:
            inserted_count += _flush_daily_factor_batch(
                client,
                batch_frames,
                batch_index=batch_index,
            )
    finally:
        if owns_client:
            client.close()

    result = empty_daily_factor_rows()
    result.attrs["inserted_rows"] = inserted_count
    result.attrs["factor_count"] = len(seen_factor_ids)
    return result


def _parse_stock_codes(value: str | None, market: str = "kr") -> list[str] | None:
    if value is None or not value.strip():
        return None
    if str(market or "kr").strip().lower() == "kr":
        return [item.strip().zfill(6) for item in value.split(",") if item.strip()]
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Insert daily factor rows into ClickHouse.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--stock-codes", help="Comma-separated stock codes. Defaults to all KOSPI/KOSDAQ stocks.")
    parser.add_argument("--financial-basis", default="annual", choices=["annual", "quarterly", "ttm"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--skip-catalog", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert-batch-size", type=int, default=25)
    parser.add_argument("--insert-max-rows", type=int, default=2_000_000)
    parser.add_argument("--progress-interval", type=int, default=25)
    parser.add_argument("--reader-mode", default="cached", choices=["cached", "csv"])
    parser.add_argument("--price-path")
    parser.add_argument("--shares-path")
    parser.add_argument("--dividend-path")
    args = parser.parse_args()

    factor_df = insert_daily_factors(
        stock_codes=_parse_stock_codes(args.stock_codes, market=args.market),
        financial_basis=args.financial_basis,
        start_date=args.start_date,
        end_date=args.end_date,
        market=args.market,
        insert_catalog=not args.skip_catalog,
        dry_run=args.dry_run,
        insert_batch_size=args.insert_batch_size,
        insert_max_rows=args.insert_max_rows,
        progress_interval=args.progress_interval,
        reader_mode=args.reader_mode,
        price_path=args.price_path,
        shares_path=args.shares_path,
        dividend_path=args.dividend_path,
    )
    print(
        "prepared rows="
        f"{factor_df.attrs.get('inserted_rows', len(factor_df)):,}, factors="
        f"{factor_df.attrs.get('factor_count', factor_df['factor_id'].nunique() if not factor_df.empty else 0):,}"
    )


if __name__ == "__main__":
    main()
