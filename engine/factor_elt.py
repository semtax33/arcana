from __future__ import annotations

import argparse
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import pandas as pd

from factor_normalizer import (
    create_stock_factor_dataframe,
    factor_columns,
    normalize_stock_code,
    preferred_factor_columns,
)


CLICKHOUSE_CONFIG = {
    "host": "127.0.0.1",
    "port": 8123,
    "username": "default",
    "password": "default",
    "database": "arcana",
}

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
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_width_pct",
    "bb_percent_b",
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
}

VALUATION_FACTORS = {
    "mcap_mil",
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
    "per",
    "pbr",
    "pcr",
    "psr",
    "peg",
}

QUALITY_FACTORS = {
    "gpm",
    "opm",
    "ebitda_margin",
    "npm",
    "tax_rate",
    "roe",
    "roa",
    "iroe",
    "roic_financial",
    "roic_operational",
    "asset_turnover",
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
    "tdpr",
    "dividend_yield",
    "payout_ratio",
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
    "sstk",
    "prstkc",
    "nopat",
    "working_capital",
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


def get_clickhouse_client(**overrides):
    import clickhouse_connect

    config = {**CLICKHOUSE_CONFIG, **overrides}
    return clickhouse_connect.get_client(**config)


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
    **kwargs,
) -> pd.DataFrame:
    frames = []
    for stock_code in _resolve_stock_codes(stock_codes):
        wide_df = create_stock_factor_dataframe(
            stock_code,
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
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


def _resolve_stock_codes(stock_codes: list[str] | None) -> list[str]:
    if stock_codes is not None:
        return [normalize_stock_code(stock_code) for stock_code in stock_codes]

    from company import kospi_kosdaq_corp_list

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
        print(f"inserted partition={partition}, rows={len(chunk):,}")

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
        f"stocks={len(batch_frames):,}, rows={len(batch_df):,}"
    )
    return _insert_daily_factor_rows_by_partition(client, batch_df)


def create_factor_catalog_dataframe(factor_ids: list[str] | None = None) -> pd.DataFrame:
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    factor_ids = factor_ids if factor_ids is not None else preferred_factor_columns()
    rows = []

    for factor_id in factor_ids:
        rows.append(
            {
                "factor_id": factor_id,
                "factor_name": factor_id.replace("_", " ").upper(),
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
    if factor_id in {"na_5", "na_20", "na_50", "na_150", "na_200"}:
        return "trend"
    if factor_id in {"rsi_14", "macd", "macd_signal", "macd_hist"}:
        return "momentum"
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
        "gpm",
        "opm",
        "ebitda_margin",
        "npm",
        "tax_rate",
        "roe",
        "sharehold_div_yield",
        "sharehold_net_buyback_yield",
        "sharehold_return",
        "dividend_yield",
        "payout_ratio",
        "tdpr",
        "rsi_14",
    }:
        return "percent"
    if factor_id.endswith("_score") or factor_id == "f_score":
        return "score"
    if factor_id.endswith("_times") or factor_id in {"per", "pbr", "pcr", "psr", "peg"}:
        return "times"
    if factor_id in {"shares", "csho"}:
        return "shares"
    if factor_id in {"inv_days", "ar_days", "ap_days", "ccc"}:
        return "days"
    if factor_id in {"bb_upper", "bb_middle", "bb_lower", "macd", "macd_signal", "macd_hist"}:
        return "krw"
    if factor_id.startswith("na_") or factor_id in FUNDAMENTAL_AMOUNT_FACTORS or factor_id in {
        "eps",
        "bps",
        "sps",
        "cps",
        "dvpsp",
        "dvpsx",
        "mcap_mil",
        "sales_change_mil",
        "op_change_mil",
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
    insert_catalog: bool = True,
    dry_run: bool = False,
    client=None,
    insert_batch_size: int = 25,
    insert_max_rows: int = 2_000_000,
    **kwargs,
) -> pd.DataFrame:
    if dry_run:
        factor_df = create_daily_factor_rows(
            stock_codes=stock_codes,
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
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

        resolved_stock_codes = _resolve_stock_codes(stock_codes)
        for stock_index, stock_code in enumerate(resolved_stock_codes, start=1):
            wide_df = create_stock_factor_dataframe(
                stock_code,
                financial_basis=financial_basis,
                start_date=start_date,
                end_date=end_date,
                **kwargs,
            )
            factor_df = prepare_daily_factor_rows(
                wide_df,
                financial_basis=financial_basis,
                sort_rows=False,
            )
            if factor_df.empty:
                continue

            seen_factor_ids.update(factor_df["factor_id"].unique())
            batch_frames.append(factor_df)
            batch_rows += len(factor_df)

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


def _parse_stock_codes(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip().zfill(6) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Insert daily factor rows into ClickHouse.")
    parser.add_argument("--stock-codes", help="Comma-separated stock codes. Defaults to all KOSPI/KOSDAQ stocks.")
    parser.add_argument("--financial-basis", default="annual", choices=["annual", "quarterly", "ttm"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--skip-catalog", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert-batch-size", type=int, default=25)
    parser.add_argument("--insert-max-rows", type=int, default=2_000_000)
    args = parser.parse_args()

    factor_df = insert_daily_factors(
        stock_codes=_parse_stock_codes(args.stock_codes),
        financial_basis=args.financial_basis,
        start_date=args.start_date,
        end_date=args.end_date,
        insert_catalog=not args.skip_catalog,
        dry_run=args.dry_run,
        insert_batch_size=args.insert_batch_size,
        insert_max_rows=args.insert_max_rows,
    )
    print(
        "prepared rows="
        f"{factor_df.attrs.get('inserted_rows', len(factor_df)):,}, factors="
        f"{factor_df.attrs.get('factor_count', factor_df['factor_id'].nunique() if not factor_df.empty else 0):,}"
    )


if __name__ == "__main__":
    main()
